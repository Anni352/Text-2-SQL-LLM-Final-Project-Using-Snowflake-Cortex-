from core import diff_tools
from utils.helpers import df_bool_to_text, quote_ident, quote_fqn

# --- data diff intent detector (tables/data MINUS) ---
import re as _re_dt
_DATA_DIFF_RE = _re_dt.compile(r'\b(compare|diff|difference)\b[\s\w]*\b(table|tables|data|rows|columns?)\b', _re_dt.IGNORECASE)
_VS_TOKENS_RE = _re_dt.compile(r'\b(vs|versus|and|with)\b', _re_dt.IGNORECASE)
def detect_table_column_data_diff_intent(text: str) -> bool:
    return bool(_DATA_DIFF_RE.search(text)) and bool(_VS_TOKENS_RE.search(text))


import io
import re
import json
import hashlib
import pandas as pd
import streamlit as st


# === Persisted result helpers for The Differences page (page-scoped) ===
import pandas as pd
from datetime import datetime

def _pack_df(df):
    if df is None:
        return None
    try:
        return df.to_json(orient="split", date_unit="ms")
    except Exception:
        return df.astype(object).applymap(lambda x: x if isinstance(x, (int, float, float)) else str(x)).to_json(orient="split", date_unit="ms")

def _unpack_df(s):
    if not s:
        return None
    try:
        return pd.read_json(s, orient="split", dtype=False)
    except Exception:
        return pd.read_json(s, orient="split")

def _remember_diff_page_store(payload: dict, active_tab: str | None = None):
    ss = st.session_state
    ss['diff_page_store'] = {
        'saved_at': datetime.utcnow().isoformat() + 'Z',
        'active_tab': active_tab,
        'meta': {
            'A': payload.get('table_a') or payload.get('A'),
            'B': payload.get('table_b') or payload.get('B'),
            'columns_compared': payload.get('columns_compared', []),
            'verdict': payload.get('verdict'),
            'summary': payload.get('summary', ''),
            'queries': payload.get('queries', {})
        },
        'frames': {
            'A_MINUS_B': _pack_df(payload.get('A_MINUS_B')),
            'B_MINUS_A': _pack_df(payload.get('B_MINUS_A')),
            'PER_COL': _pack_df(payload.get('PER_COL')),
            'UNION': _pack_df(payload.get('UNION')),
            # fallback names seen in this file
            'SRC_MINUS_TGT': _pack_df(payload.get('SRC_MINUS_TGT')),
            'TGT_MINUS_SRC': _pack_df(payload.get('TGT_MINUS_SRC')),
            'UNION_DF': _pack_df(payload.get('UNION_DF')),
        }
    }

def _recall_diff_page_store():
    packed = st.session_state.get('diff_page_store')
    if not packed:
        return None
    return {
        'saved_at': packed['saved_at'],
        'active_tab': packed.get('active_tab'),
        'A': packed['meta'].get('A'),
        'B': packed['meta'].get('B'),
        'columns_compared': packed['meta'].get('columns_compared', []),
        'verdict': packed['meta'].get('verdict'),
        'summary': packed['meta'].get('summary', ''),
        'queries': packed['meta'].get('queries', {}),
        'A_MINUS_B': _unpack_df(packed['frames'].get('A_MINUS_B')) or _unpack_df(packed['frames'].get('SRC_MINUS_TGT')),
        'B_MINUS_A': _unpack_df(packed['frames'].get('B_MINUS_A')) or _unpack_df(packed['frames'].get('TGT_MINUS_SRC')),
        'PER_COL': _unpack_df(packed['frames'].get('PER_COL')),
        'UNION': _unpack_df(packed['frames'].get('UNION')) or _unpack_df(packed['frames'].get('UNION_DF')),
    }

# Project modules (keep your existing module files)
from utils.snowflake_connector import SnowflakeConnector
from core.sql_generator import generate_sql_from_question, generate_followups
from core.query_runner import run_query
from core.visualization import summarize_df
from core.qa_tests import run_custom_tests   # batch test runner
from core.diff_tools import diff_minus, explain_diff, suggestions_from_explain


# ===== Helpers for full-session memory, table resolution & boolean display =====
# df_bool_to_text, quote_ident, quote_fqn are imported from utils.helpers (see top of file).

_TABLE_FROM_SQL = re.compile(
    r"""from\s+
        (?P<table>(?:[A-Z_][A-Z0-9_$]*\.){0,2}[A-Z_][A-Z0-9_$]*)""",
    re.IGNORECASE | re.VERBOSE,
)

def parse_table_from_sql(sql: str) -> str | None:
    if not sql:
        return None
    m = _TABLE_FROM_SQL.search(sql)
    return m.group("table").strip() if m else None

def is_catalog_table(fqn: str) -> bool:
    if not fqn:
        return False
    parts = [p.strip().strip('"').upper() for p in fqn.split(".")]
    return any(p == "INFORMATION_SCHEMA" for p in parts)

def fully_qualify_if_needed(table: str, ctx: dict) -> str:
    """
    Only accept TABLE or DB.SCHEMA.TABLE.
    If TABLE -> qualify with current DB+SCHEMA.
    If DB.SCHEMA.TABLE -> return as is.
    If SCHEMA.TABLE -> raise error (disallowed).
    """
    if not table:
        return table

    # split, strip quotes/whitespace; keep only non-empty parts
    parts = [p.strip().strip('"') for p in str(table).split(".") if p.strip()]

    # DB.SCHEMA.TABLE  → allowed, pass through exactly (no quoting)
    if len(parts) == 3:
        return ".".join(parts)

    # SCHEMA.TABLE → avoid 2‑part by coercing to TABLE or DB.SCHEMA.TABLE
    if len(parts) == 2:
        sc, tbl = parts
        db = (ctx or {}).get('database')
        if db:
            return f"{db}.{sc}.{tbl}"
        # Without DB context, fall back to TABLE (never emit SCHEMA.TABLE)
        return tbl

    # TABLE → qualify with current DB+SCHEMA from context
    if len(parts) == 1:
        db = (ctx or {}).get("database")
        sc = (ctx or {}).get("schema")
        if not db or not sc:
            raise ValueError("Current database and schema must be set to resolve unqualified table names.")
        return f"{db}.{sc}.{parts[0]}"

    # Anything else is invalid
    raise ValueError(f"Invalid table identifier: {table}")


def get_table_columns(conn, table_fqn: str) -> list[str]:
    """
    Prefer to read the column order directly from the target table (SELECT * LIMIT 0),
    and only fall back to INFORMATION_SCHEMA.COLUMNS if needed.
    """
    if not table_fqn:
        return []
    try:
        # Try to infer from the table itself (no rows, just headers)
        sql0 = f"SELECT * FROM {table_fqn} LIMIT 0;"
        df0 = run_query(conn, sql0)
        if isinstance(df0, pd.DataFrame):
            cols = [str(c) for c in df0.columns.tolist()]
            if cols:
                return cols
    except Exception:
        pass
    # Fallback to INFORMATION_SCHEMA for ordering
    try:
        parts = [p.strip().strip('"') for p in table_fqn.split(".")]
        db = sc = tbl = None
        if len(parts) == 3:
            db, sc, tbl = parts
        elif len(parts) == 2:
            sc, tbl = parts
        elif len(parts) == 1:
            tbl = parts[0]
        db_clause = f"{quote_ident(db)}." if db else ""
        sc_lit = (sc or "").replace("'", "''")
        tbl_lit = (tbl or "").replace("'", "''")
        sql = f"""
            SELECT COLUMN_NAME
            FROM {db_clause}INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = '{sc_lit}' AND TABLE_NAME = '{tbl_lit}'
            ORDER BY ORDINAL_POSITION
        """
        df = run_query(conn, sql)
        if isinstance(df, pd.DataFrame) and 'COLUMN_NAME' in df.columns:
            return df['COLUMN_NAME'].astype(str).tolist()
    except Exception:
        return []
    return []


# Memory utilities
MAX_RECENT_SQL = 5

