"""Minimal KG builder template for Assignment 4.

Keep this contract unchanged:
- Graph: (Regulation)-[:HAS_ARTICLE]->(Article)-[:CONTAINS_RULE]->(Rule)
- Article: number, content, reg_name, category
- Rule: rule_id, type, action, result, art_ref, reg_name
- Fulltext indexes: article_content_idx, rule_idx
- SQLite file: ncu_regulations.db
"""

import os
import sqlite3
from typing import Any

from dotenv import load_dotenv
from neo4j import GraphDatabase

from llm_loader import load_local_llm, get_tokenizer, get_raw_pipeline


# ========== 0) Initialization ==========
load_dotenv()

URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
AUTH = (
    os.getenv("NEO4J_USER", "neo4j"),
    os.getenv("NEO4J_PASSWORD", "password"),
)


def extract_entities(article_number: str, reg_name: str, content: str) -> dict[str, Any]:
    """TODO(student, required): implement LLM extraction and return {"rules": [...]}"""
    pipeline = get_raw_pipeline()
    tokenizer = get_tokenizer()

    if not pipeline or not tokenizer:
        print("[Error] LLM pipeline or tokenizer not available.")
        return {"rules": []}

    prompt = f"""You are a legal article analysis assistant. Analyze the following article and extract the rules within it.

Regulation: {reg_name}
Article Number: {article_number}
Article Content:
{content}

Please extract rules in JSON format, returning a list of rules. Each rule should contain:
- type: rule type ("prohibition", "obligation", "penalty", "requirement")
- action: specific action or condition
- result: consequence or outcome

Return format:
[
  {{"type": "...", "action": "...", "result": "..."}},
  ...
]

Return only JSON, no other text."""

    # 聊天模板
    messages = [{"role": "user", "content": prompt}]
    chat_text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    
        # Call LLM
    output = pipeline(chat_text, max_new_tokens=512)
    response_text = output[0]["generated_text"].strip()

    # Parse JSON
    import json
    try:
        # Try to extract JSON part
        start_idx = response_text.find('[')
        end_idx = response_text.rfind(']') + 1
        if start_idx != -1 and end_idx > start_idx:
            json_str = response_text[start_idx:end_idx]
            rules = json.loads(json_str)
            return {"rules": rules if isinstance(rules, list) else []}
    except (json.JSONDecodeError, ValueError):
        pass

    return {
        "rules": []
    }


def build_fallback_rules(article_number: str, content: str) -> list[dict[str, str]]:
    """TODO(student, optional): add deterministic fallback rules."""
    return []


# SQLite tables used:
# - regulations(reg_id, name, category)
# - articles(reg_id, article_number, content)


def build_graph() -> None:
    """Build KG from SQLite into Neo4j using the fixed assignment schema."""
    sql_conn = sqlite3.connect("ncu_regulations.db")
    cursor = sql_conn.cursor()
    driver = GraphDatabase.driver(URI, auth=AUTH)

    # Optional: warm up local LLM
    load_local_llm()

    with driver.session() as session:
        # Fixed strategy: clear existing graph data before rebuilding.
        session.run("MATCH (n) DETACH DELETE n")

        # 1) Read regulations and create Regulation nodes.
        cursor.execute("SELECT reg_id, name, category FROM regulations")
        regulations = cursor.fetchall()
        reg_map: dict[int, tuple[str, str]] = {}

        for reg_id, name, category in regulations:
            reg_map[reg_id] = (name, category)
            session.run(
                "MERGE (r:Regulation {id:$rid}) SET r.name=$name, r.category=$cat",
                rid=reg_id,
                name=name,
                cat=category,
            )

        # 2) Read articles and create Article + HAS_ARTICLE.
        cursor.execute("SELECT reg_id, article_number, content FROM articles")
        articles = cursor.fetchall()

        for reg_id, article_number, content in articles:
            reg_name, reg_category = reg_map.get(reg_id, ("Unknown", "Unknown"))
            session.run(
                """
                MATCH (r:Regulation {id: $rid})
                CREATE (a:Article {
                    number:   $num,
                    content:  $content,
                    reg_name: $reg_name,
                    category: $reg_category
                })
                MERGE (r)-[:HAS_ARTICLE]->(a)
                """,
                rid=reg_id,
                num=article_number,
                content=content,
                reg_name=reg_name,
                reg_category=reg_category,
            )

        # 3) Create full-text index on Article content.
        session.run(
            """
            CREATE FULLTEXT INDEX article_content_idx IF NOT EXISTS
            FOR (a:Article) ON EACH [a.content]
            """
        )

        rule_counter = 0

        # TODO(student, required):
        # - iterate through all articles
        # - call extract_entities(article_number, reg_name, content)
        # - skip invalid rules with empty action/result
        # - generate unique rule_id and deduplicate logically similar rules
        # - create Rule nodes with required properties and link via CONTAINS_RULE
        for reg_id, article_number, content in articles:
            print(f"Processing Article {article_number} of Regulation ID {reg_id}...")
            reg_name, reg_category = reg_map.get(reg_id, ("Unknown", "Unknown"))
            
            # 利用LLM提取規則
            entities = extract_entities(article_number, reg_name, content)
            rules = entities.get("rules", []) + build_fallback_rules(article_number, content)

            for rule in rules:
                # 過濾無效規則
                if not rule.get("action") or not rule.get("result"):
                    continue
            
                # 生成唯一 ID
                rule_id = f"rule_{rule_counter}"
                rule_counter += 1
                
                # 建立 Rule 節點
                session.run(
                    """
                    MATCH (a:Article {number: $num})
                    CREATE (r:Rule {
                        rule_id: $rid,
                        type: $type,
                        action: $action,
                        result: $result,
                        art_ref: $art_ref,
                        reg_name: $reg_name
                    })
                    MERGE (a)-[:CONTAINS_RULE]->(r)
                    """,
                    num=article_number,
                    rid=rule_id,
                    type=rule.get("type", "unknown"),
                    action=rule.get("action", ""),
                    result=rule.get("result", ""),
                    art_ref=article_number,
                    reg_name=reg_name
                )


        print("[+] Graph build completed. Total rules extracted:", rule_counter)
        # 4) Create full-text index on Rule fields.
        session.run(
            """
            CREATE FULLTEXT INDEX rule_idx IF NOT EXISTS
            FOR (r:Rule) ON EACH [r.action, r.result]
            """
        )

        # 5) Coverage audit (provided scaffold).
        coverage = session.run(
            """
            MATCH (a:Article)
            OPTIONAL MATCH (a)-[:CONTAINS_RULE]->(r:Rule)
            WITH a, count(r) AS rule_count
            RETURN count(a) AS total_articles,
                   sum(CASE WHEN rule_count > 0 THEN 1 ELSE 0 END) AS covered_articles,
                   sum(CASE WHEN rule_count = 0 THEN 1 ELSE 0 END) AS uncovered_articles
            """
        ).single()

        total_articles = int((coverage or {}).get("total_articles", 0) or 0)
        covered_articles = int((coverage or {}).get("covered_articles", 0) or 0)
        uncovered_articles = int((coverage or {}).get("uncovered_articles", 0) or 0)

        print(
            f"[Coverage] covered={covered_articles}/{total_articles}, "
            f"uncovered={uncovered_articles}"
        )

    driver.close()
    sql_conn.close()


if __name__ == "__main__":
    build_graph()
