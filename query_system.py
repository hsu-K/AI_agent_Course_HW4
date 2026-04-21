"""Minimal KG query template for Assignment 4.

Keep these APIs unchanged for auto-test:
- generate_text(messages, max_new_tokens=220)
- get_relevant_articles(question)
- generate_answer(question, rule_results)

Keep Rule fields aligned with build_kg output:
rule_id, type, action, result, art_ref, reg_name
"""

import os
from typing import Any

from neo4j import GraphDatabase
from dotenv import load_dotenv

from llm_loader import load_local_llm, get_tokenizer, get_raw_pipeline

# ========== 0) Initialization ==========
load_dotenv()

URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
AUTH = (
	os.getenv("NEO4J_USER", "neo4j"),
	os.getenv("NEO4J_PASSWORD", "password"),
)

# Avoid local proxy settings interfering with model/Neo4j access.
for key in ["http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY"]:
	if key in os.environ:
		del os.environ[key]


try:
	driver = GraphDatabase.driver(URI, auth=AUTH)
	driver.verify_connectivity()
except Exception as e:
	print(f"⚠️ Neo4j connection warning: {e}")
	driver = None


# ========== 1) Public API (query flow order) ==========
# Order: extract_entities -> build_typed_cypher -> get_relevant_articles -> generate_answer

def generate_text(messages: list[dict[str, str]], max_new_tokens: int = 220) -> str:
	"""
	Call local HF model via chat template + raw pipeline.

	Interface:
	- Input:
	  - messages: list[dict[str, str]] (chat messages with role/content)
	  - max_new_tokens: int
	- Output:
	  - str (model generated text, no JSON guarantee)
	"""
	tok = get_tokenizer()
	pipe = get_raw_pipeline()
	if tok is None or pipe is None:
		load_local_llm()
		tok = get_tokenizer()
		pipe = get_raw_pipeline()
	prompt = tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
	return pipe(prompt, max_new_tokens=max_new_tokens)[0]["generated_text"].strip()


def extract_entities(question: str) -> dict[str, Any]:
	"""TODO(student, required): parse question to {question_type, subject_terms, aspect}."""
	"""Parse question to extract entities for retrieval."""
	# Use LLM to understand the question
	messages = [
		{
			"role": "user",
			"content": f"""Analyze this question and extract:
1. question_type: "penalty", "requirement", "procedure", "fee", or "general"
2. subject_terms: key nouns/subjects (e.g., ["student ID", "late"])
3. aspect: "penalty", "condition", "process", or "general"

Question: {question}

Return JSON format only:
{{"question_type": "...", "subject_terms": [...], "aspect": "..."}}"""
		}
	]
	
	try:
		response = generate_text(messages, max_new_tokens=100)
		import json
		import re
		json_match = re.search(r'\{.*\}', response, re.DOTALL)
		if json_match:
			result = json.loads(json_match.group())
			return result
	except Exception as e:
		print(f"[Warning] Entity extraction failed: {e}")
	
	# Fallback: simple keyword extraction
	return {
		"question_type": "general",
		"subject_terms": question.lower().split(),
		"aspect": "general",
	}


def build_typed_cypher(entities: dict[str, Any]) -> tuple[str, str]:
	"""TODO(student, required): return (typed_query, broad_query) with score and required fields."""
	"""Build typed (precise) and broad (fuzzy) Cypher queries."""
	subject_terms = entities.get("subject_terms", [])
	question_type = entities.get("question_type", "general")
	aspect = entities.get("aspect", "general")
	
	# Build typed query: precise match on terms and rule type
	where_conditions = []
	
	# Match rule type if available
	if question_type in ["penalty", "requirement", "obligation", "prohibition"]:
		where_conditions.append(f'r.type = "{question_type}"')
	
	# Match subject terms in action or result
	if subject_terms:
		term_conditions = []
		for term in subject_terms[:3]:  # Limit to 3 terms to avoid over-constraint
			if len(term) > 2:  # Skip very short terms
				term_conditions.append(f'(r.action CONTAINS "{term}" OR r.result CONTAINS "{term}")')
		if term_conditions:
			where_conditions.append(" OR ".join(term_conditions))
	
	cypher_typed = f"""
	MATCH (a:Article)-[:CONTAINS_RULE]->(r:Rule)
	WHERE {" AND ".join(where_conditions) if where_conditions else "r.rule_id IS NOT NULL"}
	RETURN r.rule_id AS rule_id, r.type AS type, r.action AS action, 
		   r.result AS result, r.art_ref AS art_ref, r.reg_name AS reg_name,
		   a.content AS article_content, a.number AS article_number
	LIMIT 10
	""" if where_conditions else ""
	
	# Build broad query: fulltext search
	search_terms = " OR ".join(subject_terms[:5]) if subject_terms else "regulation"
	cypher_broad = f"""
	CALL db.index.fulltext.queryNodes("rule_idx", "{search_terms}")
	YIELD node AS r, score
	MATCH (a:Article)-[:CONTAINS_RULE]->(r)
	RETURN r.rule_id AS rule_id, r.type AS type, r.action AS action,
		   r.result AS result, r.art_ref AS art_ref, r.reg_name AS reg_name,
		   a.content AS article_content, a.number AS article_number,
		   score
	ORDER BY score DESC
	LIMIT 10
	"""
	
	return cypher_typed, cypher_broad