def _remember_sql(chat_name: str, sql_text: str):
    if not sql_text:
        return
    sql_stripped = sql_text.strip()
    mem = st.session_state.setdefault("main_memory", {}).setdefault(chat_name, {"tables": [], "columns": {}, "recent_sql": []})
    if sql_stripped:
        mem["recent_sql"].append(sql_stripped)
        if len(mem["recent_sql"]) > MAX_RECENT_SQL:
            mem["recent_sql"] = mem["recent_sql"][-MAX_RECENT_SQL:]
    for m in _TABLE_FROM_SQL.finditer(sql_stripped):
        raw = m.group("table").strip()
        fqn = fully_qualify_if_needed(raw, st.session_state.get("context_values", {}))
        if fqn and not is_catalog_table(fqn):
            if fqn not in mem["tables"]:
                mem["tables"].append(fqn)

def _remember_columns(chat_name: str, table_fqn: str, cols: list[str]):
    if not table_fqn or not cols:
        return
    mem = st.session_state.setdefault("main_memory", {}).setdefault(chat_name, {"tables": [], "columns": {}, "recent_sql": []})
    existing = mem["columns"].get(table_fqn, [])
    if not existing:
        mem["columns"][table_fqn] = list(cols)
        return
    seen = set(existing)
    for c in cols:
        if c not in seen:
            existing.append(c); seen.add(c)
    mem["columns"][table_fqn] = existing

def build_context_preface_from_session(chat_name: str) -> str:
    mem = st.session_state.get("main_memory", {}).get(chat_name) or {"tables": [], "columns": {}, "recent_sql": []}
    tables = [t for t in mem.get("tables", []) if not is_catalog_table(t)]
    lines = []
    if tables:
        lines.append("Tables referenced in this chat:")
        for t in tables:
            lines.append(f"  - {t}")
    cols_map = mem.get("columns", {})
    if cols_map:
        lines.append("Columns seen/known (ordered) per table:")
        for t in tables:
            cols = cols_map.get(t, [])
            if cols:
                preview = ", ".join(cols[:80])
                lines.append(f"  - {t}: {preview}")
    recent = mem.get("recent_sql", [])
    if recent:
        lines.append("Recent SQLs:")
        for i, s in enumerate(recent[-MAX_RECENT_SQL:], 1):
            lines.append(f"  ({i}) {s}")
    if not lines:
        return ""
    return "Context for this follow-up (from the whole chat):\n" + "\n".join(lines) + "\n\n"

# Intent + reference resolvers
_same_table_patterns = re.compile(r'\b(same|above|that)\s+table\b', re.IGNORECASE)
_table_after_keyword = re.compile(r'\btable\s+([A-Za-z0-9_.$"]+)', re.IGNORECASE)
_fqn_like = re.compile(r'([A-Za-z0-9_"]+\.[A-Za-z0-9_"]+\.[A-Za-z0-9_"]+)|([A-Za-z0-9_"]+\.[A-Za-z0-9_"]+)', re.IGNORECASE)

_INTENT_COUNT_ROWS = "COUNT_ROWS"
_INTENT_LIST_COLUMNS = "LIST_COLUMNS"
_INTENT_DUP_FIRST_N = "DUP_FIRST_N"
_INTENT_DIFF_BETWEEN = "DIFF_BETWEEN"
_INTENT_WHERE_ERROR = "WHERE_ERROR"
_INTENT_WHY_ERROR = "WHY_ERROR"
_INTENT_UNKNOWN = "UNKNOWN"

def find_explicit_table_in_text(q: str) -> str | None:
    if not q: return None
    m = _fqn_like.search(q)
    if m: return m.group(0)
    m = _table_after_keyword.search(q)
    if m: return m.group(1)
    return None

def resolve_table_from_text(q: str, chat_name: str) -> str | None:
    t = find_explicit_table_in_text(q)
    if t:
        return fully_qualify_if_needed(t, st.session_state.get("context_values", {}))
    if _same_table_patterns.search(q or ""):
        mem = st.session_state.get("main_memory", {}).get(chat_name) or {}
        recent_sql = mem.get("recent_sql", [])
        if recent_sql:
            for sql in reversed(recent_sql):
                tt = parse_table_from_sql(sql)
                if tt and not is_catalog_table(tt):
                    return fully_qualify_if_needed(tt, st.session_state.get("context_values", {}))
        tables = [t for t in mem.get("tables", []) if not is_catalog_table(t)]
        if len(tables) == 1:
            return tables[0]
        if tables:
            return tables[-1]
    mem = st.session_state.get("main_memory", {}).get(chat_name) or {}
    tables = [t for t in mem.get("tables", []) if not is_catalog_table(t)]
    if len(tables) == 1:
        return tables[0]
    return None

def build_live_schema_hint(conn, q: str, chat_name: str) -> str | None:
    """Introspect the live schema of a table mentioned in the question and return
    a compact, authoritative hint string to ground SQL generation in real column
    names. Best-effort: returns None on any failure so generation still proceeds."""
    try:
        tbl = resolve_table_from_text(q, chat_name)
        if not tbl or is_catalog_table(tbl):
            return None
        cols = get_table_columns(conn, tbl)
        if not cols:
            return None
        preview = ", ".join(cols[:80])
        return f"Live schema (authoritative, use these exact column names):\n  - {tbl}: {preview}"
    except Exception:
        return None

def detect_main_intent(q: str) -> dict:
    p = (q or "").strip().lower()
    if re.search(r'(row\s*count|count\s+rows|number\s+of\s+rows|no\.\s*of\s*rows)', p):
        return {"type": _INTENT_COUNT_ROWS}
    if re.search(r'(what\s+are\s+the\s+columns|columns\s+present|list\s+columns|show\s+columns)', p):
        return {"type": _INTENT_LIST_COLUMNS}
    m = re.search(r'duplicates?\s+for\s+the\s+first\s+(\d+)\s+columns?', p)
    if m:
        try:
            return {"type": _INTENT_DUP_FIRST_N, "n": int(m.group(1))}
        except Exception:
            pass
    if re.search(r'(minus|except|difference\s+between|compare\s+tables?)', p):
        return {"type": _INTENT_DIFF_BETWEEN}
    if re.search(r'(where\s+is\s+the\s+error|show\s+error\s+rows|where\s+error)', p):
        return {"type": _INTENT_WHERE_ERROR}
    if re.search(r'(why\s+.*error|why\s+error|root\s+cause)', p):
        return {"type": _INTENT_WHY_ERROR}
    return {"type": _INTENT_UNKNOWN}


def build_sql_for_intent(intent: dict, table_fqn: str) -> tuple[str, list[str]]:
    """
    Deterministic builders for common intents. 
    - COUNT_ROWS
    - LIST_COLUMNS (via SELECT * LIMIT 0 on the user table)
    - DUP_FIRST_N (first N columns, using actual order via get_table_columns)
    """
    ctx = st.session_state.get("context_values", {}) or {}
    fq = fully_qualify_if_needed(table_fqn, ctx)
    render_tbl = render_table_for_sql(fq, prefer_minimal=True)

    # Count rows
    if intent["type"] == _INTENT_COUNT_ROWS:
        sql = f"SELECT COUNT(*) AS ROW_COUNT FROM {render_tbl};"
        return sql, []

    # List columns (derive from table headers)
    if intent["type"] == _INTENT_LIST_COLUMNS:
        sql = f"SELECT * FROM {render_tbl} LIMIT 0;"
        return sql, []

    # Duplicates for first N columns
    if intent["type"] == _INTENT_DUP_FIRST_N:
        n = max(1, int(intent.get("n") or 1))
        cols = get_table_columns(st.session_state["connector"].conn(), fq)
        if not cols:
            raise RuntimeError("Could not read columns for the table to build duplicate check.")
        use = cols[:n]
        group_cols = ", ".join(quote_ident(c) for c in use)
        sql = f"""
WITH first_col_counts AS (
    SELECT {group_cols}, COUNT(*) as cnt
    FROM {render_tbl}
    GROUP BY {group_cols}
)
SELECT COUNT(*) as duplicate_count
FROM first_col_counts
WHERE cnt > 1;
""".strip()
        return sql, use

    return "", []


# ---- Table rendering helpers (ensure minimal, no quotes) ----
def quote_ident(name: str) -> str:
    return str(name).strip().strip('"')

def quote_fqn(table: str) -> str:
    return ".".join(p.strip().strip('"') for p in str(table).split("."))

