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
	"""Parse question to extract rule types, subject terms, and key aspects."""
	# Use LLM to understand the question and map to rule types
	messages = [
		{
			"role": "user",
			"content": f"""Analyze this regulation question and extract key information for retrieval.

Question: {question}

Extract:
1. rule_types: relevant rule types from ["Prohibition", "Obligation", "Requirement", "Permissions", "Incentive Rules"] 
   (e.g., if question asks about penalties -> "Prohibition"; if asks about what's required -> "Obligation")
2. subject_terms: 2-5 key nouns/subjects (e.g., ["student", "withdrawal", "penalty"])
3. keywords: important keywords to search in action/result fields

Return JSON format only:
{{"rule_types": [...], "subject_terms": [...], "keywords": [...]}}

Example:
- Q: "What is the penalty for late submission?"
- A: {{"rule_types": ["Prohibition"], "subject_terms": ["submission", "penalty", "late"], "keywords": ["late", "penalty", "submission"]}}
"""
		}
	]
	
	try:
		response = generate_text(messages, max_new_tokens=120)
		import json
		import re
		json_match = re.search(r'\{.*\}', response, re.DOTALL)
		if json_match:
			result = json.loads(json_match.group())
			return result
	except Exception as e:
		print(f"[Warning] Entity extraction failed: {e}")
	
	# Fallback: simple keyword extraction
	words = question.lower().split()
	return {
		"rule_types": [],
		"subject_terms": [w for w in words if len(w) > 2][:5],
		"keywords": [w for w in words if len(w) > 3][:5],
	}


def build_typed_cypher(entities: dict[str, Any]) -> tuple[str, str]:
	"""Build typed (precise) and broad (fuzzy) Cypher queries based on extracted entities."""
	rule_types = entities.get("rule_types", [])
	subject_terms = entities.get("subject_terms", [])
	keywords = entities.get("keywords", [])
	
	# Build typed query: precise match on rule types and keywords
	where_conditions = []
	
	# 1) Match rule types if identified
	if rule_types:
		type_conditions = [f'r.type = "{rt}"' for rt in rule_types]
		where_conditions.append("(" + " OR ".join(type_conditions) + ")")
	
	# 2) Match keywords in action or result fields
	if keywords:
		keyword_conditions = []
		for kw in keywords[:4]:  # Limit to 4 keywords
			if len(kw) > 1:
				keyword_conditions.append(f'(r.action CONTAINS "{kw}" OR r.result CONTAINS "{kw}")')
		if keyword_conditions:
			where_conditions.append("(" + " OR ".join(keyword_conditions) + ")")
	
	# Build typed query with conditions
	cypher_typed = ""
	if where_conditions:
		cypher_typed = f"""
	MATCH (a:Article)-[:CONTAINS_RULE]->(r:Rule)
	WHERE {" AND ".join(where_conditions)}
	RETURN r.rule_id AS rule_id, r.type AS type, r.action AS action, 
		   r.result AS result, r.art_ref AS art_ref, r.reg_name AS reg_name,
		   a.content AS article_content, a.number AS article_number, a.category AS article_category
	LIMIT 15
	"""
	
	# Build broad query: fulltext search on all keywords
	search_terms = " OR ".join(set(keywords + subject_terms))  # Deduplicate combined terms
	if not search_terms or search_terms.strip() == "":
		search_terms = "regulation"
	
	cypher_broad = f"""
	CALL db.index.fulltext.queryNodes("rule_idx", "{search_terms}")
	YIELD node AS r, score
	MATCH (a:Article)-[:CONTAINS_RULE]->(r)
	RETURN r.rule_id AS rule_id, r.type AS type, r.action AS action,
		   r.result AS result, r.art_ref AS art_ref, r.reg_name AS reg_name,
		   a.content AS article_content, a.number AS article_number, a.category AS article_category,
		   score
	ORDER BY score DESC
	LIMIT 15
	"""
	
	return cypher_typed, cypher_broad


