"""Cooking service layer.

All authoritative state (sessions, step position, timers, history) lives in SQLite and is
changed only by the functions here. The LLM asks for actions through tools; it never owns
state or deadlines. Every function returns plain dicts and raises CookingError for
expected, user-explainable failures.
"""
import json
import math
import re
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from typing import Any

from db import connect

DATA_DIR = Path(__file__).parent / "data"
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

MIN_TIMER_S, MAX_TIMER_S, MAX_RUNNING_TIMERS = 1, 12 * 3600, 10
MAX_GENERATED_PER_SESSION = 10
MAX_SESSIONS_PER_PRINCIPAL = 100

# Demo pantry (mock). Replace with a real inventory source later.
MOCK_PANTRY = [
    {"item": "chicken breast", "quantity": 2, "unit": "pieces"},
    {"item": "lemon", "quantity": 0.5, "unit": "pieces"},
    {"item": "rice", "quantity": 500, "unit": "grams"},
    {"item": "soy sauce", "quantity": 200, "unit": "ml"},
    {"item": "ginger", "quantity": 1, "unit": "root"},
]
STAPLES = {"salt", "pepper", "black pepper", "water", "oil", "olive oil", "vegetable oil", "cooking oil"}


class CookingError(Exception):
    """An expected failure whose message is safe to show the model and the user."""


class SessionNotFound(CookingError):
    """Unknown session, or one owned by someone else (deliberately indistinguishable)."""


# Who is calling. Set by the API layer (browser user) or the MCP layer (bearer-key client) and
# checked on every session access. Unset means unauthenticated, and every access fails closed.
_principal: ContextVar[str | None] = ContextVar("principal", default=None)


@contextmanager
def acting_as(principal: str):
    token = _principal.set(principal)
    try:
        yield
    finally:
        _principal.reset(token)


def _now() -> float:
    return time.time()  # tests patch this


# ---------------------------------------------------------------- data loading
@lru_cache(maxsize=1)
def _recipes() -> dict:
    items = json.loads((DATA_DIR / "recipes.json").read_text(encoding="utf-8"))
    return {r["id"]: {**r, "ai_generated": False} for r in items}