def render_table_for_sql(table_fqn: str, prefer_minimal: bool = True) -> str:
    """
    Render user tables exactly the way the user expects:
    - If table is in current DB/SCHEMA and prefer_minimal, render TABLE
    - Else render DB.SCHEMA.TABLE
    No double quotes are added.
    """
    if not table_fqn:
        return table_fqn
    parts = [p.strip().strip('"') for p in str(table_fqn).split(".")]
    ctx = st.session_state.get("context_values", {}) or {}
    db = ctx.get("database")
    sc = ctx.get("schema")
    if len(parts) == 3:
        t_db, t_sc, t_tbl = parts
        if prefer_minimal and db and sc and t_db == db and t_sc == sc:
            return t_tbl
        return f"{t_db}.{t_sc}.{t_tbl}"
    if len(parts) == 2:
        t_sc, t_tbl = parts
        # Prefer TABLE if schema matches current; otherwise coerce to DB.SCHEMA.TABLE when DB is known
        if prefer_minimal and sc and t_sc == sc:
            return t_tbl
        if db:
            return f"{db}.{t_sc}.{t_tbl}"
        # As a last resort, return TABLE (never emit SCHEMA.TABLE)
        return t_tbl

    return parts[0]


def _normalize_sql_identifiers(sql: str, ctx: dict) -> str:
    """
    Ensure identifiers are only 1-part (TABLE) or 3-part (DB.SCHEMA.TABLE).
    If a 2-part identifier appears, expand using current env:
      - If first part equals current DB → assume DB.TABLE and expand to DB.<SCHEMA>.TABLE
      - If first part equals current SCHEMA → assume SCHEMA.TABLE and expand to <DB>.SCHEMA.TABLE
      - Otherwise leave it unchanged (avoid over-correcting).
    This runs on FROM/JOIN/INTO/UPDATE/DELETE/TRUNCATE statements.
    """
    if not isinstance(sql, str) or not sql.strip():
        return sql
    db = (ctx or {}).get("database")
    sc = (ctx or {}).get("schema")
    if not db or not sc:
        return sql

    def repl(m):
        lead = m.group(1)
        ident = m.group(2)
        parts = [p.strip().strip('"') for p in ident.split('.') if p.strip()]
        if len(parts) == 2:
            a, b = parts
            A = a.upper()
            DB = db.upper()
            SC = sc.upper()
            if A == DB:
                return f"{lead} {db}.{sc}.{b}"
            if A == SC:
                return f"{lead} {db}.{a}.{b}"
        return f"{lead} {ident}"

    pattern = re.compile(r'(?i)\b(from|join|into|update|delete\s+from|truncate\s+table|table)\s+([A-Za-z0-9_\."]+)')
    out = pattern.sub(repl, sql)
    return out


def _minimize_current_db_schema(sql: str, ctx: dict) -> str:
    """Collapse DB.SCHEMA.TABLE to TABLE when DB+SCHEMA match the current context.
    This is only for **display** so the UI doesn't show the database unless the user did.
    """
    if not isinstance(sql, str) or not sql.strip():
        return sql
    db = (ctx or {}).get("database")
    sc = (ctx or {}).get("schema")
    if not db or not sc:
        return sql

    DB = db.upper()
    SC = sc.upper()

    def repl(m):
        lead = m.group(1)
        ident = m.group(2)
        parts = [p.strip().strip('"') for p in ident.split('.') if p.strip()]
        if len(parts) == 3:
            a, b, c = parts
            if a.upper() == DB and b.upper() == SC:
                return f"{lead} {c}"
        return f"{lead} {ident}"

    pattern = re.compile(r'(?i)\b(from|join|into|update|delete\s+from|truncate\s+table|table)\s+([A-Za-z0-9_\."]+)')
    out = pattern.sub(repl, sql)
    return out

st.set_page_config(page_title="Text-to-SQL for Snowflake", layout="wide")

# ---------- Small helpers ----------

def auto_height_for_text(text: str, min_h: int = 80, line_h: int = 24, max_h: int = 400) -> int:
    rows = max(1, (text or "").count("\n") + 1)
    return min(max_h, max(min_h, rows * line_h + 16))

def format_env_text(env: dict) -> str:
    return (
        f"Warehouse: {env.get('warehouse','')}\n"
        f"Database:  {env.get('database','')}\n"
        f"Schema:    {env.get('schema','')}"
    )

def _strip_sql_wrappers(s: str) -> str:
    """Remove ```sql fences so SQL renders correctly."""
    if not isinstance(s, str):
        return ""
    t = s.strip()
    if t.lower().startswith("```sql") and t.endswith("```"):
        return t[6:-3].strip()
    if t.startswith("```") and t.endswith("```"):
        return t[3:-3].strip()
    return t

# df_bool_to_text is imported from utils.helpers (see top of file).

# ---------- Session state ----------

def _init_state():
    ss = st.session_state
    ss.setdefault("route", "login")
    ss.setdefault("page", "context")
    # Options for Main page
    ss.setdefault("options", {
        "show_sql": True, "show_table": True, "show_summary": True, "show_followups": True
    })
    # Options for QA page (Detail -> Summary; has a Table toggle)
    ss.setdefault("qa_options", {
        "show_sql": True, "show_table": True, "show_summary": True, "show_status": True
    })

    ss.setdefault("connector", None)
    ss.setdefault("context_values", {"warehouse": "", "database": "", "schema": ""})
    ss.setdefault("contexts", [])

    # Main chat sessions
    ss.setdefault("chat_main", [])
    ss.setdefault("chat_main_sessions", {})
    ss.setdefault("current_main_session", "Chat 1")
    ss["chat_main_sessions"].setdefault(ss["current_main_session"], [])

    # QA sessions + persistent QA inputs
    ss.setdefault("chat_qa", [])
    ss.setdefault("chat_qa_sessions", {})
    ss.setdefault("current_qa_session", "QA 1")
    ss["chat_qa_sessions"].setdefault(ss["current_qa_session"], [])

    # Persisted QA input
    ss.setdefault("qa_input_mode", "Prompts")     # "Prompts" | "Document"
    ss.setdefault("qa_prompts_text", "")
    ss.setdefault("qa_doc_store", None)           # {"name": str, "data": bytes} or None
    ss.setdefault("qa_last_input_hash", "")

    
    # Difference (Diff) tabs
    ss.setdefault("diff_sessions", {})
    ss.setdefault("current_diff_session", "Tab 1")
    ss["diff_sessions"].setdefault(ss["current_diff_session"], {
        "target_is_sql": False,
        "target": "",
        "source_sql": ""
    })
    ss.setdefault("is_env_connected", False)
    ss.setdefault("main_memory", {})

def _connector_ok() -> bool:
    return st.session_state.get("connector") is not None

def _context_ok() -> bool:
    conn = st.session_state.get("connector")
    vals = st.session_state.get("context_values", {}) or {}
    filled = all(vals.get(k) for k in ("warehouse","database","schema"))
    return bool(conn and conn.has_context() and st.session_state.get("is_env_connected", False) and filled)

def _save_current_context():
    c = st.session_state.get("context_values", {})
    if not c or not all(k in c for k in ("warehouse","database","schema")):
        return
    lst = st.session_state.setdefault("contexts", [])
    # Replace first blank placeholder if present
    for i, ctx in enumerate(lst):
        if not ctx.get("warehouse") and not ctx.get("database") and not ctx.get("schema"):
            lst[i] = c.copy()
            return
    # Otherwise append if distinct
    if c not in lst:
        lst.append(c.copy())
    return

def _switch_main_session(name: str):
    st.session_state["current_main_session"] = name
    st.session_state["chat_main"] = st.session_state["chat_main_sessions"].get(name, []).copy()
    st.session_state.setdefault("main_memory", {}).setdefault(name, {"tables": [], "columns": {}, "recent_sql": []})
    st.rerun()


def _switch_diff_session(name: str):
    st.session_state["current_diff_session"] = name
    # load saved values into the page-level keys for a smooth UX
    conf = st.session_state.get("diff_sessions", {}).get(name, {})
    st.session_state["diff_target_is_sql"] = conf.get("target_is_sql", False)
    if st.session_state["diff_target_is_sql"]:
        st.session_state["diff_target_sql"] = conf.get("target", "")
    else:
        st.session_state["diff_target"] = conf.get("target", "")
    st.session_state["diff_source_sql"] = conf.get("source_sql", "")
    st.rerun()
