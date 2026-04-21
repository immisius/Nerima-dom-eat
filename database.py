import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "meals.db")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


_NEW_SCHEMA = """
    CREATE TABLE meals (
        user_key TEXT NOT NULL DEFAULT '',
        date TEXT,
        meal_type TEXT,
        menu TEXT,
        registered INTEGER DEFAULT 0,
        deadline_passed INTEGER DEFAULT 0,
        is_holiday INTEGER DEFAULT 0,
        calendar_event_id TEXT,
        updated_at TEXT,
        PRIMARY KEY (user_key, date, meal_type)
    )
"""


def init_db():
    with get_conn() as conn:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='meals'").fetchone()
        existing_sql = row[0] if row else ""

        # PRIMARY KEY が正しくなければテーブルを作り直す
        if existing_sql and "PRIMARY KEY (user_key" not in existing_sql:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(meals)").fetchall()]
            if "user_key" not in cols:
                conn.execute("ALTER TABLE meals ADD COLUMN user_key TEXT NOT NULL DEFAULT ''")
            if "is_holiday" not in cols:
                conn.execute("ALTER TABLE meals ADD COLUMN is_holiday INTEGER DEFAULT 0")
            conn.execute("UPDATE meals SET user_key = '' WHERE user_key IS NULL")
            conn.execute("ALTER TABLE meals RENAME TO meals_old")
            conn.execute(_NEW_SCHEMA)
            conn.execute("INSERT INTO meals SELECT user_key,date,meal_type,menu,registered,deadline_passed,is_holiday,calendar_event_id,updated_at FROM meals_old")
            conn.execute("DROP TABLE meals_old")
        elif not existing_sql:
            conn.execute(_NEW_SCHEMA)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)


def get_setting(key: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None


def set_setting(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )


def upsert_meal(user_key: str, meal_date: str, meal_type: str, menu: str,
                registered: bool, deadline_passed: bool, is_holiday: bool = False):
    from datetime import datetime
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO meals (user_key, date, meal_type, menu, registered, deadline_passed, is_holiday, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_key, date, meal_type) DO UPDATE SET
                menu=excluded.menu,
                registered=excluded.registered,
                deadline_passed=excluded.deadline_passed,
                is_holiday=excluded.is_holiday,
                updated_at=excluded.updated_at
        """, (user_key, meal_date, meal_type, menu, int(registered),
              int(deadline_passed), int(is_holiday), datetime.now().isoformat()))


def get_all_meals(user_key: str = ""):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM meals WHERE user_key=? ORDER BY date, meal_type DESC",
            (user_key,)
        ).fetchall()
        return [dict(r) for r in rows]


def set_meal_registered(user_key: str, meal_date: str, meal_type: str, registered: bool):
    from datetime import datetime
    with get_conn() as conn:
        conn.execute(
            "UPDATE meals SET registered=?, updated_at=? WHERE user_key=? AND date=? AND meal_type=?",
            (int(registered), datetime.now().isoformat(), user_key, meal_date, meal_type)
        )


def update_calendar_event_id(user_key: str, meal_date: str, meal_type: str, event_id: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE meals SET calendar_event_id=? WHERE user_key=? AND date=? AND meal_type=?",
            (event_id, user_key, meal_date, meal_type)
        )
