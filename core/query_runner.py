import re, pandas as pd
from utils.helpers import extract_sql_from_text

UNSAFE = re.compile(r"\b(ALTER|CREATE|DROP|TRUNCATE|INSERT|UPDATE|DELETE|MERGE|GRANT|REVOKE|CALL|COPY)\b", re.I)

def validate_safe_sql(sql: str) -> bool:
    if not sql: return False
    if UNSAFE.search(sql): return False
    if sql.strip().count(";") > 1: return False
    return bool(re.match(r"^\s*(WITH|SELECT)\b", sql, re.I))

def run_query(conn, sql_fenced_or_plain: str) -> pd.DataFrame:
    sql = extract_sql_from_text(sql_fenced_or_plain) or ""
    if not validate_safe_sql(sql):
        raise ValueError("Unsafe or invalid SQL blocked.")
    cur = conn.cursor()
    try:
        cur.execute(sql)
        rows = cur.fetchall()
        cols = [c[0] for c in cur.description]
        return pd.DataFrame(rows, columns=cols)
    finally:
        cur.close()
