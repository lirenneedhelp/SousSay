"""SQLite persistence. Stdlib only. The app (not the LLM) owns all of this state."""
import os
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    owner TEXT,                                   -- principal that created the session
    recipe_id TEXT,
    recipe_json TEXT,
    step_index INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'idle',          -- idle | cooking | completed
    adaptations_json TEXT NOT NULL DEFAULT '[]',
    servings INTEGER,                             -- NULL = the recipe's own serving count
    deduction_state TEXT,                         -- NULL (pending) | applied | skipped
    deduction_batch TEXT,                         -- pantry_log batch to undo
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pantry (
    owner TEXT NOT NULL,                          -- the signed-in user's principal (a pantry outlives sessions)
    item TEXT NOT NULL,                           -- normalised key, e.g. "egg"
    label TEXT NOT NULL,                          -- as the user said it, e.g. "eggs"
    amount REAL,                                  -- NULL = "have some", quantity unknown
    unit TEXT,                                    -- g | ml | piece | clove | can | bunch | head | handful
    updated_at REAL NOT NULL,
    PRIMARY KEY (owner, item)
);
CREATE TABLE IF NOT EXISTS pantry_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL,
    batch TEXT NOT NULL,
    item TEXT NOT NULL,
    label TEXT NOT NULL,
    delta REAL NOT NULL,                          -- amount subtracted (positive), restored on undo
    unit TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pantry_log_batch ON pantry_log(owner, batch);
CREATE TABLE IF NOT EXISTS user_prefs (
    owner TEXT PRIMARY KEY,
    units TEXT NOT NULL,                          -- metric | us
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
        # Migrate databases created before ownership existed. Their sessions keep owner NULL,
        # which matches no principal, so they become inaccessible rather than shared.
        columns = [r["name"] for r in conn.execute("PRAGMA table_info(sessions)")]
        for column, ddl in (("owner", "TEXT"), ("servings", "INTEGER"), ("deduction_state", "TEXT"), ("deduction_batch", "TEXT")):
            if column not in columns:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {column} {ddl}")
    try:  # the database holds user data: owner read/write only
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(db_path() + suffix):
                os.chmod(db_path() + suffix, 0o600)
    except OSError:
        pass


def ping() -> bool:
    """Cheap liveness check for the health endpoint."""
    with connect() as conn:
        return conn.execute("SELECT 1").fetchone()[0] == 1