def _switch_qa_session(name: str):
    st.session_state["current_qa_session"] = name
    st.session_state["chat_qa"] = st.session_state["chat_qa_sessions"].get(name, []).copy()
    st.rerun()

# ---------- Login ----------

def login_view():
    st.title("🔐 Login to Snowflake")
    with st.form("login_form", clear_on_submit=False):
        account = st.text_input("Account", placeholder="xy12345.region.azure")
        user = st.text_input("User")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login")
    if submitted:
        try:
            connector = SnowflakeConnector(account=account, user=user, password=password)
            connector.ping()
            st.session_state["connector"] = connector
            st.success("Login is Successful")
            st.balloons()
            st.session_state["route"] = "app"
            st.session_state["page"] = "context"
            st.rerun()
        except Exception as e:
            st.error(f"Login failed: {e}")


# ---------- Reusable safe dataframe display ----------

def _safe_display_df(df):
    """Robust wrapper to render a dataframe without crashing the Streamlit frontend.
    - Handles None, empty, and weird dtypes.
    - Resets index to avoid index objects confusing the renderer after reruns.
    """
    import pandas as _pd
    if df is None:
        st.info("No rows.")
        return
    if isinstance(df, _pd.DataFrame):
        try:
            _df = df.copy()
            # Avoid objects that sometimes break renderer on re-run
            _df = _df.reset_index(drop=True)
            st.dataframe(_df, use_container_width=True)
        except Exception:
            st.table(df)
    else:
        st.write(df)

# ---------- Top Nav + Options ----------

def top_nav():
    st.markdown("## 🚀 Text-to-SQL for Snowflake")
    col_env, col_main, col_qa, col_diff, col_opts = st.columns([1, 1, 1, 1, 1])

    def tab_btn(col, page_key, label):
        with col:
            is_active = st.session_state.get("page") == page_key
            icon_map = {"context":"🧭","main":"💬","qa":"🧪","diff":"🔎"}
            base = f"{icon_map.get(page_key, '')} {label}"
            btn_label = (f"**{base}**" if is_active else base)
            if st.button(btn_label, use_container_width=True):
                st.session_state["page"] = page_key
                st.rerun()

    tab_btn(col_env, "context", "**Environment**")
    tab_btn(col_main, "main", "**Main**")
    tab_btn(col_qa,   "qa",   "**QA**")  # bold look in button text
    tab_btn(col_diff, "diff",  "**The Differences**")

    with col_opts:
        if st.session_state.get("page") not in ("context", "diff"):
            with st.popover("⚙ Options"):
                if st.session_state.get("page") == "qa":
                    qa_opts = st.session_state.get("qa_options", {})
                    # migrate any old key
                    if "show_details" in qa_opts and "show_summary" not in qa_opts:
                        qa_opts["show_summary"] = qa_opts.pop("show_details")
                    qa_opts["show_sql"]     = st.toggle("Show SQL",     value=bool(qa_opts.get("show_sql", True)),     key="qa_opt_sql")
                    qa_opts["show_table"]   = st.toggle("Show Table",   value=bool(qa_opts.get("show_table", True)),   key="qa_opt_table")
                    qa_opts["show_summary"] = st.toggle("Show Summary", value=bool(qa_opts.get("show_summary", True)), key="qa_opt_summary")
                    qa_opts["show_status"]  = st.toggle("Show Status",  value=bool(qa_opts.get("show_status", True)),  key="qa_opt_status")
                    st.session_state["qa_options"] = qa_opts
                else:
                    opts = st.session_state.get("options", {})
                    opts["show_sql"]       = st.toggle("Show SQL",        value=bool(opts.get("show_sql", True)),        key="opt_sql_top")
                    opts["show_table"]     = st.toggle("Show Table",      value=bool(opts.get("show_table", True)),      key="opt_table_top")
                    opts["show_summary"]   = st.toggle("Show Summary",    value=bool(opts.get("show_summary", True)),    key="opt_summary_top")
                    opts["show_followups"] = st.toggle("Show Follow-ups", value=bool(opts.get("show_followups", True)),  key="opt_followups_top")
                    st.session_state["options"] = opts
        else:
            st.empty()
    st.markdown("---")

# ---------- Sidebar ----------

