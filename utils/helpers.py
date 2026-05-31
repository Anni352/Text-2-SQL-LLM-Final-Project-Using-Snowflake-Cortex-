import re, os, json, base64, pathlib
from typing import Optional
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend
from cryptography.fernet import Fernet

STORE_DIR = ".store"
SALT = b"text2sql_snowflake_v2"

def extract_sql_from_text(text: str) -> Optional[str]:
    """Extract a fenced SQL block or a plain SELECT/WITH. Return with a single trailing semicolon."""
    if not text:
        return None
    m = re.search(r"```sql\s*(.*?)```", text, flags=re.IGNORECASE|re.DOTALL)
    if m:
        return m.group(1).strip().rstrip(";") + ";"
    m = re.search(r"```\s*(.*?)```", text, flags=re.DOTALL)
    if m:
        return m.group(1).strip().rstrip(";") + ";"
    txt = text.strip()
    if re.match(r"^(WITH|SELECT)\b", txt, re.IGNORECASE):
        txt = re.split(r"(?i)\n(?:follow[- ]?ups?|questions?)\b", txt)[0]
        return txt.rstrip(";") + ";"
    return None

def derive_key(account: str, user: str, password: str) -> bytes:
    material = f"{account}|{user}|{password}".encode("utf-8")
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=SALT, iterations=390000, backend=default_backend())
    return base64.urlsafe_b64encode(kdf.derive(material))

class EncryptedKV:
    def __init__(self, key: bytes, account: str, user: str):
        self.fernet = Fernet(key)
        pathlib.Path(STORE_DIR).mkdir(exist_ok=True)
        self.path = pathlib.Path(STORE_DIR) / f"{account}__{user}.json.enc"

    def read(self) -> Optional[dict]:
        if not self.path.exists():
            return None
        try:
            raw = self.path.read_bytes()
            return json.loads(self.fernet.decrypt(raw).decode("utf-8"))
        except Exception:
            return None

    def write(self, payload: dict):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        token = self.fernet.encrypt(raw)
        self.path.write_bytes(token)


# ----- Common SQL identifier helpers & DataFrame utilities -----
def quote_ident(name: str) -> str:
    n = str(name).strip().strip('"')
    return f'"{n}"'

def quote_fqn(table: str) -> str:
    parts = [p.strip().strip('"') for p in str(table).split(".") if p.strip()]
    return ".".join(quote_ident(p) for p in parts)

def df_bool_to_text(df):
    """Convert boolean columns in a DataFrame to 'True'/'False' strings (non-destructive)."""
    try:
        import pandas as pd
        if df is None or not isinstance(df, pd.DataFrame):
            return df
        out = df.copy()
        for c in out.columns:
            if str(out[c].dtype) == 'bool':
                out[c] = out[c].map({True: "True", False: "False"})
        return out
    except Exception:
        return df
