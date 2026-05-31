# core/llm_handler.py
import os
import json
from typing import Optional, Dict, Any

_DEFAULT_MODEL = os.getenv("CORTEX_MODEL", "claude-3-5-sonnet")

# JSON schema for AI_COMPLETE structured output
_CORTEX_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "sql": {"type": "string"},
        "explanation": {"type": "string"}
    },
    "required": ["sql"]
}

_SYSTEM_INSTRUCTIONS = """You are a Snowflake SQL generator.
- Produce a single valid Snowflake SQL query that answers the user question.
- Prefer SELECT queries unless the question explicitly asks for DDL/DML.
- No comments, no code fences, no extra prose around the SQL.
Return STRICT JSON with keys: sql, explanation (short).
"""

def _build_prompt(user_question: str, schema_hint: Optional[str] = None) -> str:
    hint = f"\nSchema context:\n{schema_hint}\n" if schema_hint else ""
    return f"""{_SYSTEM_INSTRUCTIONS}
User question:
{user_question}
{hint}
Return JSON ONLY matching this example:
{{"sql":"SELECT 1","explanation":"why this solves it"}}
"""

def _parse_json_loose(text: str) -> Dict[str, str]:
    try:
        return json.loads(text)
    except Exception:
        t = (text or "").strip()
        s = t.find("{"); e = t.rfind("}")
        if s != -1 and e != -1 and e > s:
            try:
                return json.loads(t[s:e+1])
            except Exception:
                pass
    return {"sql": (text or "").strip(), "explanation": ""}

def _ai_complete(cur, model: str, prompt: str) -> str:
    """Prefer AI_COMPLETE with schema; fallback to SNOWFLAKE.CORTEX.COMPLETE."""
    try:
        q = """
        SELECT AI_COMPLETE(
            %s,
            %s,
            OBJECT_CONSTRUCT(),
            OBJECT_CONSTRUCT('type','json','schema', PARSE_JSON(%s)),
            FALSE
        ) AS out;
        """
        cur.execute(q, (model, prompt, json.dumps(_CORTEX_JSON_SCHEMA)))
        return cur.fetchone()[0]
    except Exception:
        q = "SELECT SNOWFLAKE.CORTEX.COMPLETE(%s, %s) AS out;"
        cur.execute(q, (model, prompt))
        return cur.fetchone()[0]

def generate_sql(conn, question: str, schema_hint: Optional[str] = None, model: Optional[str] = None) -> Dict[str, str]:
    """NL -> SQL using Snowflake Cortex. Returns dict with keys: sql, explanation, raw."""
    mdl = model or _DEFAULT_MODEL
    prompt = _build_prompt(question, schema_hint)
    cur = conn.cursor()
    try:
        raw = _ai_complete(cur, mdl, prompt)
    finally:
        try:
            cur.close()
        except Exception:
            pass
    parsed = _parse_json_loose(raw)
    return {"sql": parsed.get("sql", "").strip(), "explanation": parsed.get("explanation", "").strip(), "raw": raw}

def generate_followups(conn, question: str, model: Optional[str] = None) -> list[str]:
    """Ask Cortex for 1–3 short follow-up questions. Return list[str]."""
    mdl = model or _DEFAULT_MODEL
    system = "Return a JSON array (max 3) of concise follow-up questions about Snowflake data."             " No explanations, no numbering, only array of strings."
    prompt = f"""{system}\nUser question:\n{question}\nReturn JSON array like [\"Q1\", \"Q2\"]."""
    cur = conn.cursor()
    try:
        # Ask AI_COMPLETE but with a simple json response format (no schema needed)
        q = """
        SELECT AI_COMPLETE(
            %s,
            %s,
            OBJECT_CONSTRUCT(),
            OBJECT_CONSTRUCT('type','json'),
            FALSE
        ) AS out;
        """
        try:
            cur.execute(q, (mdl, prompt))
            raw = cur.fetchone()[0]
        except Exception:
            # fallback
            cur.execute("SELECT SNOWFLAKE.CORTEX.COMPLETE(%s, %s) AS out;", (mdl, prompt))
            raw = cur.fetchone()[0]
    finally:
        try:
            cur.close()
        except Exception:
            pass
    try:
        arr = json.loads(raw)
        if isinstance(arr, list):
            return [str(x).strip() for x in arr if str(x).strip()][:3]
        # if it wasn't array, try crude split
        return [s.strip("-• ").strip() for s in str(raw).splitlines() if s.strip()][:3]
    except Exception:
        return [s.strip("-• ").strip() for s in str(raw).splitlines() if s.strip()][:3]
