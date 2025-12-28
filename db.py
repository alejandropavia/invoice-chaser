import sqlite3
from pathlib import Path

DB_PATH = Path("app.db")

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_name TEXT NOT NULL,
            signature_name TEXT NOT NULL,
            day_1 INTEGER NOT NULL,
            day_2 INTEGER NOT NULL,
            day_3 INTEGER NOT NULL
        )
    """)
    # Una sola fila de settings para MVP
    cur.execute("SELECT COUNT(*) as c FROM settings")
    c = cur.fetchone()["c"]
    if c == 0:
        cur.execute("""
            INSERT INTO settings (company_name, signature_name, day_1, day_2, day_3)
            VALUES (?, ?, ?, ?, ?)
        """, ("Tu empresa", "Tu nombre", 7, 14, 30))
    conn.commit()
    conn.close()

def get_settings():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM settings ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def save_settings(company_name: str, signature_name: str, day_1: int, day_2: int, day_3: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO settings (company_name, signature_name, day_1, day_2, day_3)
        VALUES (?, ?, ?, ?, ?)
    """, (company_name, signature_name, day_1, day_2, day_3))
    conn.commit()
    conn.close()
