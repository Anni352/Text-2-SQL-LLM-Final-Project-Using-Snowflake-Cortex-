# core/sql_generator.py
import os, re
from typing import Optional, Dict
from .llm_handler import generate_sql as _gen_sql, generate_followups as _gen_followups

def normalize_table_mentions(question: str) -> str:
    """
    Unless the user specifies a fully-qualified 3-part name database.schema.table,
    treat any 1-part or 2-part identifier as a TABLE name (not a column, not schema.table).
    That means we collapse tokens like `schema.table` -> `table` in the natural-language question
    before prompting the model. We do NOT touch 3-part names.
    """
    if not question:
        return question
    # Replace schema.table (two-part) with just table, but preserve database.schema.table (three-part)
    # Pattern matches word.word not followed by a dot (to avoid three-part).
    return re.sub(r"\b([A-Za-z_][\w$]*)\.([A-Za-z_][\w$]*)\b(?!\.)", r"\2", question)

def _load_system_prompt() -> str:
    p = os.path.join("config", "system_prompt.txt")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return "Return only SQL fenced in ```sql ...``` with no prose."

def _to_fenced(sql_plain: str) -> str:
    sql_plain = (sql_plain or "").strip()
    if re.search(r"^```", sql_plain):  # already fenced
        return sql_plain
    return f"```sql\n{sql_plain}\n```" if sql_plain else ""

def generate_sql_from_question(conn, question: str, schema_hint: Optional[str] = None) -> str:
    """Return SQL fenced in ```sql ...``` for downstream code."""
    # Normalize table mentions so 1-2 part identifiers are treated as table names only
    question = normalize_table_mentions(question)
    out = _gen_sql(conn=conn, question=question, schema_hint=schema_hint)
    return _to_fenced(out.get("sql", ""))

def generate_followups(conn, question: str) -> list[str]:
    return _gen_followups(conn=conn, question=question)
