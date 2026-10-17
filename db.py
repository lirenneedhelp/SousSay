"""SQLite persistence. Stdlib only. The app (not the LLM) owns all of this state."""
import os
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    recipe_id TEXT,
    recipe_json TEXT,
    step_index INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'idle',          -- idle | cooking | completed
    adaptations_json TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS timers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,         -- never reused
    session_id TEXT NOT NULL REFERENCES sessions(id),
    label TEXT NOT NULL,
    duration_s INTEGER NOT NULL,
    due_at REAL NOT NULL,                         -- absolute UTC epoch seconds
    status TEXT NOT NULL DEFAULT 'running',       -- running | expired | cancelled
    notified INTEGER NOT NULL DEFAULT 0,          -- 1 once the UI has been told it expired
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_timers_session ON timers(session_id, status);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,                           -- user | assistant (final text only)
    text TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE TABLE IF NOT EXISTS generated_recipes (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    recipe_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


def db_path() -> str:
    # Read at call time so tests / deployments can change it via the environment.
    return os.getenv("TALKSOUS_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "talksous.db"))


@contextmanager
def connect():
    path = db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