def get_relevant_articles(question: str) -> list[dict[str, Any]]:
	"""
	Retrieve relevant rules from Neo4j using enhanced retrieval strategy.
	
	Flow:
	1. Extract rule_types, subject_terms, and keywords from question using LLM
	2. Build typed query (with rule type filtering) and broad query (fulltext search)
	3. Execute typed query first for precision
	4. If insufficient results (<5), execute broad query for coverage
	5. Merge and deduplicate results, returning rich information for answer generation
	"""

	if driver is None:
		print("[Error] Neo4j driver not available")
		return []
	
	# Step 1: Extract entities using enhanced method
	entities = extract_entities(question)
	print(f"[Debug] Extracted entities: {entities}")
	
	# Step 2: Build queries
	cypher_typed, cypher_broad = build_typed_cypher(entities)
	
	# Step 3: Execute queries and merge results
	results_dict = {}  # Use dict to deduplicate by rule_id
	source_articles = {}  # Track article sources for citing
	
	with driver.session() as session:
		# Try typed query first (if not empty)
		if cypher_typed.strip():
			try:
				print("[Query] Executing typed query with rule type filtering...")
				typed_results = session.run(cypher_typed)
				typed_count = 0
				for record in typed_results:
					rule = {
						"rule_id": record.get("rule_id"),
						"type": record.get("type"),
						"action": record.get("action"),
						"result": record.get("result"),
						"art_ref": record.get("art_ref"),
						"reg_name": record.get("reg_name"),					
						"article_content": record.get("article_content"),
						"article_number": record.get("article_number"),
						"article_category": record.get("article_category"),
						"relevance_score": 0.9,  # Typed query has higher confidence
					}
					if rule["rule_id"]:
						results_dict[rule["rule_id"]] = rule
						source_articles[rule["article_number"]] = {
							"number": rule["article_number"],
							"regulation": rule["reg_name"],
							"category": rule.get("article_category", "")
						}
						typed_count += 1
				print(f"[Result] Found {typed_count} rules from typed query")
			except Exception as e:
				print(f"[Warning] Typed query failed: {e}")
		
		# If insufficient results, execute broad query for coverage
		if len(results_dict) < 5:
			try:
				print("[Query] Executing broad fulltext search...")
				broad_results = session.run(cypher_broad)
				broad_count = 0
				for record in broad_results:
					rule_id = record.get("rule_id")
					if rule_id and rule_id not in results_dict:
						rule = {
							"rule_id": rule_id,
							"type": record.get("type"),
							"action": record.get("action"),
							"result": record.get("result"),
							"art_ref": record.get("art_ref"),
							"reg_name": record.get("reg_name"),					
							"article_content": record.get("article_content"),
							"article_number": record.get("article_number"),
							"article_category": record.get("article_category"),
							"relevance_score": record.get("score", 0.5),
						}
						results_dict[rule_id] = rule
						source_articles[rule["article_number"]] = {
							"number": rule["article_number"],
							"regulation": rule["reg_name"],
							"category": rule.get("article_category", "")
						}
						broad_count += 1
				print(f"[Result] Found {broad_count} additional rules from broad query, total: {len(results_dict)}")
			except Exception as e:
				print(f"[Warning] Broad query failed: {e}")
	
	# Sort by relevance score
	result_list = sorted(results_dict.values(), key=lambda x: x.get("relevance_score", 0), reverse=True)
	print(f"[Summary] Retrieved {len(result_list)} unique rules from {len(source_articles)} articles")
	
	return result_list


def generate_answer(question: str, rule_results: list[dict[str, Any]]) -> str:
	"""
	Generate grounded answer from retrieved rules using LLM.
	
	The answer must cite sources (Article number and Regulation name).
	Format: Include Rule type, action, result, and full Article context.
	"""
	# Check if we have relevant rules
	if not rule_results:
		return "Insufficient rule evidence to answer this question."
	
	# Format rules for the LLM (include full Article content)
	rules_text = ""
	cited_sources = set()
	
	for i, rule in enumerate(rule_results[:10], 1):  # Limit to top 10 results
		rules_text += f"\n[Rule {i}]\n"
		rules_text += f"  Type: {rule.get('type', 'unknown')}\n"
		rules_text += f"  Action/Condition: {rule.get('action', '')}\n"
		rules_text += f"  Result/Consequence: {rule.get('result', '')}\n"
		rules_text += f"  Source: {rule.get('reg_name', '')} Article {rule.get('art_ref', '')}\n"
		rules_text += f"  Relevance Score: {rule.get('relevance_score', 0):.2f}\n"
		
		# Track sources for final citation
		cited_sources.add((rule.get('art_ref', ''), rule.get('reg_name', '')))
		
		# Add full Article content for context
		if rule.get('article_content'):
			# Truncate very long content
			content = rule.get('article_content', '')
			if len(content) > 500:
				content = content[:500] + "..."
			rules_text += f"  Full Article Text:\n    {content}\n"
	
	# Create enhanced prompt for LLM with source citation requirement
	messages = [
		{
			"role": "user",
			"content": f"""Based on the following regulations, answer the question concisely and cite relevant articles and regulation names clearly.

Question: {question}

Relevant Rules:
{rules_text}

Provide a concise answer in the following format:
1. Directly answer the question
2. Cite relevant regulation names and article numbers
3. Mention the relevant rule type (Prohibition/Obligation/Requirement/Permission, etc.)

Answer (Concise and clear, must cite sources):"""
		}
	]
	
	try:
		answer = generate_text(messages, max_new_tokens=200)
		
		# Ensure sources are cited in the answer
		sources_citation = "\n\n[References]"
		for art_ref, reg_name in sorted(cited_sources):
			sources_citation += f"\n- {reg_name} Article {art_ref}"
		
		return answer.strip() + sources_citation
		
	except Exception as e:
		print(f"[Error] Answer generation failed: {e}")
		# Fallback: construct structured answer from rules
		fallback_answer = "Based on the following rules:\n"
		for i, rule in enumerate(rule_results[:3], 1):
			fallback_answer += f"\n{i}. ({rule.get('type', 'unknown')}) "
			fallback_answer += f"{rule.get('action', '')} → {rule.get('result', '')}\n"
			fallback_answer += f"   Source: {rule.get('reg_name', '')} Article {rule.get('art_ref', '')}"
		
		return fallback_answer


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