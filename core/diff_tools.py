
# core/diff_tools.py
from __future__ import annotations
import re
from typing import Dict, List, Optional, Tuple
import pandas as pd

# =====================================================
# Data-diff based on MINUS, honoring user's identifier style
# - Table identifiers: ONLY "table" or "db.schema.table" are allowed.
#   "schema.table" (2-part) is INVALID by requirement.
# - Display queries prefer `SELECT *` and `SELECT * EXCLUDE (...)` exactly as the user wants.
# - Execution uses aligned explicit column lists when necessary to guarantee column order equality,
#   but we do NOT show quotes or auto-qualify with CURRENT_DB/SCHEMA in the displayed queries.
# =====================================================

def diff_tables_from_prompt_data(conn, prompt_text: str, *, sample_values_per_col: int = 10):
    """
    Parse NL prompt → two tables (+ optional excludes), run symmetric MINUS, and
    return frames for A−B, B−A, per-column value diffs (all columns), and a union view.
    """
    t1, t2, excludes = _parse_data_diff_prompt(prompt_text)
    if not (t1 and t2):
        raise ValueError("Could not parse two table names. Try: difference between <A> and <B>.")
    if _is_schema_table(t1) or _is_schema_table(t2):
        raise ValueError("Identifiers like 'SCHEMA.TABLE' are not allowed. Use 'TABLE' or 'DB.SCHEMA.TABLE'.")

    cols_a = _describe_columns(conn, t1)
    cols_b = _describe_columns(conn, t2)

    excludes_norm = [c.strip() for c in (excludes or []) if c.strip()]
    excludes_upper = {c.upper() for c in excludes_norm}

    a_after = [c for c in cols_a if c.upper() not in excludes_upper]
    b_after = [c for c in cols_b if c.upper() not in excludes_upper]
    common = [c for c in a_after if c in b_after]

    if not common:
        raise ValueError("No common columns to compare after exclusions.")

    order_aligned = (a_after == b_after)
    if excludes_norm:
        disp_a = f'SELECT * EXCLUDE ({", ".join(excludes_norm)}) FROM {t1}'
        disp_b = f'SELECT * EXCLUDE ({", ".join(excludes_norm)}) FROM {t2}'
    else:
        disp_a = f'SELECT * FROM {t1}'
        disp_b = f'SELECT * FROM {t2}'

    display_a_minus_b = f"{disp_a}\nMINUS\n{disp_b}"
    display_b_minus_a = f"{disp_b}\nMINUS\n{disp_a}"


    if order_aligned:
        exec_a_minus_b = display_a_minus_b
        exec_b_minus_a = display_b_minus_a
    else:
        select_list = ", ".join(common)
        exec_a_minus_b = f"SELECT {select_list} FROM {t1}\nMINUS\nSELECT {select_list} FROM {t2}"
        exec_b_minus_a = f"SELECT {select_list} FROM {t2}\nMINUS\nSELECT {select_list} FROM {t1}"


    df_a_minus_b = _run_df(conn, exec_a_minus_b)
    df_b_minus_a = _run_df(conn, exec_b_minus_a)

    verdict = "OK" if (df_a_minus_b.empty and df_b_minus_a.empty) else "DIFF"
    message = "No differences detected ✅" if verdict == "OK" else "Differences found ❌"

    
    # --- Build per-column value differences focused on only the differing rows ---
    
    # --- Build per-column value differences focused on only the differing rows ---
    rows = []
    # Sets of values in the differing rows only
    for col in common:
        a_vals = set([str(x) for x in df_a_minus_b[col].dropna().unique().tolist()]) if col in df_a_minus_b.columns else set()
        b_vals = set([str(x) for x in df_b_minus_a[col].dropna().unique().tolist()]) if col in df_b_minus_a.columns else set()

        only_a_vals = sorted(list(a_vals - b_vals))[:sample_values_per_col]
        only_b_vals = sorted(list(b_vals - a_vals))[:sample_values_per_col]

        cnt_a = len(a_vals - b_vals)
        cnt_b = len(b_vals - a_vals)

        if cnt_a and not cnt_b:
            reason = f"Values appear only when rows are in {t1} but not in {t2}"
        elif cnt_b and not cnt_a:
            reason = f"Values appear only when rows are in {t2} but not in {t1}"
        elif cnt_a and cnt_b:
            probe = (only_a_vals[:1] or only_b_vals[:1])
            numish = any(_looks_number(s) for s in probe) if probe else False
            reason = "Numeric/value mismatch in differing rows" if numish else "Value sets differ within the differing rows"
        else:
            reason = "No difference in this column among differing rows"

        rows.append({
            "column": col,
            "only_in_A_distinct_count": int(cnt_a),
            "sample_values_in_A_only": only_a_vals,
            "only_in_B_distinct_count": int(cnt_b),
            "sample_values_in_B_only": only_b_vals,
            "reason": reason
        })
    import pandas as pd
    col_diff_df = pd.DataFrame(rows, columns=[
        "column",
        "only_in_A_distinct_count",
        "sample_values_in_A_only",
        "only_in_B_distinct_count",
        "sample_values_in_B_only",
        "reason"
    ])

    df_a_labeled = df_a_minus_b.copy()
    if not df_a_labeled.empty:
        df_a_labeled.insert(0, "DIFFERENCE_TYPE", f"In {t1} but not in {t2}")
    df_b_labeled = df_b_minus_a.copy()
    if not df_b_labeled.empty:
        df_b_labeled.insert(0, "DIFFERENCE_TYPE", f"In {t2} but not in {t1}")
    union_df = pd.concat([df_a_labeled, df_b_labeled], ignore_index=True)

    if order_aligned:
        union_display_sql = (
            "WITH A_MINUS_B AS (\n" + display_a_minus_b + "\n),\n"
            "B_MINUS_A AS (\n" + display_b_minus_a + "\n)\n"
            f"SELECT 'In {t1} but not in {t2}' AS DIFFERENCE_TYPE, * FROM A_MINUS_B\nUNION ALL\n"
            f"SELECT 'In {t2} but not in {t1}' AS DIFFERENCE_TYPE, * FROM B_MINUS_A\n"
            f"ORDER BY 2"
        )
    else:
        select_list = ", ".join(common)
        union_display_sql = (
            "WITH A_MINUS_B AS (\n" + exec_a_minus_b + "\n),\n"
            "B_MINUS_A AS (\n" + exec_b_minus_a + "\n)\n"
            f"SELECT 'In {t1} but not in {t2}' AS DIFFERENCE_TYPE, {select_list} FROM A_MINUS_B\nUNION ALL\n"
            f"SELECT 'In {t2} but not in {t1}' AS DIFFERENCE_TYPE, {select_list} FROM B_MINUS_A\n"
            f"ORDER BY 2"
        )


    return {
        "table_a": t1,
        "table_b": t2,
        "excluded": excludes_norm or None,
        "columns_compared": common,
        "queries": {
            "display_a_minus_b": display_a_minus_b,
            "display_b_minus_a": display_b_minus_a,
            "executed_a_minus_b": exec_a_minus_b,
            "executed_b_minus_a": exec_b_minus_a,
            "union_display": union_display_sql
        },
        "frames": {
            "a_minus_b": df_a_minus_b,
            "b_minus_a": df_b_minus_a,
            "column_diffs": col_diff_df,
            "union": union_df
        },
        "verdict": verdict,
        "message": message,
        "order_aligned": order_aligned
    }