def sidebar_per_page():
    page = st.session_state["page"]
    with st.sidebar:
        if st.button("Logout", use_container_width=True):
            st.session_state.clear()
            st.session_state["route"] = "login"
            st.rerun()

        st.markdown("### 🧭 Navigation")
        page_label_map = {"context": "Environment", "main": "Chat", "qa": "QA", "diff": "Difference"}
        # page_emoji_map = {"context": "🧭", "main": "💬", "qa": "🧪", "diff": "🔎"}
        label_txt = page_label_map.get(page, page.title())
        if label_txt.upper()=="QA": label_txt = "QA"  # ensure all caps
        # st.write(f"**Page:** {page_emoji_map.get(page, )} {label_txt}")
        st.write(f"**Page:** {label_txt}")
        st.markdown("---")

        st.markdown("**Current Environment**")
        env_text = format_env_text(st.session_state.get("context_values", {}))
        st.text_area(" ", env_text, height=auto_height_for_text(env_text), disabled=True)

        if page == "context":
            # Heading first, then Add Environment
            st.markdown("### 📚 Environments")
            if st.button("➕ Add Environment", use_container_width=True):
                placeholder = {"warehouse": "", "database": "", "schema": ""}
                st.session_state.setdefault("contexts", []).append(placeholder)
                st.session_state["context_values"] = placeholder.copy()
                st.toast("New environment slot created — fill the form and click 'Set Environment / Connect'.", icon="🧭")

            # Numbered list
            for i, ctx in enumerate(st.session_state.get("contexts", []), start=1):
                cols = st.columns([4, 1])
                label = f"Environment {i}"
                with cols[0]:
                    if st.button(label, key=f"env_sel_{i}"):
                        st.session_state["context_values"] = ctx.copy()
                        st.toast("Environment loaded. Click 'Set Environment / Connect' to apply.", icon="✅")
                        st.rerun()
                with cols[1]:
                    if st.button("❌", key=f"env_del_{i}"):
                        st.session_state[f"confirm_del_env_{i}"] = True

                # Inline confirm (reverted from modal)
                if st.session_state.get(f"confirm_del_env_{i}"):
                    c1, c2 = st.columns([1, 1])
                    with c1:
                        if st.button("Yes", key=f"env_yes_{i}"):
                            del st.session_state["contexts"][i - 1]
                            st.session_state.pop(f"confirm_del_env_{i}", None)
                            st.toast("Environment deleted.", icon="🗑️")
                            st.rerun()
                    with c2:
                        if st.button("Cancel", key=f"env_cancel_{i}"):
                            st.session_state.pop(f"confirm_del_env_{i}", None)
                            st.rerun()

        elif page == "main":
            st.markdown("### 💬 Chats")
            if st.button("➕ Add Chat", use_container_width=True):
                i = 1
                while f"Chat {i}" in st.session_state["chat_main_sessions"]:
                    i += 1
                name = f"Chat {i}"
                st.session_state["chat_main_sessions"][name] = []
                st.session_state["current_main_session"] = name
                st.session_state["chat_main"] = []
                st.session_state["main_memory"][name] = {"tables": [], "columns": {}, "recent_sql": []}
                st.toast("New chat slot created — start asking questions!", icon="💬")
                st.rerun()
                st.toast("New chat slot created — start asking questions!", icon="💬")
                st.rerun()

            for name in list(st.session_state["chat_main_sessions"].keys()):
                cols = st.columns([4, 1])
                with cols[0]:
                    if st.button(name, key=f"chat_sel_{name}"):
                        _switch_main_session(name)
                with cols[1]:
                    if st.button("❌", key=f"chat_del_{name}"):
                        st.session_state[f"confirm_del_chat_{name}"] = True

                # Inline confirm (reverted from modal)
                if st.session_state.get(f"confirm_del_chat_{name}"):
                    c1, c2 = st.columns([1, 1])
                    with c1:
                        if st.button("Yes", key=f"chat_yes_{name}"):
                            del st.session_state["chat_main_sessions"][name]
                            if st.session_state.get("current_main_session") == name:
                                st.session_state["current_main_session"] = next(
                                    iter(st.session_state["chat_main_sessions"]), None
                                )
                                st.session_state["chat_main"] = st.session_state["chat_main_sessions"].get(
                                    st.session_state["current_main_session"], []
                                )
                            st.session_state.pop(f"confirm_del_chat_{name}", None)
                            st.toast("Chat deleted.", icon="🗑️")
                            st.rerun()
                    with c2:
                        if st.button("Cancel", key=f"chat_cancel_{name}"):
                            st.session_state.pop(f"confirm_del_chat_{name}", None)
                            st.rerun()

        elif page == "qa":
            st.markdown("### 🧪 Sessions")
            if st.button("➕ Add Sessions", use_container_width=True):
                base = "QA "
                idx = 1
                existing = set(st.session_state.get("chat_qa_sessions", {}).keys())
                while f"{base}{idx}" in existing:
                    idx += 1
                name = f"{base}{idx}"
                st.session_state["chat_qa_sessions"][name] = []
                st.session_state["current_qa_session"] = name
                st.session_state["chat_qa"] = []
                st.session_state["qa_prompts_text"] = ""
                st.session_state["qa_doc_store"] = None
                st.session_state["qa_last_input_hash"] = ""
                st.toast("New QA slot created — add prompts or upload a document, then click 'Run QA'.", icon="🧪")
                st.rerun()

            for name in list(st.session_state.get("chat_qa_sessions", {}).keys()):
                cols = st.columns([4, 1])
                with cols[0]:
                    if st.button(name, key=f"qa_sel_{name}"):
                        _switch_qa_session(name)
                with cols[1]:
                    if st.button("❌", key=f"qa_del_{name}"):
                        st.session_state[f"confirm_del_qa_{name}"] = True

                # Inline confirm (reverted from modal)
                if st.session_state.get(f"confirm_del_qa_{name}"):
                    c1, c2 = st.columns([1, 1])
                    with c1:
                        if st.button("Yes", key=f"qa_yes_{name}"):
                            del st.session_state["chat_qa_sessions"][name]
                            if st.session_state.get("current_qa_session") == name:
                                st.session_state["current_qa_session"] = next(
                                    iter(st.session_state["chat_qa_sessions"]), None
                                )
                                st.session_state["chat_qa"] = st.session_state["chat_qa_sessions"].get(
                                    st.session_state["current_qa_session"], []
                                )
                            st.session_state.pop(f"confirm_del_qa_{name}", None)
                            st.toast("QA session deleted.", icon="🧪")
                            st.rerun()
                    with c2:
                        if st.button("Cancel", key=f"qa_cancel_{name}"):
                            st.session_state.pop(f"confirm_del_qa_{name}", None)
                            st.rerun()
        elif page == "diff":
            st.markdown("### 🗂 Tabs")
            if st.button("➕ Add Tab", use_container_width=True):
                base = "Tab "
                idx_t = 1
                existing = set(st.session_state.get("diff_sessions", {}).keys())
                while f"{base}{idx_t}" in existing:
                    idx_t += 1
                name = f"{base}{idx_t}"
                st.session_state["diff_sessions"][name] = {"target_is_sql": False, "target": "", "source_sql": ""}
                st.session_state["current_diff_session"] = name
                st.toast("New diff tab created — enter inputs on the page.", icon="🗂️")
                st.rerun()

            for name in list(st.session_state.get("diff_sessions", {}).keys()):
                cols = st.columns([4, 1])
                with cols[0]:
                    if st.button(name, key=f"diff_sel_{name}"):
                        _switch_diff_session(name)
                with cols[1]:
                    if st.button("❌", key=f"diff_del_{name}"):
                        st.session_state[f"confirm_del_diff_{name}"] = True

                if st.session_state.get(f"confirm_del_diff_{name}"):
                    c1, c2 = st.columns([1, 1])
                    with c1:
                        if st.button("Yes", key=f"diff_yes_{name}"):
                            try:
                                del st.session_state["diff_sessions"][name]
                            except KeyError:
                                pass
                            if st.session_state.get("current_diff_session") == name:
                                st.session_state["current_diff_session"] = next(iter(st.session_state.get("diff_sessions", {})), None)
                            st.session_state.pop(f"confirm_del_diff_{name}", None)
                            st.toast("Tab deleted.", icon="🗑️")
                            st.rerun()
                    with c2:
                        if st.button("Cancel", key=f"diff_cancel_{name}"):
                            st.session_state.pop(f"confirm_del_diff_{name}", None)
                            st.rerun()
    # ---------- Environment Page ----------

def context_page():
    st.header("Set Environment / Connect")
    if not _connector_ok():
        st.warning("Please login first.")
        return

    cvals = st.session_state.get("context_values", {})
    with st.form("ctx_form", clear_on_submit=False):
        wh = st.text_input("Warehouse", value=cvals.get("warehouse", ""))
        db = st.text_input("Database",  value=cvals.get("database", ""))
        sc = st.text_input("Schema",    value=cvals.get("schema", ""))
        apply_btn = st.form_submit_button("Set Environment / Connect")

    if apply_btn:
        try:
            st.session_state["connector"].set_context(wh, db, sc)
            st.session_state["context_values"] = {"warehouse": wh, "database": db, "schema": sc}
            st.success("✅ Environment set.")
            st.balloons()
            try:
                st.snow()
            except Exception:
                pass
            st.toast("Successfully connected to Snowflake.", icon="🎉")
            _save_current_context()
            st.session_state["is_env_connected"] = True
            st.rerun()
        except Exception as e:
            st.error(f"Failed to set context: {e}")

# ---------- Main Page ----------

def _render_main_block(question: str, sql: str, df: pd.DataFrame, summary: str, followups: list):
    with st.container():
        st.markdown(f"**User:** {question}")

        opts = st.session_state.get("options", {})
        if opts.get("show_sql", True) and sql:
            st.caption("SQL")
            st.code(_strip_sql_wrappers(sql), language="sql")

        if opts.get("show_table", True) and isinstance(df, pd.DataFrame):
            st.dataframe(df_bool_to_text(df), use_container_width=True, hide_index=True)

        if opts.get("show_summary", True) and isinstance(summary, str):
            st.caption("Summary")
            st.write(summary)

        if opts.get("show_followups", True) and followups:
            st.caption("Follow-ups")
            for f in followups:
                st.write("• " + f)

        st.divider()


def _set_last_diff_context(left, right, key_cols=None, compare_cols=None, cols=None):
    st.session_state["last_diff_context"] = {"left": left, "right": right, "key_cols": key_cols or [], "compare_cols": compare_cols or [], "cols": cols or []}
def _get_last_diff_context():
    return st.session_state.get("last_diff_context")