@lru_cache(maxsize=1)
def _substitutions() -> dict:
    return json.loads((DATA_DIR / "substitutions.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _safety() -> dict:
    return json.loads((DATA_DIR / "food_safety.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------- text utils
def _norm_word(w: str) -> str:
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("oes") and len(w) > 4:
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def _norm(text: str) -> str:
    text = re.sub(r"[^a-z0-9 ]+", " ", str(text).lower().replace("-", " "))
    return " ".join(_norm_word(w) for w in text.split())


def _tokens_match(a: str, b: str) -> bool:
    ta, tb = set(_norm(a).split()), set(_norm(b).split())
    return bool(ta and tb) and (ta <= tb or tb <= ta)


def _mmss(seconds: int) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


# --------------------------------------------------------------------- sessions
def ensure_session(session_id: str) -> None:
    if not isinstance(session_id, str) or not SESSION_RE.match(session_id):
        raise CookingError("Invalid session id.")
    principal = _principal.get()
    if not principal:
        raise SessionNotFound("Unknown session.")
    now = _now()
    with connect() as conn:
        inserted = conn.execute(
            "INSERT OR IGNORE INTO sessions(id, owner, created_at, updated_at) VALUES (?,?,?,?)",
            (session_id, principal, now, now),
        ).rowcount == 1
        if inserted and conn.execute("SELECT COUNT(*) FROM sessions WHERE owner=?", (principal,)).fetchone()[0] \
                > MAX_SESSIONS_PER_PRINCIPAL:
            raise CookingError("Too many sessions.")  # raising rolls the insert back
        owner = conn.execute("SELECT owner FROM sessions WHERE id=?", (session_id,)).fetchone()["owner"]
    if owner != principal:
        raise SessionNotFound("Unknown session.")


def _session(conn, session_id: str):
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if row is None:
        raise CookingError("Unknown session.")
    return row


def _recipe_of(row) -> dict | None:
    return json.loads(row["recipe_json"]) if row["recipe_json"] else None


def _step_payload(recipe: dict, idx: int, adaptations: list) -> dict:
    step = recipe["steps"][idx]
    payload = {
        "step_number": idx + 1,
        "total_steps": len(recipe["steps"]),
        "instruction": step["text"],
    }
    if step.get("timer_s"):
        payload["timer_hint"] = {"label": step.get("timer_label", "timer"), "duration_s": step["timer_s"]}
    if adaptations:
        payload["adaptations"] = [f"use {a['replacement']} instead of {a['original']}" for a in adaptations]
    return payload


def session_view(session_id: str) -> dict:
    ensure_session(session_id)
    with connect() as conn:
        row = _session(conn, session_id)
    recipe = _recipe_of(row)
    view = {"status": row["status"], "recipe": None, "step": None, "adaptations": []}
    if recipe:
        adaptations = json.loads(row["adaptations_json"])
        view["recipe"] = {"id": recipe["id"], "title": recipe["title"], "ai_generated": recipe["ai_generated"]}
        view["adaptations"] = adaptations
        view["step"] = _step_payload(recipe, row["step_index"], adaptations)
    return view


# ---------------------------------------------------------------------- recipes
def get_inventory(session_id: str) -> dict:
    ensure_session(session_id)
    return {"inventory": MOCK_PANTRY, "note": "Demo pantry data."}


def _generated(conn, session_id: str) -> dict:
    rows = conn.execute("SELECT recipe_json FROM generated_recipes WHERE session_id=?", (session_id,)).fetchall()
    out = {}
    for r in rows:
        rec = json.loads(r["recipe_json"])
        out[rec["id"]] = rec
    return out


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [p.strip() for p in re.split(r",|;|\band\b", value) if p.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def search_recipes(session_id: str, ingredients=None, max_results: int = 3) -> dict:
    ensure_session(session_id)
    have = _as_list(ingredients)
    used_inventory = False
    if not have:
        have = [p["item"] for p in MOCK_PANTRY]
        used_inventory = True
    max_results = max(1, min(int(max_results or 3), 5))
    with connect() as conn:
        pool = {**_recipes(), **_generated(conn, session_id)}
    staples = {_norm(s) for s in STAPLES}
    scored = []
    for rec in pool.values():
        required = [i["name"] for i in rec["ingredients"] if _norm(i["name"]) not in staples]
        if not required:
            continue
        matched = [n for n in required if any(_tokens_match(h, n) for h in have)]
        if not matched:
            continue
        missing = [n for n in required if n not in matched]
        scored.append((len(matched) / len(required), len(missing), rec["time_min"], rec, matched, missing))
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))
    results = [
        {"id": r["id"], "title": r["title"], "time_min": r["time_min"], "serves": r["serves"],
         "match_pct": round(cov * 100), "have": m, "missing": miss, "ai_generated": r["ai_generated"]}
        for cov, _, _, r, m, miss in scored[:max_results]
    ]
    out = {"searched_with": have, "used_inventory": used_inventory, "results": results}
    if not results or results[0]["match_pct"] < 60:
        out["note"] = "No strong curated match. Suggest substitutions, or create_recipe as a labelled AI fallback."
    return out


def _find_recipe(conn, session_id: str, query: str) -> dict:
    q = _norm(query)
    if len(q) < 3:
        raise CookingError("Tell me which recipe you want.")
    pool = {**_recipes(), **_generated(conn, session_id)}
    for rid, rec in pool.items():  # exact id or exact title first
        if q in (_norm(rid), _norm(rec["title"])):
            return rec
    hits = [rec for rec in pool.values() if _tokens_match(q, rec["title"]) or _tokens_match(q, rec["id"])]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise CookingError(f"I couldn't find a recipe called '{query}'.")
    raise CookingError("That matches several recipes: " + "; ".join(f"{h['title']} ({h['id']})" for h in hits[:5]))


def start_recipe(session_id: str, recipe: str) -> dict:
    ensure_session(session_id)
    with connect() as conn:
        rec = _find_recipe(conn, session_id, recipe)
        conn.execute(
            "UPDATE sessions SET recipe_id=?, recipe_json=?, step_index=0, status='cooking', "
            "adaptations_json='[]', updated_at=? WHERE id=?",
            (rec["id"], json.dumps(rec), _now(), session_id),  # timers are deliberately kept
        )
    return {
        "status": "recipe_started", "title": rec["title"], "ai_generated": rec["ai_generated"],
        "ingredients": [f"{i['qty']} {i['name']}".strip() for i in rec["ingredients"]],
        "safety_notes": rec.get("safety_notes", []),
        "current_step": _step_payload(rec, 0, []),
    }


def _move(session_id: str, delta: int) -> dict:
    ensure_session(session_id)
    with connect() as conn:
        row = _session(conn, session_id)
        rec = _recipe_of(row)
        if not rec or row["status"] == "idle":
            raise CookingError("No recipe is active. Ask what to cook first.")
        idx, last = row["step_index"], len(rec["steps"]) - 1
        adaptations = json.loads(row["adaptations_json"])
        if delta > 0 and (row["status"] == "completed" or idx >= last):
            conn.execute("UPDATE sessions SET status='completed', updated_at=? WHERE id=?", (_now(), session_id))
            return {"status": "completed", "message": "That was the last step. Enjoy!",
                    "leftovers": "Refrigerate leftovers within 2 hours."}
        if delta < 0 and row["status"] == "completed":
            new_idx = last
        else:
            new_idx = max(0, min(last, idx + delta))
        conn.execute("UPDATE sessions SET step_index=?, status='cooking', updated_at=? WHERE id=?",
                     (new_idx, _now(), session_id))
        payload = _step_payload(rec, new_idx, adaptations)
        if delta < 0 and new_idx == idx and idx == 0:
            payload["note"] = "Already at the first step."
        return payload


def next_step(session_id: str) -> dict:
    return _move(session_id, +1)


def previous_step(session_id: str) -> dict:
    return _move(session_id, -1)


def get_current_step(session_id: str) -> dict:
    ensure_session(session_id)
    with connect() as conn:
        row = _session(conn, session_id)
    rec = _recipe_of(row)
    if not rec or row["status"] == "idle":
        raise CookingError("No recipe is active. Ask what to cook first.")
    if row["status"] == "completed":
        return {"status": "completed", "message": "The recipe is finished."}
    return _step_payload(rec, row["step_index"], json.loads(row["adaptations_json"]))


def stop_recipe(session_id: str) -> dict:
    ensure_session(session_id)
    with connect() as conn:
        conn.execute("UPDATE sessions SET recipe_id=NULL, recipe_json=NULL, step_index=0, status='idle', "
                     "adaptations_json='[]', updated_at=? WHERE id=?", (_now(), session_id))
    return {"status": "stopped", "note": "Timers keep running."}


# ------------------------------------------------------------------ adaptation
def suggest_substitution(session_id: str, ingredient: str) -> dict:
    ensure_session(session_id)
    key = _norm(ingredient)
    if len(key) < 2:
        raise CookingError("Which ingredient is missing?")
    for name, options in _substitutions().items():
        if _norm(name) == key or _tokens_match(name, key):
            return {"ingredient": ingredient, "found": True, "options": options}
    return {"ingredient": ingredient, "found": False,
            "note": "No curated substitution. Say so, offer a cautious suggestion, keep food-safety temperatures unchanged."}


def apply_substitution(session_id: str, original: str, replacement: str) -> dict:
    ensure_session(session_id)
    if len(_norm(replacement)) < 2:
        raise CookingError("What should replace it?")
    with connect() as conn:
        row = _session(conn, session_id)
        rec = _recipe_of(row)
        if not rec or row["status"] == "idle":
            raise CookingError("No recipe is active.")
        if not any(_tokens_match(original, i["name"]) for i in rec["ingredients"]):
            raise CookingError(f"This recipe doesn't use {original}.")
        adaptations = [a for a in json.loads(row["adaptations_json"]) if not _tokens_match(a["original"], original)]
        adaptations.append({"original": original, "replacement": replacement})
        conn.execute("UPDATE sessions SET adaptations_json=?, updated_at=? WHERE id=?",
                     (json.dumps(adaptations), _now(), session_id))
    out: dict[str, Any] = {"status": "substituted", "use": replacement, "instead_of": original}
    safety = food_safety_lookup(replacement)
    if safety.get("found"):
        out["safety_check"] = safety
    return out


_BLOCKED = [
    r"\brins\w* (the )?(raw )?(chicken|poultry|turkey)",
    r"\b(rare|undercooked|pink)\b[^.]{0,15}\b(chicken|turkey|poultry|pork)\b",
    r"\b(chicken|turkey|poultry|pork)\b(?![^.]{0,30}\b(until|no longer|not|never|without)\b)[^.]{0,20}\b(pink|rare|undercooked)\b",
    r"\b(leave|left|sit|stand|rest|thaw)\w*[^.]{0,40}\b(counter|room temperature)\b[^.]{0,40}(overnight|hours?|\d+ ?h)",
    r"\bthaw\w*[^.]{0,30}\bon the counter",
    r"\braw (egg|chicken|pork|poultry)\b[^.]{0,30}\b(eat|serve|taste)",
]


def create_recipe(session_id: str, title: str, ingredients, steps, safety_notes=None) -> dict:
    """Controlled AI-generation fallback: the app validates, labels and stores the recipe."""
    ensure_session(session_id)
    title = str(title or "").strip()
    if not 3 <= len(title) <= 80:
        raise CookingError("Recipe title must be 3 to 80 characters.")
    ings = []
    for i in ingredients or []:
        name, qty = (i.get("name"), i.get("qty", "")) if isinstance(i, dict) else (i, "")
        name, qty = str(name or "").strip(), str(qty or "").strip()
        if not 2 <= len(name) <= 60 or len(qty) > 40:
            raise CookingError("Each ingredient needs a name (2-60 chars) and a short quantity.")
        ings.append({"name": name, "qty": qty})
    if not 2 <= len(ings) <= 20:
        raise CookingError("A recipe needs 2 to 20 ingredients.")
    step_list = [str(s).strip() for s in steps or []]
    if not 3 <= len(step_list) <= 12 or any(not 10 <= len(s) <= 300 for s in step_list):
        raise CookingError("A recipe needs 3 to 12 steps, each 10 to 300 characters.")
    joined = " ".join(step_list).lower()
    for pattern in _BLOCKED:
        if re.search(pattern, joined):
            raise CookingError("A step describes an unsafe food-handling practice. Rewrite it safely.")
    notes = [str(n).strip() for n in (safety_notes or []) if str(n).strip()][:5]
    haystack = _norm(title + " " + " ".join(i["name"] for i in ings))
    for entry in _safety()["entries"]:
        if not entry.get("cook"):
            continue
        if any(set(_norm(k).split()) <= set(haystack.split()) for k in entry["keys"]):
            f, c = entry["temp_f"], entry["temp_c"]
            if str(f) not in joined and str(c) not in joined:
                raise CookingError(f"This recipe contains {entry['keys'][0]}. Add a step that cooks it to "
                                   f"{f}°F ({c}°C) internal temperature, checked with a thermometer.")
            required = f"Cook to an internal temperature of {f}°F ({c}°C)."
            if required not in notes:
                notes.append(required)
    with connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM generated_recipes WHERE session_id=?", (session_id,)).fetchone()[0]
        if count >= MAX_GENERATED_PER_SESSION:
            raise CookingError("Too many generated recipes this session.")
        rid = "gen-" + uuid.uuid4().hex[:8]
        rec = {"id": rid, "title": title, "time_min": 30, "serves": 2, "tags": ["ai-generated"],
               "ingredients": ings, "steps": [{"text": s} for s in step_list],
               "safety_notes": notes, "ai_generated": True}
        conn.execute("INSERT INTO generated_recipes(id, session_id, recipe_json, created_at) VALUES (?,?,?,?)",
                     (rid, session_id, json.dumps(rec), _now()))
    return {"status": "created", "recipe_id": rid, "title": title, "ai_generated": True, "safety_notes": notes,
            "note": "Tell the user this is an AI-generated recipe, then ask if they want to start it."}


# ----------------------------------------------------------------------- safety
def food_safety_lookup(food: str) -> dict:
    q = set(_norm(food).split())
    general = _safety()["general"]
    for entry in _safety()["entries"]:
        if any(set(_norm(k).split()) <= q for k in entry["keys"]):
            return {"found": True, "food": food, "min_internal_temp_f": entry["temp_f"],
                    "min_internal_temp_c": entry["temp_c"], "rest_time": entry["rest"],
                    "note": entry["note"], "general_rules": general}
    return {"found": False, "food": food, "general_rules": general,
            "note": "No curated entry. Recommend checking USDA guidance."}


# ----------------------------------------------------------------------- timers
def _refresh(conn, session_id: str, now: float) -> None:
    conn.execute("UPDATE timers SET status='expired' WHERE session_id=? AND status='running' AND due_at<=?",
                 (session_id, now))


def _timer_view(row, now: float) -> dict:
    view = {"id": row["id"], "label": row["label"], "duration_s": row["duration_s"], "status": row["status"]}
    if row["status"] == "running":
        view["remaining_s"] = max(0, math.ceil(row["due_at"] - now))
    else:
        view["remaining_s"] = 0
        view["expired_ago_s"] = max(0, int(now - row["due_at"]))
    return view


def set_timer(session_id: str, label: str, minutes: float = 0, seconds: int = 0) -> dict:
    ensure_session(session_id)
    label = str(label or "").strip()
    if not 1 <= len(label) <= 60:
        raise CookingError("Give the timer a short name.")
    try:
        total = int(round(float(minutes or 0) * 60)) + int(seconds or 0)
    except (TypeError, ValueError):
        raise CookingError("Timer duration must be a number.")
    if not MIN_TIMER_S <= total <= MAX_TIMER_S:
        raise CookingError("A timer must be between 1 second and 12 hours.")
    now = _now()
    with connect() as conn:
        _refresh(conn, session_id, now)
        running = conn.execute("SELECT COUNT(*) FROM timers WHERE session_id=? AND status='running'",
                               (session_id,)).fetchone()[0]
        if running >= MAX_RUNNING_TIMERS:
            raise CookingError("Too many timers are running.")
        cur = conn.execute("INSERT INTO timers(session_id, label, duration_s, due_at, created_at) VALUES (?,?,?,?,?)",
                           (session_id, label, total, now + total, now))
        row = conn.execute("SELECT * FROM timers WHERE id=?", (cur.lastrowid,)).fetchone()
    return {"status": "timer_started", "timer": _timer_view(row, now)}


def list_timers(session_id: str) -> dict:
    ensure_session(session_id)
    now = _now()
    with connect() as conn:
        _refresh(conn, session_id, now)
        rows = conn.execute("SELECT * FROM timers WHERE session_id=? AND status!='cancelled' "
                            "ORDER BY due_at LIMIT 30", (session_id,)).fetchall()
    return {"timers": [_timer_view(r, now) for r in rows]}


def cancel_timer(session_id: str, timer_id: int | None = None, label: str | None = None) -> dict:
    """Cancels a running timer or dismisses an expired one, by id or by label."""
    ensure_session(session_id)
    now = _now()
    with connect() as conn:
        _refresh(conn, session_id, now)
        rows = conn.execute("SELECT * FROM timers WHERE session_id=? AND status!='cancelled'", (session_id,)).fetchall()
        if timer_id is not None:
            hits = [r for r in rows if r["id"] == int(timer_id)]
        elif label:
            hits = [r for r in rows if _tokens_match(label, r["label"])]
        else:
            hits = rows if len(rows) == 1 else []
        if not hits:
            raise CookingError("I couldn't find that timer.")
        if len(hits) > 1:
            raise CookingError("Several timers match: " + "; ".join(f"#{r['id']} {r['label']}" for r in hits))
        conn.execute("UPDATE timers SET status='cancelled', notified=1 WHERE id=?", (hits[0]["id"],))
    return {"status": "timer_cancelled", "timer": {"id": hits[0]["id"], "label": hits[0]["label"]}}


def _spoken_duration(seconds: int) -> str:
    minutes, secs = divmod(int(seconds), 60)
    parts = []
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if secs or not minutes:
        parts.append(f"{secs} second{'s' if secs != 1 else ''}")
    return " and ".join(parts)


def describe_timers(session_id: str) -> str:
    """Answers "what timers do I have / how long is left" from the database, never from the model."""
    timers = list_timers(session_id)["timers"]
    if not timers:
        return "You don't have any timers running."
    parts = [f"{t['label']} is done" if t["status"] == "expired" else f"{t['label']} has {_spoken_duration(t['remaining_s'])} left"
             for t in timers]
    head = "You have one timer: " if len(parts) == 1 else f"You have {len(parts)} timers: "
    return head + "; ".join(parts) + "."


def pop_newly_expired(session_id: str) -> list:
    """Returns timers that expired and were not yet reported to the UI, and marks them reported."""
    ensure_session(session_id)
    now = _now()
    with connect() as conn:
        _refresh(conn, session_id, now)
        rows = conn.execute("SELECT * FROM timers WHERE session_id=? AND status='expired' AND notified=0 "
                            "ORDER BY due_at", (session_id,)).fetchall()
        if rows:
            conn.execute(f"UPDATE timers SET notified=1 WHERE id IN ({','.join('?' * len(rows))})",
                         [r["id"] for r in rows])
    return [_timer_view(r, now) for r in rows]


# ---------------------------------------------------------- state and history
def get_state(session_id: str, pop_expired: bool = False) -> dict:
    view = session_view(session_id)
    state = {"session": view, "timers": list_timers(session_id)["timers"]}
    if pop_expired:
        state["newly_expired"] = pop_newly_expired(session_id)
    return state


def state_summary(session_id: str) -> str:
    """Authoritative state, injected into the system prompt every turn."""
    st = get_state(session_id)
    s = st["session"]
    lines = ["CURRENT KITCHEN STATE (authoritative, maintained by the app):"]
    if s["recipe"]:
        tag = " (AI-generated)" if s["recipe"]["ai_generated"] else ""
        lines.append(f"- Recipe: {s['recipe']['title']}{tag}; status: {s['status']}")
        if s["step"] and s["status"] == "cooking":
            lines.append(f"- Step {s['step']['step_number']} of {s['step']['total_steps']}: {s['step']['instruction']}")
        for a in s["adaptations"]:
            lines.append(f"- Substitution: use {a['replacement']} instead of {a['original']}")
    else:
        lines.append("- No active recipe.")
    if st["timers"]:
        for t in st["timers"]:
            when = "EXPIRED" if t["status"] == "expired" else f"{_mmss(t['remaining_s'])} left"
            lines.append(f"- Timer #{t['id']} '{t['label']}': {when}")
    else:
        lines.append("- No timers.")
    return "\n".join(lines)


def add_messages(session_id: str, pairs: list) -> None:
    ensure_session(session_id)
    now = _now()
    with connect() as conn:
        conn.executemany("INSERT INTO messages(session_id, role, text, created_at) VALUES (?,?,?,?)",
                         [(session_id, role, text, now) for role, text in pairs])


def load_history(session_id: str, limit: int = 10) -> list:
    ensure_session(session_id)
    with connect() as conn:
        rows = conn.execute("SELECT role, text FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
                            (session_id, limit)).fetchall()
    rows = list(reversed(rows))
    while rows and rows[0]["role"] != "user":
        rows.pop(0)
    return [{"role": r["role"], "content": [{"text": r["text"]}]} for r in rows]


# ------------------------------------------------------------------ conversion
_VOL_ML = {"tsp": 4.92892, "tbsp": 14.7868, "cup": 236.588, "floz": 29.5735, "ml": 1.0, "l": 1000.0}
_WT_G = {"g": 1.0, "kg": 1000.0, "oz": 28.3495, "lb": 453.592}
_ALIASES = {"teaspoon": "tsp", "tablespoon": "tbsp", "cups": "cup", "gram": "g", "gr": "g", "kilogram": "kg",
            "ounce": "oz", "pound": "lb", "lbs": "lb", "milliliter": "ml", "millilitre": "ml", "liter": "l",
            "litre": "l", "fl oz": "floz", "fluid ounce": "floz", "fahrenheit": "f", "celsius": "c"}
_G_PER_CUP = {"flour": 120, "sugar": 200, "brown sugar": 213, "butter": 227, "rice": 185, "milk": 245,
              "water": 237, "honey": 340, "oil": 218, "oats": 90, "cocoa": 85}
_KNOWN_UNITS = set(_VOL_ML) | set(_WT_G) | {"f", "c"}


def _unit(u: str) -> str:
    u = re.sub(r"\s+", " ", str(u).lower().replace("°", "").replace("degrees", "").strip().rstrip("."))
    for cand in (u, u[:-1] if u.endswith("s") else u):
        cand = _ALIASES.get(cand, cand)
        if cand in _KNOWN_UNITS:
            return cand
    return u


def convert_measurements(amount: float, from_unit: str, to_unit: str, ingredient_context: str = "") -> dict:
    a, f, t = float(amount), _unit(from_unit), _unit(to_unit)
    if f in ("f", "c") and t in ("f", "c"):
        val = a if f == t else ((a - 32) * 5 / 9 if f == "f" else a * 9 / 5 + 32)
        return {"original": f"{a}°{f.upper()}", "converted": f"{round(val)}°{t.upper()}"}
    ing = _norm(ingredient_context)
    if f in _VOL_ML and t in _VOL_ML:
        return {"converted": f"{round(a * _VOL_ML[f] / _VOL_ML[t], 2)} {t}"}
    if f in _WT_G and t in _WT_G:
        return {"converted": f"{round(a * _WT_G[f] / _WT_G[t], 2)} {t}"}
    density = next((g for name, g in sorted(_G_PER_CUP.items(), key=lambda kv: -len(kv[0])) if name in ing), None)
    if density and f in _VOL_ML and t in _WT_G:
        grams = a * _VOL_ML[f] / _VOL_ML["cup"] * density
        return {"converted": f"{round(grams / _WT_G[t], 1)} {t}", "note": f"Assumes {density} g per cup of {ing}."}
    if density and f in _WT_G and t in _VOL_ML:
        cups = a * _WT_G[f] / density
        return {"converted": f"{round(cups * _VOL_ML['cup'] / _VOL_ML[t], 2)} {t}",
                "note": f"Assumes {density} g per cup of {ing}."}
    raise CookingError(f"I can't convert {from_unit} to {to_unit} for '{ingredient_context}'.")