def get_relevant_articles(question: str) -> list[dict[str, Any]]:
	"""
	Retrieve relevant rules from Neo4j using typed+broad strategy.
	
	Flow:
	1. Extract entities from question
	2. Build typed (precise) and broad (fuzzy) queries
	3. Execute typed query first
	4. If insufficient results, execute broad query
	5. Merge and deduplicate results
	"""

	if driver is None:
		print("[Error] Neo4j driver not available")
		return []
	
	# Step 1: Extract entities
	entities = extract_entities(question)
	print(f"[Debug] Extracted entities: {entities}")
	
	# Step 2: Build queries
	cypher_typed, cypher_broad = build_typed_cypher(entities)
	
	# Step 3: Execute typed query
	results_dict = {}  # Use dict to deduplicate by rule_id
	
	with driver.session() as session:
		# Try typed query first (if not empty)
		if cypher_typed.strip():
			try:
				print("[Query] Executing typed query...")
				typed_results = session.run(cypher_typed)
				for record in typed_results:
					rule = {
						"rule_id": record.get("rule_id"),
						"type": record.get("type"),
						"action": record.get("action"),
						"result": record.get("result"),
						"art_ref": record.get("art_ref"),
						"reg_name": record.get("reg_name"),					"article_content": record.get("article_content"),
					"article_number": record.get("article_number"),					}
					if rule["rule_id"]:
						results_dict[rule["rule_id"]] = rule
				print(f"[Result] Found {len(results_dict)} rules from typed query")
			except Exception as e:
				print(f"[Warning] Typed query failed: {e}")
		
		# If insufficient results, execute broad query
		if len(results_dict) < 3:
			try:
				print("[Query] Executing broad query...")
				broad_results = session.run(cypher_broad)
				for record in broad_results:
					rule = {
						"rule_id": record.get("rule_id"),
						"type": record.get("type"),
						"action": record.get("action"),
						"result": record.get("result"),
						"art_ref": record.get("art_ref"),
						"reg_name": record.get("reg_name"),					"article_content": record.get("article_content"),
					"article_number": record.get("article_number"),					}
					if rule["rule_id"] and rule["rule_id"] not in results_dict:
						results_dict[rule["rule_id"]] = rule
				print(f"[Result] Found {len(results_dict)} total rules after broad query")
			except Exception as e:
				print(f"[Warning] Broad query failed: {e}")
	
	return list(results_dict.values())


def generate_answer(question: str, rule_results: list[dict[str, Any]]) -> str:
	"""TODO(student, required): generate grounded answer from retrieved rules only."""
	"""Generate grounded answer from retrieved rules using LLM."""
	# return "Insufficient rule evidence to answer this question."
	# Check if we have relevant rules
	if not rule_results:
		return "Insufficient rule evidence to answer this question."
	
	# Format rules for the LLM (include full Article content)
	rules_text = ""
	for i, rule in enumerate(rule_results, 1):
		rules_text += f"\nRule {i}:\n"
		rules_text += f"  Type: {rule.get('type', 'unknown')}\n"
		rules_text += f"  Action: {rule.get('action', '')}\n"
		rules_text += f"  Result: {rule.get('result', '')}\n"
		rules_text += f"  Article: {rule.get('art_ref', '')}\n"
		rules_text += f"  Regulation: {rule.get('reg_name', '')}\n"
		# ✅ Add full Article content so LLM can read the complete text
		if rule.get('article_content'):
			rules_text += f"  Full Article Text:\n    {rule.get('article_content')}\n"
	
	# Create prompt for LLM to generate answer
	messages = [
		{
			"role": "user",
			"content": f"""Based on the following regulations, answer the question concisely and cite the relevant article/regulation.

Question: {question}

Relevant Rules:
{rules_text}

Answer (be concise, cite sources):"""
		}
	]
	
	try:
		answer = generate_text(messages, max_new_tokens=150)
		return answer.strip()
	except Exception as e:
		print(f"[Error] Answer generation failed: {e}")
		# Fallback: construct simple answer from first rule
		if rule_results:
			first_rule = rule_results[0]
			return f"Based on {first_rule.get('reg_name', 'the regulations')}, Article {first_rule.get('art_ref', '')}: {first_rule.get('result', 'See regulations for details.')}"
		return "Unable to generate answer."


def main() -> None:
	"""Interactive CLI (provided scaffold)."""
	if driver is None:
		return

	load_local_llm()

	print("=" * 50)
	print("🎓 NCU Regulation Assistant (Template)")
	print("=" * 50)
	print("💡 Try: 'What is the penalty for forgetting student ID?'")
	print("👉 Type 'exit' to quit.\n")

	while True:
		try:
			user_q = input("\nUser: ").strip()
			if not user_q:
				continue
			if user_q.lower() in {"exit", "quit"}:
				print("👋 Bye!")
				break

			results = get_relevant_articles(user_q)
			answer = generate_answer(user_q, results)
			print(f"Bot: {answer}")

		except KeyboardInterrupt:
			print("\n👋 Bye!")
			break
		except NotImplementedError as e:
			print(f"⚠️ {e}")
			break
		except Exception as e:
			print(f"❌ Error: {e}")

	driver.close()


if __name__ == "__main__":
	main()