def main_page():
    st.header("Main Chat")
    if not _connector_ok():
        st.warning("Please login and set context.")
    elif not _context_ok():
        st.info("Set environment in the Environment page.")

    # Show previous chat runs
    for item in st.session_state["chat_main"]:
        _render_main_block(
            item.get("q", ""),
            item.get("sql", ""),
            item.get("df"),
            item.get("summary", ""),
            item.get("followups", []),
        )

    # Hide chat input if environment not set
    q = None
    if _context_ok():
        q = st.chat_input("Ask a question about your Snowflake data…")
        
    if not q:
        return  # nothing typed

    if not _context_ok():
        st.error("Please set your Environment first (Warehouse, Database, Schema) on the Environment tab.")
        st.stop()

    chat_name = st.session_state.get("current_main_session", "Chat 1")

    # -------------------------------
    # Data-diff handler (tables/data MINUS) — runs BEFORE text→SQL flow
    # -------------------------------
    if detect_table_column_data_diff_intent(q):
        try:
            conn = st.session_state.get("connector").conn() if "connector" in st.session_state else None
            if conn is None:
                st.error("No active Snowflake connection. Set context on the Environment page first.")
            else:
                # NEW: show the user's prompt just like other chat entries
                st.markdown(f"**User:** {q}")

                result = diff_tools.diff_tables_from_prompt_data(conn, q)
                # # persist EXACT output so the Differences page can mirror it
                # st.session_state['prefill_diff_from_chat'] = result
                st.subheader("Data Difference (MINUS)")

                st.write(f"**A:** `{result['table_a']}`  |  **B:** `{result['table_b']}`")
                if result.get("excluded"):
                    st.write(f"**Excluded columns:** {', '.join(result['excluded'])}")
                st.write(f"**Columns compared ({len(result['columns_compared'])}):** {', '.join(result['columns_compared'])}")
                st.markdown(f"**Verdict:** {result['message']}")

                
                with st.expander("Queries (display style vs executed)"):
                    st.code("-- Displayed in UI\n" + result['queries']['display_a_minus_b'])
                    st.code(result['queries']['display_b_minus_a'])
                    # Include UNION view query as well
                    st.code("-- Union (A ∪ B) displayed in UI\n" + result['queries'].get('union_display',''))
                    if not result['order_aligned']:
                        st.code("-- Executed for correctness (aligned column order)\n" + result['queries']['executed_a_minus_b'])
                        st.code(result['queries']['executed_b_minus_a'])
            
            tab1, tab2, tab3, tab4 = st.tabs([
                "A MINUS B",
                "B MINUS A",
                "Per-column value differences",
                "Union (A∪B differences)"
            ])

            with tab1:
                _safe_display_df(result['frames']['a_minus_b'])
                if not result['frames']['a_minus_b'].empty:
                    csv1 = result['frames']['a_minus_b'].to_csv(index=False).encode("utf-8")
            with tab2:
                _safe_display_df(result['frames']['b_minus_a'])
                if not result['frames']['b_minus_a'].empty:
                    csv2 = result['frames']['b_minus_a'].to_csv(index=False).encode("utf-8")
            with tab3:
                _safe_display_df(result['frames']['column_diffs'])
                if not result['frames']['column_diffs'].empty:
                    csv3 = result['frames']['column_diffs'].to_csv(index=False).encode("utf-8")
            with tab4:
                _safe_display_df(result['frames']['union'])
                with st.expander("Union query"):
                    st.code(result['queries']['union_display'])
                if not result['frames']['union'].empty:
                    csv4 = result['frames']['union'].to_csv(index=False).encode("utf-8")
    # NEW: save to chat history so it renders in the feed like other runs
                combined_sql = (
                    "-- Displayed in UI\n" + result['queries']['display_a_minus_b'] +
                    "\n-- and --\n" + result['queries']['display_b_minus_a']
                )
                entry = {
                    "q": q,
                    "sql": combined_sql,
                    # store A−B result in history (you can swap to B−A or concatenate if you prefer)
                    "df": result['frames']['a_minus_b'],
                    "summary": result['message'],
                    "followups": []
                }
                st.session_state["chat_main"].append(entry)
                name = st.session_state.get("current_main_session", "Chat 1")
                st.session_state["chat_main_sessions"][name] = st.session_state["chat_main"]
                _remember_sql(name, combined_sql)

        except Exception as e:
            st.error(f"Could not compute data diff: {e}")

        # stop the rest of main_page after handling diff
        st.stop()


    # -------------------------------
    # Your existing text→SQL flow
    # -------------------------------
    intent = detect_main_intent(q)
    used_table = None

    # Initialize in case we stop early
    sql_plain = None
    sql_exec = None
    df = None
    summary = None

    if intent.get("type") != _INTENT_UNKNOWN:
        used_table = resolve_table_from_text(q, chat_name)
        if not used_table and intent["type"] in (_INTENT_COUNT_ROWS, _INTENT_LIST_COLUMNS, _INTENT_DUP_FIRST_N):
            st.error("I couldn't identify the table for this request. Please mention it once.")
            st.stop()
        sql_plain, used_cols = build_sql_for_intent(intent, used_table)
        sql_exec = sql_plain
    else:
        preface = build_context_preface_from_session(chat_name)
        _conn = st.session_state["connector"].conn()
        schema_hint = build_live_schema_hint(_conn, q, chat_name)
        sql_plain = generate_sql_from_question(_conn, preface + q, schema_hint=schema_hint)
        sql_exec = sql_plain

    
    # Normalize identifiers for display too, then minimize DB+SCHEMA when targeting current env
    ctx_display = st.session_state.get("context_values", {})
    sql_plain = _minimize_current_db_schema(_normalize_sql_identifiers(sql_plain, ctx_display), ctx_display)
# (Legacy) extra branches you already have for diff/why/where-error — keep as-is
    if intent.get("type") == _INTENT_DIFF_BETWEEN:
        left = st.text_input("Left table (schema.table)", key="diff_left")
        right = st.text_input("Right table (schema.table)", key="diff_right")
        cols = st.text_input("Columns to compare (comma separated, optional)", key="diff_cols")
        if left and right:
            cols_list = [c.strip() for c in cols.split(",")] if cols else None
            try:
                d1, d2, s1, s2 = diff_minus(st.session_state["connector"].conn(), left, right, cols_list)
                df = d1
                sql_plain = s1 + "\n-- and --\n" + s2
                summary = f"Rows in LEFT not in RIGHT: {len(d1)}; RIGHT not in LEFT: {len(d2)}"
                _set_last_diff_context(left, right, cols=cols_list or [])
            except Exception as e:
                df = None
                summary = f"Error: {e}"
    elif intent.get("type") == _INTENT_WHERE_ERROR:
        ctxd = _get_last_diff_context()
        if not ctxd:
            summary = "Run a comparison first (MINUS/EXCEPT) or set left/right tables."
            df = None
        else:
            try:
                d1, d2, s1, s2 = diff_minus(st.session_state["connector"].conn(), ctxd["left"], ctxd["right"], ctxd.get("cols"))
                df = d1
                sql_plain = s1 + "\n-- and --\n" + s2
                summary = f"Showing LEFT minus RIGHT sample; also found {len(d2)} rows in RIGHT minus LEFT."
            except Exception as e:
                df = None
                summary = f"Error: {e}"
    elif intent.get("type") == _INTENT_WHY_ERROR:
        ctxd = _get_last_diff_context()
        if not ctxd:
            summary = "Provide left/right tables, key columns, and compare columns."
            df = None
        else:
            keys = st.text_input("Key columns (comma separated)", key="diff_keys")
            comps = st.text_input("Compare columns (comma separated)", key="diff_comps")
            tol = st.number_input("Numeric tolerance", min_value=0.0, value=0.0, step=0.01, key="diff_tol")
            if keys and comps is not None:
                key_cols = [c.strip() for c in keys.split(",") if c.strip()]
                compare_cols = [c.strip() for c in comps.split(",") if c.strip()]
                try:
                    rep, sqlx = explain_diff(st.session_state["connector"].conn(), ctxd["left"], ctxd["right"], key_cols, compare_cols, tolerance=tol)
                    df = rep
                    sql_plain = sqlx
                    summary = suggestions_from_explain(rep)
                    _set_last_diff_context(ctxd["left"], ctxd["right"], key_cols=key_cols, compare_cols=compare_cols, cols=ctxd.get("cols", []))
                except Exception as e:
                    df = None
                    summary = f"Error: {e}"

    # If legacy branches didn't set df/summary, run the generated SQL
    if df is None:
        try:
            # normalize any 2-part identifiers before executing
            sql_exec = _normalize_sql_identifiers(sql_exec, st.session_state.get("context_values", {}))
            df = run_query(st.session_state["connector"].conn(), sql_exec)
            summary = summarize_df(df)
        except Exception as e:
            df = None
            summary = f"Error: {e}"
            
    followups = generate_followups(st.session_state["connector"].conn(), q)

    entry = {"q": q, "sql": sql_plain, "df": df, "summary": summary, "followups": followups}
    st.session_state["chat_main"].append(entry)
    name = st.session_state["current_main_session"]
    st.session_state["chat_main_sessions"][name] = st.session_state["chat_main"]

    # Remember session-wide
    _remember_sql(chat_name, sql_plain)
    if used_table and isinstance(df, pd.DataFrame):
        # derive columns from df or catalog
        cols = list(map(str, df.columns.tolist())) if (df is not None and not df.empty) else get_table_columns(st.session_state["connector"].conn(), used_table)
        if cols:
            _remember_columns(chat_name, used_table, cols)

    st.rerun()


# ---------- QA Page (multi-prompts or document, persistent) ----------

