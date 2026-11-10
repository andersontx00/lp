"""PostgreSQL adapter for RadarPreco's parameterized sqlite-style SQL.

Activated only with DATABASE_URL. Never falls back silently when PostgreSQL is
configured: failed durable storage must be visible rather than losing data.
"""
from __future__ import annotations
import re

class HybridRow(dict):
    """Match sqlite3.Row named and numerical lookups without leaking SQL objects."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


def hybrid_row(cursor):
    # Psycopg also invokes row factories for DDL and INSERT/UPDATE without RETURNING.
    # Those operations have no result-column description.
    if cursor.description is None:
        return lambda values: HybridRow()
    names = [c.name for c in cursor.description]
    def make(values): return HybridRow(zip(names, values))
    return make


def translate(query):
    q = str(query)
    if re.search(r'INSERT\s+OR\s+IGNORE\s+INTO', q, flags=re.I):
        q = re.sub(r'INSERT\s+OR\s+IGNORE\s+INTO', 'INSERT INTO', q, flags=re.I).rstrip().rstrip(';') + ' ON CONFLICT DO NOTHING'
    if re.search(r'INSERT\s+OR\s+REPLACE\s+INTO\s+bright_jobs', q, flags=re.I):
        q = re.sub(r'INSERT\s+OR\s+REPLACE\s+INTO', 'INSERT INTO', q, flags=re.I).rstrip().rstrip(';') + (
            ' ON CONFLICT(product) DO UPDATE SET snapshot_id=EXCLUDED.snapshot_id, created_at=EXCLUDED.created_at, checked_at=EXCLUDED.checked_at')
    # qmark placeholders are not used inside quoted SQL literals in this app.
    return q.replace('?', '%s')

class PgConnection:
    def __init__(self, connection): self._conn=connection
    def execute(self,sql,args=None): return self._conn.execute(translate(sql),args)
    def __enter__(self): self._conn.__enter__();return self
    def __exit__(self,*args): return self._conn.__exit__(*args)
    def close(self): return self._conn.close()


def connect(url):
    try: import psycopg
    except ImportError as exc:
        raise RuntimeError('DATABASE_URL foi configurada, mas falta instalar psycopg[binary].') from exc
    return PgConnection(psycopg.connect(url,connect_timeout=10,row_factory=hybrid_row))


def initialize(cn):
    cn.execute('''CREATE TABLE IF NOT EXISTS prices (
        id BIGSERIAL PRIMARY KEY,product TEXT NOT NULL,series TEXT NOT NULL,
        price DOUBLE PRECISION NOT NULL,at TEXT NOT NULL,source TEXT NOT NULL,
        url TEXT NOT NULL DEFAULT '',kind TEXT NOT NULL CHECK(kind IN ('snapshot','historical')),
        UNIQUE(product,series,at,kind))''')
    cn.execute('CREATE INDEX IF NOT EXISTS by_product_date ON prices(product,at)')
    cn.execute('''CREATE TABLE IF NOT EXISTS offers (product TEXT NOT NULL,merchant TEXT NOT NULL,
        title TEXT NOT NULL,price DOUBLE PRECISION NOT NULL,url TEXT NOT NULL,
        checked_at TEXT NOT NULL,image_url TEXT NOT NULL DEFAULT '',
        provider TEXT NOT NULL DEFAULT 'serpapi',shipping_price DOUBLE PRECISION,
        PRIMARY KEY(product,merchant))''')
    cn.execute('ALTER TABLE offers ADD COLUMN IF NOT EXISTS shipping_price DOUBLE PRECISION')
    cn.execute('''CREATE TABLE IF NOT EXISTS bright_jobs (
        product TEXT PRIMARY KEY,snapshot_id TEXT NOT NULL,created_at TEXT NOT NULL,
        checked_at TEXT NOT NULL DEFAULT '')''')
    cn.execute('''CREATE TABLE IF NOT EXISTS lookup_quota (
        id BIGSERIAL PRIMARY KEY,at TEXT NOT NULL,product TEXT NOT NULL,client_key TEXT NOT NULL DEFAULT '')''')
    cn.execute("ALTER TABLE lookup_quota ADD COLUMN IF NOT EXISTS client_key TEXT NOT NULL DEFAULT ''")
    cn.execute('CREATE INDEX IF NOT EXISTS lookup_quota_at ON lookup_quota(at)')
