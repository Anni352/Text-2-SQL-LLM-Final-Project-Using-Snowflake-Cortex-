import re
import pandas as pd

from .query_runner import run_query
from .sql_generator import generate_sql_from_question
from utils.helpers import quote_ident, quote_fqn


def _df(conn, sql: str) -> pd.DataFrame:
    cur = conn.cursor()
    try:
        cur.execute(sql)
        rows = cur.fetchall()
        cols = [c[0] for c in cur.description]
        return pd.DataFrame(rows, columns=cols)
    finally:
        cur.close()


def _lit(value: str) -> str:
    """Escape a value for safe use inside a single-quoted SQL string literal."""
    return str(value or "").replace("'", "''")


def test_rowcount(conn, table: str):
    sql = f"SELECT COUNT(*) AS CNT FROM {quote_fqn(table)}"
    df = _df(conn, sql)
    cnt = int(df.iloc[0, 0])
    return {"test": "Row Count > 0", "status": "PASS" if cnt > 0 else "FAIL", "details": f"Rows = {cnt}", "sql": sql}


def test_all_null(conn, table: str):
    tbl_lit = _lit(table)
    cols_sql = f"""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = SPLIT_PART('{tbl_lit}', '.', -1)
          AND TABLE_SCHEMA = SPLIT_PART('{tbl_lit}', '.', -2)
    """
    cols_df = _df(conn, cols_sql)
    cols = [r[0] for r in cols_df.values]
    null_cols = []
    for c in cols:
        q = f"SELECT COUNT(*) FROM {quote_fqn(table)} WHERE {quote_ident(c)} IS NOT NULL"
        dfx = _df(conn, q)
        if int(dfx.iloc[0, 0]) == 0:
            null_cols.append(c)
    status = "PASS" if not null_cols else "FAIL"
    details = "all columns have non-null values" if status == "PASS" else f"all-null: {', '.join(null_cols)}"
    return {"test": "No all-null columns", "status": status, "details": details, "sql": "(per-column checks)"}


def test_pk_unique(conn, table: str):
    tbl_lit = _lit(table)
    info_sql = f"""
    SELECT kcu.COLUMN_NAME
    FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
    JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
      ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
     AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA
     AND tc.TABLE_NAME = kcu.TABLE_NAME
    WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
      AND tc.TABLE_NAME = SPLIT_PART('{tbl_lit}', '.', -1)
      AND tc.TABLE_SCHEMA = SPLIT_PART('{tbl_lit}', '.', -2)
    ORDER BY kcu.ORDINAL_POSITION
    """
    cols_df = _df(conn, info_sql)
    cols = [r[0] for r in cols_df.values]
    if not cols:
        return {"test": "Primary key uniqueness (if defined)", "status": "SKIP", "details": "No PK defined", "sql": info_sql}
    pk_cols = ", ".join(quote_ident(c) for c in cols)
    uniq_sql = f"SELECT COUNT(*) - COUNT(DISTINCT {pk_cols}) AS DUPES FROM {quote_fqn(table)}"
    df = _df(conn, uniq_sql)
    dupes = int(df.iloc[0, 0])
    status = "PASS" if dupes == 0 else "FAIL"
    details = "PK unique" if status == "PASS" else f"{dupes} duplicate PKs"
    return {"test": "Primary key uniqueness (if defined)", "status": status, "details": details, "sql": uniq_sql}


def run_table_tests(conn, table: str) -> pd.DataFrame:
    tests = [test_rowcount, test_all_null, test_pk_unique]
    out = []
    for t in tests:
        try:
            out.append(t(conn, table))
        except Exception as e:
            out.append({"test": t.__name__, "status": "ERROR", "details": str(e), "sql": ""})
    return pd.DataFrame(out)


# --- Run custom natural-language test prompts in batch ---
def _status_for_test(prompt: str, sql: str, df: pd.DataFrame):
    """
    Decide PASS/FAIL/ERROR for a given test result.
    - Violation-style tests (duplicates/unique, nulls) PASS when 0 rows (or count==0)
    - Positive assertions (row count > 0) PASS when >0 rows (or count>0)
    Returns (status, details)
    """
    if df is None:
        return "ERROR", "query execution error"

    pl = (prompt or "").lower()
    sl = (sql or "").lower()

    # Heuristics for violation checks
    is_violation = any([
        "duplicate" in pl or "unique" in pl,
        "no all-null" in pl or "no null" in pl or "no nulls" in pl,
        "having count(" in sl and "> 1" in sl,
        "difference between" in pl,
        "is there difference" in pl,
        " minus " in sl or " except " in sl
    ])

    # Helper to get a count-like column if present
    count_cols = [c for c in df.columns if c.lower() in ("cnt", "count", "row_count", "rows", "dup_cnt")]
    if is_violation:
        # 0 rows => PASS
        if getattr(df, "empty", True):
            return "PASS", "No Violations (0 Rows Returned)"
        if count_cols:
            try:
                v = int(df.iloc[0][count_cols[0]])
                return ("PASS", "0 Violations") if v == 0 else ("FAIL", f"{v} Violations")
            except Exception:
                pass
        # rows present = violations found
        return "FAIL", f"{len(df)} Violating Row(s)"

    # Positive assertions
    if count_cols:
        try:
            v = int(df.iloc[0][count_cols[0]])
            return ("PASS", f"{count_cols[0]} = {v}") if v > 0 else ("FAIL", f"{count_cols[0]} = {v}")
        except Exception:
            pass

    return ("PASS", f"Rows = {len(df)}") if getattr(df, "empty", True) is False else ("FAIL", "Rows = 0")


def run_custom_tests(conn, prompts: list[str]):
    records = []
    for prompt in prompts:
        sql = ""
        try:
            sql = generate_sql_from_question(conn, prompt)
            df = run_query(conn, sql)
            status, details = _status_for_test(prompt, sql, df)
            records.append({"test": prompt, "status": status, "details": details, "sql": sql, "df": df})
        except Exception as e:
            records.append({"test": prompt, "status": "ERROR", "details": str(e), "sql": sql, "df": None})
    # Summary dataframe without the heavy 'df' objects
    df_summary = pd.DataFrame([{"test": r["test"], "status": r["status"], "details": r["details"], "sql": r["sql"]} for r in records])
    return records, df_summary