def _extract_prompts_from_text(text: str) -> list[str]:
    lines = [ln.strip() for ln in (text or "").split("\n")]
    prompts = [ln for ln in lines if ln]
    # Strip "1. something" style numbering
    cleaned = []
    for ln in prompts:
        if re.match(r"^\d+\.\s+", ln):
            cleaned.append(re.sub(r"^\d+\.\s+", "", ln).strip())
        else:
            cleaned.append(ln)
    return cleaned

def _extract_prompts_from_file(uploaded_or_buffer) -> list[str]:
    if uploaded_or_buffer is None:
        return []
    name = getattr(uploaded_or_buffer, "name", "") or ""
    low = name.lower()
    try:
        if low.endswith(".txt") or low.endswith(".md"):
            data = uploaded_or_buffer.read()
            if isinstance(data, bytes):  # file_uploader
                txt = data.decode("utf-8", errors="ignore")
            else:
                txt = data
            return _extract_prompts_from_text(txt)
        elif low.endswith(".csv"):
            df = pd.read_csv(uploaded_or_buffer)
            for col in ["prompt", "test", "case", "query"]:
                if col in df.columns:
                    return [str(x).strip() for x in df[col].dropna().tolist() if str(x).strip()]
            if df.shape[1] >= 1:
                return [str(x).strip() for x in df.iloc[:, 0].dropna().tolist() if str(x).strip()]
        else:
            data = uploaded_or_buffer.read()
            if isinstance(data, bytes):
                txt = data.decode("utf-8", errors="ignore")
            else:
                txt = data
            return _extract_prompts_from_text(txt)
    except Exception as e:
        st.error(f"Could not read file: {e}")
    return []

def qa_page():
    st.header("QA Automation")
    if not _connector_ok():
        st.warning("Please login and set environment first.")
        return
    if not _context_ok():
        st.info("Set environment in the Environment page.")
        return

    # Persisted mode selection
    mode = st.session_state.get("qa_input_mode", "Prompts")
    mode = st.radio("Input source", ["Prompts", "Document"], horizontal=True,
                    index=0 if mode == "Prompts" else 1)
    st.session_state["qa_input_mode"] = mode

    prompts = []
    with st.form("qa_form_new", clear_on_submit=False):
        if mode == "Prompts":
            default_text = st.session_state.get("qa_prompts_text", "")
            multi = st.text_area(
                "Enter multiple prompts (one per line)",
                height=150,
                value=default_text,
                key="qa_prompts_textarea",
                placeholder="1. Row count > 0\\n2. No all-null columns\\n3. Primary key unique ..."
            )
            st.session_state["qa_prompts_text"] = multi
            prompts = _extract_prompts_from_text(multi)

        else:  # Document
            doc_info = st.session_state.get("qa_doc_store")
            uploaded = st.file_uploader("Upload a document (.txt, .md, .csv)",
                                        type=["txt", "md", "csv"], key="qa_doc")
            if uploaded is not None:
                st.session_state["qa_doc_store"] = {"name": uploaded.name, "data": uploaded.getvalue()}
                doc_info = st.session_state["qa_doc_store"]

            if doc_info:
                st.success(f"Using document: {doc_info['name']}")
                buf = io.BytesIO(doc_info["data"])
                buf.name = doc_info["name"]
                prompts = _extract_prompts_from_file(buf)

        submitted = st.form_submit_button("Run QA")

    # Reset runs when input changed
    input_key = {
        "mode": mode,
        "prompts": prompts,
        "doc": st.session_state.get("qa_doc_store")
    }
    cur_hash = hashlib.sha256(json.dumps(input_key, sort_keys=True, default=str).encode()).hexdigest()
    if cur_hash != st.session_state.get("qa_last_input_hash"):
        st.session_state["chat_qa"] = []
        st.session_state["qa_last_input_hash"] = cur_hash

    if submitted:
        if not prompts:
            st.warning("Please enter at least one prompt or upload a document with prompts.")
        else:
            # Delegate to qa_tests
            records, _ = run_custom_tests(st.session_state["connector"].conn(), prompts)
            # Build run bundle
            total = len(records)
            passed = sum(1 for r in records if r["status"] == "PASS")
            failed = sum(1 for r in records if r["status"] == "FAIL")
            errored = sum(1 for r in records if r["status"] == "ERROR")
            df_summary = pd.DataFrame([{
                "test": r["test"], "status": r["status"], "details": r["details"], "sql": r["sql"]
            } for r in records])
            run = {
                "table": "",
                "df": df_summary,
                "note": f"{total} tests • {passed} passed • {failed} failed • {errored} error",
                "counts": {"total": total, "passed": passed, "failed": failed, "error": errored},
                "records": records,
            }
            st.session_state["chat_qa"].append(run)
            active = st.session_state.get("current_qa_session", "QA 1")
            st.session_state["chat_qa_sessions"][active] = st.session_state["chat_qa"]
            st.success("QA run completed.")
            st.rerun()

    # Options (QA-only)
    qa_opts = st.session_state.get("qa_options", {
        "show_sql": True, "show_table": True, "show_summary": True, "show_status": True
    })

    # Render previous runs
    for run in st.session_state["chat_qa"]:
        with st.container():
            st.markdown("**QA Run**")
            tabs = st.tabs(["Results", "Summary"])

            with tabs[0]:
                records = run.get("records", [])
                if records:
                    for i, rec in enumerate(records, start=1):
                        name = rec["test"]
                        status = rec["status"]
                        details = rec["details"]
                        sql = rec["sql"]
                        dfp = rec.get("df")

                        st.markdown(f"**{i}. {name}**")

                        if qa_opts.get("show_sql", True) and sql:
                            st.caption("SQL")
                            st.code(_strip_sql_wrappers(sql), language="sql")

                        if qa_opts.get("show_table", True) and isinstance(dfp, pd.DataFrame) and not dfp.empty:
                            st.caption("Table")
                            st.dataframe(df_bool_to_text(dfp.head(50)), use_container_width=True, hide_index=True)

                        if qa_opts.get("show_summary", True) and details:
                            st.caption("Summary")
                            st.write(details)

                        if qa_opts.get("show_status", True):
                            st.caption("STATUS")
                            st.markdown(f"**{status}**")

                        st.markdown("---")

            with tabs[1]:
                counts = run.get("counts", {})
                total = counts.get("total", 0)
                c1, c2, c3, c4 = st.columns([1, 1, 1, 1])
                c1.metric("Total tests", total)
                c2.metric("Passed", counts.get("passed", 0))
                c3.metric("Failed", counts.get("failed", 0))
                c4.metric("Error", counts.get("error", 0))

                dfr = run.get("df")
                if dfr is not None and not dfr.empty:
                    def _status_of(row):
                        try:
                            return str(getattr(row, "status")).upper()
                        except Exception:
                            try:
                                return str(row["status"]).upper()
                            except Exception:
                                return ""
                    passed_idx = [str(i+1) for i, r in enumerate(dfr.itertuples(index=False)) if _status_of(r) == "PASS"]
                    failed_idx = [str(i+1) for i, r in enumerate(dfr.itertuples(index=False)) if _status_of(r) == "FAIL"]
                    error_idx  = [str(i+1) for i, r in enumerate(dfr.itertuples(index=False)) if _status_of(r) == "ERROR"]

                    if passed_idx:
                        st.write("**Passed:** " + ", ".join(passed_idx))
                    if failed_idx:
                        st.write("**Failed:** " + ", ".join(failed_idx))
                    if error_idx:
                        st.write("**Error:** " + ", ".join(error_idx))
        st.divider()


# ---------- The Differences Page ----------