# -----------------------------
# Helpers
# -----------------------------

_TABLE = r'(?:[A-Za-z0-9_]+|"(?:[^"]+)")'  # allow quoted names too, but we won't add quotes ourselves
_DB_SCH_TBL = rf'(?:{_TABLE}\.{_TABLE}\.{_TABLE})'
_SCHEMA_TBL = rf'(?:{_TABLE}\.{_TABLE})'     # INVALID per requirement

VS_TOKENS = r'(?:vs|versus|and|with)'

PROMPT_RE = re.compile(
    rf'(?:difference|diff|compare)[\s\w]*?({_DB_SCH_TBL}|{_TABLE})\s*(?:{VS_TOKENS})\s*({_DB_SCH_TBL}|{_TABLE})',
    re.IGNORECASE
)

# Accept:
#  - "... except the c1, c2 columns"
#  - "... except (c1, c2)"
EXCEPT_RE = re.compile(
    r'except(?:\s+the)?\s*(?:\((?P<paren>[^)]+)\)|(?P<bare>[^,]+(?:\s*,\s*[^,]+)*))\s*(?:columns?)?',
    re.IGNORECASE
)

def _parse_data_diff_prompt(text: str):
    m = PROMPT_RE.search(text)
    t1 = t2 = None
    if m:
        t1, t2 = m.group(1), m.group(2)

    excludes = []
    for mx in EXCEPT_RE.finditer(text):
        if mx.group("paren"):
            part = mx.group("paren")
        else:
            part = mx.group("bare") or ""
        pieces = [p.strip().strip('"') for p in re.split(r'[,\s]+', part) if p.strip()]
        excludes.extend(pieces)

    excludes = list(dict.fromkeys(excludes))  # de-dup preserve order
    return t1, t2, excludes or None

