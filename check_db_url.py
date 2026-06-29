"""
Diagnose the DATABASE_URL without exposing the password.

Prints the parsed pieces (password masked), flags symbols that can break a
connection URL, and attempts a real connection so we see the true error.

    python check_db_url.py
"""
import os
import sys
from urllib.parse import urlparse

url = os.environ.get("DATABASE_URL", "").strip()
if not url:
    sys.exit("DATABASE_URL is not set in this shell.")

p = urlparse(url)
pw = p.password or ""
risky = sorted({c for c in pw if not (c.isalnum())})

print("scheme  :", p.scheme)
print("username:", p.username)
print("host    :", p.hostname)
print("port    :", p.port)
print("database:", (p.path or "").lstrip("/"))
print("password: length", len(pw),
      "| masked", (pw[0] + "*" * (len(pw) - 2) + pw[-1]) if len(pw) > 2 else "***")
if risky:
    print("  >>> password contains non-alphanumeric chars:", " ".join(risky))
    print("  >>> these can break the URL. Use a letters+numbers-only password,")
    print("      or percent-encode them.")
else:
    print("  >>> password is all letters/numbers (good).")

print("\nAttempting connection...")
try:
    import psycopg2
    import db
    conn = psycopg2.connect(db._pg_dsn())
    cur = conn.cursor()
    cur.execute("SELECT current_user, version()")
    who, ver = cur.fetchone()
    print("  SUCCESS — connected as:", who)
    print("  server:", ver.split(",")[0])
    conn.close()
except Exception as e:
    print("  FAILED:", e)