def diff_page():
    st.header("🔎 The Differences")
    if not _connector_ok():
        st.warning("Please login and set context.")
        return
    if not _context_ok():
        st.info("Set environment in the Environment page.")
        return

    
    # Show the last result run on this page (persists on this page only)
    _dp = _recall_diff_page_store()
    if _dp:
        st.markdown("### The Differences (last run here)")
        st.caption(f"A: `{_dp.get('A','')}`  |  B: `{_dp.get('B','')}`")
        cols = _dp.get('columns_compared') or []
        if cols:
            st.caption("Columns compared: " + ", ".join(map(str, cols)))
        verdict = _dp.get('verdict')
        st.write("**Verdict:** " + ("Differences found ❌" if verdict else "No differences ✅"))
        with st.expander("Queries (display style vs executed)", expanded=False):
            for name, sql in (_dp.get('queries') or {}).items():
                st.code(sql or "", language="sql")
        tabs = st.tabs(["A MINUS B", "B MINUS A", "Per-column value differences", "Union (A∪B differences)"])
        with tabs[0]: _safe_display_df(_dp.get("A_MINUS_B"))
        with tabs[1]: _safe_display_df(_dp.get("B_MINUS_A"))
        with tabs[2]: _safe_display_df(_dp.get("PER_COL"))
        with tabs[3]: _safe_display_df(_dp.get("UNION"))
        st.divider()
    
    ctx = st.session_state.get("context_values", {}) or {}

    
    # Toggle to accept either TABLE name or full SQL query for the target
    target_is_sql = st.toggle(
        "Target is a SQL query",
        value=st.session_state.get("diff_target_is_sql", False),
        help="ON: enter a full SQL for the target; OFF: provide only a table name"
    )
    st.session_state["diff_target_is_sql"] = target_is_sql

    if target_is_sql:
        target = st.text_area(
            "Target table query",
            key="diff_target_sql",
            placeholder="e.g. SELECT * FROM MYDB.PUBLIC.CUSTOMERS WHERE COUNTRY = 'IN'",
            height=140
        )
    else:
        target = st.text_input(
            "Target table (TABLE or DB.SCHEMA.TABLE)",
            key="diff_target",
            placeholder="MYDB.PUBLIC.CUSTOMERS or CUSTOMERS"
        )

    source_sql = st.text_area("Source table SQL (SELECT ...)", height=180, key="diff_source_sql",
                              placeholder="SELECT ... FROM ... WHERE ...")

    run = st.button("Run Difference Check", type="primary", use_container_width=True)
    st.divider()

    if not run:
        return

    if not target or not source_sql.strip().lower().startswith("select"):
        st.error("Please provide a target (table or query) and a valid SELECT query for Source.")
        return

    # Normalize target for later use
    if target_is_sql:
        target_q = f"({target.strip().rstrip(';')})"
    else:
        try:
            target_q = fully_qualify_if_needed(target, ctx)
        except Exception:
            target_q = target  # fallback; diff_tools will still reject schema.table

    try:
        conn = st.session_state.get("connector").conn()
    except Exception as e:
        st.error(f"No active connection: {e}")
        return

    # Helpers to fetch column lists
    def _columns_from_query(conn, select_sql: str):
        q = f"""SELECT * FROM ({select_sql}) WHERE 1=0"""
        cur = conn.cursor()
        try:
            cur.execute(q)
            cols = [c[0] for c in (cur.description or [])]
        finally:
            cur.close()
        return cols

    try:
        if target_is_sql:
            cols_tgt = _columns_from_query(conn, target.strip().rstrip(';'))
        else:
            cols_tgt = diff_tools._describe_columns(conn, target_q)
        cols_src = _columns_from_query(conn, source_sql)
    except Exception as e:
        st.error(f"Could not inspect columns: {e}")
        return

    # Choose common columns preserving target order
    common = [c for c in cols_tgt if c in cols_src]
    if not common:
        st.error("No common columns between target and source.")
        return

    col_list = ", ".join([f'"{c}"' if not str(c).isidentifier() else str(c) for c in common])

    sql_src_minus_tgt = f"""\nWITH SRC AS ({source_sql})\nSELECT {col_list} FROM SRC\nMINUS\nSELECT {col_list} FROM {target_q}\n"""
    sql_tgt_minus_src = f"""\nWITH SRC AS ({source_sql})\nSELECT {col_list} FROM {target_q}\nMINUS\nSELECT {col_list} FROM SRC\n"""

    try:
        df_src_minus_tgt = diff_tools._run_df(conn, sql_src_minus_tgt)
        df_tgt_minus_src = diff_tools._run_df(conn, sql_tgt_minus_src)
    except Exception as e:
        st.error(f"Failed to run MINUS queries: {e}")
        return

    # Status
    has_diffs = (len(df_src_minus_tgt) > 0) or (len(df_tgt_minus_src) > 0)
    if not has_diffs:
        st.success("No differences detected ✅ (both MINUS queries returned zero rows)")
    else:
        st.warning("Differences found. Review the tabs below.")

    # Build union
    import pandas as pd
    src_tagged = df_src_minus_tgt.copy()
    if not src_tagged.empty:
        src_tagged["SIDE"] = "SOURCE_MINUS_TARGET"
    tgt_tagged = df_tgt_minus_src.copy()
    if not tgt_tagged.empty:
        tgt_tagged["SIDE"] = "TARGET_MINUS_SOURCE"
    
    # # Persist the result for this page
    # _remember_diff_page_store({
    #     'A': src_table if 'src_table' in locals() else None,
    #     'B': tgt_table if 'tgt_table' in locals() else None,
    #     'table_a': src_table if 'src_table' in locals() else None,
    #     'table_b': tgt_table if 'tgt_table' in locals() else None,
    #     'columns_compared': common,
    #     'verdict': bool(has_diffs),
    #     'summary': '',
    #     'queries': {
    #         'SRC_MINUS_TGT': sql_src_minus_tgt,
    #         'TGT_MINUS_SRC': sql_tgt_minus_src,
    #         'UNION': sql_union
    #     },
    #     'SRC_MINUS_TGT': df_src_minus_tgt,
    #     'TGT_MINUS_SRC': df_tgt_minus_src,
    #     'UNION_DF': union_df
    # })
    
    union_df = pd.concat([src_tagged, tgt_tagged], ignore_index=True) if has_diffs else pd.DataFrame(columns=common + ["SIDE"])

    tabs = st.tabs(["Overview", "Source − Target", "Target − Source", "Union", "Reason (RCA)", "SQL Used"])

    # Overview (reverted style)
    with tabs[0]:
        # Target label
        st.markdown("**Target:**  <code>%s</code>" % target_q, unsafe_allow_html=True)
        # Common columns with count
        st.markdown("**Common columns (%d):** %s" % (len(common), ', '.join(common)))
        # Source query columns detected
        st.markdown("**Source query provided:** %d columns detected" % len(cols_src))
        # Big counters
        st.metric("Rows in Source − Target", len(df_src_minus_tgt))
        st.metric("Rows in Target − Source", len(df_tgt_minus_src))

    # Source − Target
    with tabs[1]:
        st.dataframe(df_src_minus_tgt, use_container_width=True)

    # Target − Source
    with tabs[2]:
        st.dataframe(df_tgt_minus_src, use_container_width=True)

    # Union
    with tabs[3]:
        st.dataframe(union_df, use_container_width=True)

    with tabs[4]:
        # Simple RCA: per-column distinct counts in each side
        if not has_diffs:
            st.info("No differences to analyze.")
        else:
            rows = []
            for col in common:
                a_vals = set(df_src_minus_tgt[col].dropna().head(20).astype(str).tolist()) if col in df_src_minus_tgt.columns else set()
                b_vals = set(df_tgt_minus_src[col].dropna().head(20).astype(str).tolist()) if col in df_tgt_minus_src.columns else set()
                only_a = a_vals - b_vals
                only_b = b_vals - a_vals
                reason = "No difference in this column among differing rows"
                if only_a or only_b:
                    reason = "Value sets differ within the differing rows"
                rows.append({"column": col, "sample_values_in_source_only": list(sorted(only_a))[:10], "sample_values_in_target_only": list(sorted(only_b))[:10], "reason": reason})
            rca_df = pd.DataFrame(rows, columns=["column", "sample_values_in_source_only", "sample_values_in_target_only", "reason"])            
            st.dataframe(rca_df)

    with tabs[5]:
        st.code(sql_src_minus_tgt.strip(), language="sql")
        st.code(sql_tgt_minus_src.strip(), language="sql")
# ---------- Router ----------

def _init_connector_guard():
    # Ensures connector object has expected API (has_context, conn, etc.)
    pass

_init_state()
if st.session_state["route"] == "login":
    login_view()
else:
    top_nav()
    sidebar_per_page()
    page = st.session_state["page"]
    if page == "context":
        context_page()
    elif page == "main":
        main_page()
    elif page == "qa":
        qa_page()
    else:
        diff_page()