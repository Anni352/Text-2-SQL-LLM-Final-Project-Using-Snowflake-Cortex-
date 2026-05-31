import snowflake.connector

class SnowflakeConnector:
    def __init__(self, account: str, user: str, password: str):
        self._conn = snowflake.connector.connect(account=account, user=user, password=password)
        self._ctx = {"warehouse":"", "database":"", "schema":""}

    def ping(self):
        cur = self._conn.cursor()
        try:
            cur.execute("SELECT 1")
            cur.fetchone()
        finally:
            cur.close()

    def set_context(self, warehouse: str, database: str, schema: str):
        cur = self._conn.cursor()
        try:
            if warehouse:
                cur.execute(f"USE WAREHOUSE {warehouse}")
            if database:
                cur.execute(f"USE DATABASE {database}")
            if schema:
                cur.execute(f"USE SCHEMA {schema}")
            self._ctx = {"warehouse":warehouse, "database":database, "schema":schema}
        finally:
            cur.close()

    def has_context(self) -> bool:
        c = self._ctx
        return all(c.get(k) for k in ["warehouse","database","schema"])

    def conn(self):
        return self._conn