def _is_schema_table(ident: str) -> bool:
    ident = ident.strip()
    # two-part path must be considered invalid (not 1 or 3+ parts)
    parts = [p for p in ident.split(".") if p]
    return len(parts) == 2

def _describe_columns(conn, ident: str) -> List[str]:
    cur = conn.cursor()
    try:
        cur.execute(f"DESCRIBE TABLE {ident}")
        rows = cur.fetchall()
    finally:
        cur.close()
    cols = []
    for r in rows:
        name = r[0]
        if name is None or str(name).startswith("("):
            continue
        cols.append(str(name))
    return cols

def _run_df(conn, sql: str) -> pd.DataFrame:
    cur = conn.cursor()
    try:
        cur.execute(sql)
        cols = [c[0] for c in cur.description] if cur.description else []
        data = cur.fetchall()
    finally:
        cur.close()
    return pd.DataFrame(data, columns=cols)


# =====================================================
# Backward-compatible shims for legacy imports in app.py
# =====================================================

def diff_minus(conn, left_table: str, right_table: str, cols_list=None):
    """
    Legacy helper expected by older app.py branches.
    Returns (df_left_minus_right, df_right_minus_left, sql_left_minus_right, sql_right_minus_left).
    If cols_list is provided, only those columns are used; otherwise we align on common columns
    based on DESCRIBE TABLE and preserve left_table's order.
    """
    # Get columns
    cols_left = _describe_columns(conn, left_table)
    cols_right = _describe_columns(conn, right_table)

    if cols_list:
        # honor explicit user-provided list as-is
        common = [c for c in cols_list if c in cols_left and c in cols_right]
    else:
        # use intersection
        common = [c for c in cols_left if c in cols_right]

    if not common:
        raise ValueError("No common columns to compare.")

    select_list = ", ".join(common)

    s1 = f"SELECT {select_list} FROM {left_table}\nMINUS\nSELECT {select_list} FROM {right_table}"
    s2 = f"SELECT {select_list} FROM {right_table}\nMINUS\nSELECT {select_list} FROM {left_table}"

    df1 = _run_df(conn, s1)
    df2 = _run_df(conn, s2)
    return df1, df2, s1, s2


def explain_diff(conn, left_table: str, right_table: str, key_cols, compare_cols, tolerance: float = 0.0):
    """
    Legacy 'why error' helper. Produces a per-column distinct value difference report
    for the given compare_cols, ignoring key_cols (not joining). Tolerance is not used
    here (placeholder for future numeric diff support).
    Returns (report_df, sql_text).
    """
    if not compare_cols:
        raise ValueError("compare_cols must be provided for explain_diff.")

    rows = []
    sql_parts = []
    for col in compare_cols:
        q1 = f"SELECT DISTINCT {col} FROM {left_table} MINUS SELECT DISTINCT {col} FROM {right_table}"
        q2 = f"SELECT DISTINCT {col} FROM {right_table} MINUS SELECT DISTINCT {col} FROM {left_table}"
        only_left = _run_df(conn, q1)
        only_right = _run_df(conn, q2)
        rows.append({
            "column": col,
            "only_in_left_distinct_count": len(only_left.index),
            "only_in_right_distinct_count": len(only_right.index)
        })
        sql_parts.extend([q1, q2])

    import pandas as pd
    rep = pd.DataFrame(rows, columns=["column","only_in_left_distinct_count","only_in_right_distinct_count"])
    sql_text = "\n-- and --\n".join(sql_parts)
    return rep, sql_text


def suggestions_from_explain(rep_df):
    """
    Legacy helper to convert the explain_diff report into a friendly summary string.
    """
    if rep_df is None or rep_df.empty:
        return "No per-column differences detected."
    problems = []
    for _, r in rep_df.iterrows():
        left_cnt = int(r.get("only_in_left_distinct_count", 0) or 0)
        right_cnt = int(r.get("only_in_right_distinct_count", 0) or 0)
        if left_cnt or right_cnt:
            problems.append(f"{r['column']}: left-only={left_cnt}, right-only={right_cnt}")
    if not problems:
        return "No per-column differences detected."
    return "Columns with differing value sets → " + "; ".join(problems)


def _looks_number(s: str) -> bool:
    try:
        float(str(s))
        return True
    except Exception:
        return False
