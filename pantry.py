"""Per-user pantry with quantities, undoable deduction after cooking, and the user's unit system.

A pantry belongs to the signed-in user (not to a cooking session), so it survives "New session".
Amounts are stored in canonical units (g, ml, or a counted unit); the unit system only changes
how they are shown. The app, never the model, changes these numbers."""
import json
import re
import uuid

import units
from cooking import (STAPLES, CookingError, _now, _recipe_of, _session, current_principal, ensure_session,
                     ingredient_amount, session_scale)
from db import connect

MAX_ITEMS = 100   # distinct pantry items per user
MAX_BATCH = 20    # items changed per request


# ------------------------------------------------------------------ preferences
def _units(conn, owner: str) -> str:
    row = conn.execute("SELECT units FROM user_prefs WHERE owner=?", (owner,)).fetchone()
    return row["units"] if row else "metric"


def get_units() -> str:
    owner = current_principal()
    with connect() as conn:
        return _units(conn, owner)


def units_set() -> bool:
    owner = current_principal()
    with connect() as conn:
        return conn.execute("SELECT 1 FROM user_prefs WHERE owner=?", (owner,)).fetchone() is not None


def set_units(system: str) -> dict:
    owner = current_principal()
    if system not in units.UNIT_SYSTEMS:
        raise CookingError("Units must be 'metric' or 'us'.")
    with connect() as conn:
        conn.execute("INSERT INTO user_prefs(owner, units, updated_at) VALUES (?,?,?) "
                     "ON CONFLICT(owner) DO UPDATE SET units=excluded.units, updated_at=excluded.updated_at",
                     (owner, system, _now()))
    return {"units": system}


# ----------------------------------------------------------------------- reading
def _rows(conn, owner: str) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM pantry WHERE owner=? ORDER BY label", (owner,))]


def rows() -> list[dict]:
    owner = current_principal()
    with connect() as conn:
        return _rows(conn, owner)


def available_names(pantry_rows: list[dict]) -> list[str]:
    return [r["label"] for r in pantry_rows if r["amount"] is None or r["amount"] > 0]


def _find_row(pantry_rows: list[dict], name: str, exact_only: bool = False) -> dict | None:
    key = units.norm(name)
    for r in pantry_rows:
        if r["item"] == key:
            return r
    if exact_only:
        return None
    hits = [r for r in pantry_rows if units.tokens_match(r["label"], name) or units.tokens_match(r["item"], name)]
    if not hits:
        return None
    overlap = lambda r: len(set(r["item"].split()) & set(key.split()))  # noqa: E731
    best = max(overlap(r) for r in hits)
    top = [r for r in hits if overlap(r) == best]
    return top[0] if len(top) == 1 else None  # ambiguous -> no match rather than a wrong one


def _display(row: dict, system: str) -> str:
    return units.format_item(row["label"], row["amount"], row["unit"], system)


def list_pantry() -> dict:
    owner = current_principal()
    with connect() as conn:
        system, items = _units(conn, owner), _rows(conn, owner)
        is_set = conn.execute("SELECT 1 FROM user_prefs WHERE owner=?", (owner,)).fetchone() is not None
    return {"units": system, "units_set": is_set,
            "items": [{"item": r["item"], "label": r["label"], "amount": r["amount"], "unit": r["unit"],
                       "display": _display(r, system)} for r in items]}


def describe_pantry() -> str:
    data = list_pantry()
    if not data["items"]:
        return "Your pantry is empty."
    shown = [i["display"] for i in data["items"][:12]]
    more = len(data["items"]) - len(shown)
    return "In your pantry: " + _join(shown) + (f", and {more} more." if more else ".")


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1] if parts else ""


# ----------------------------------------------------------------------- changing
def split_items(items) -> list[str]:
    raw = [items] if isinstance(items, str) else list(items or [])
    out: list[str] = []
    for chunk in raw:
        out += [p.strip() for p in re.split(r"\s*(?:,|;|&|\band\b)\s*", str(chunk)) if p.strip()]
    return out


def update_pantry(action: str, items) -> dict:
    """add: adds to what is there. set: replaces the amount. remove: removes the item (or subtracts an amount)."""
    owner = current_principal()
    action = (action or "").strip().lower()
    if action not in ("add", "set", "remove"):
        raise CookingError("Action must be add, set or remove.")
    parts = split_items(items)
    if not parts:
        raise CookingError("Tell me which items.")
    if len(parts) > MAX_BATCH:
        raise CookingError(f"Please change at most {MAX_BATCH} items at a time.")
    added: list[str] = []
    changed: list[str] = []
    removed: list[str] = []
    skipped: list[dict] = []
    now = _now()
    with connect() as conn:
        system = _units(conn, owner)
        for text in parts:
            name, amount, unit = units.parse_item(text)
            key = units.norm(name)
            if not key or len(name) > 60:
                skipped.append({"item": text[:40], "reason": "I couldn't read that."})
                continue
            current = _rows(conn, owner)
            row = _find_row(current, name, exact_only=(action == "add"))

            if action == "remove":
                if row is None:
                    skipped.append({"item": name, "reason": "it isn't in your pantry"})
                    continue
                if amount and row["amount"] is not None:
                    conv = units.to_unit(amount, unit or row["unit"], row["unit"], name)
                    if conv is None:
                        skipped.append({"item": name, "reason": f"I track it in {row['unit']}, not {unit}"})
                        continue
                    left = row["amount"] - conv
                    if left > 1e-6:
                        conn.execute("UPDATE pantry SET amount=?, updated_at=? WHERE owner=? AND item=?",
                                     (round(left, 2), now, owner, row["item"]))
                        changed.append(_display({**row, "amount": round(left, 2)}, system))
                        continue
                conn.execute("DELETE FROM pantry WHERE owner=? AND item=?", (owner, row["item"]))
                removed.append(row["label"])
                continue

            if action == "set" and amount is not None and amount <= 0:
                if row:
                    conn.execute("DELETE FROM pantry WHERE owner=? AND item=?", (owner, row["item"]))
                    removed.append(row["label"])
                continue

            if row is None:
                if len(current) >= MAX_ITEMS:
                    skipped.append({"item": name, "reason": "your pantry is full"})
                    continue
                conn.execute("INSERT INTO pantry(owner, item, label, amount, unit, updated_at) VALUES (?,?,?,?,?,?)",
                             (owner, key, name, amount, unit if amount is not None else None, now))
                added.append(units.format_item(name, amount, unit, system))
                continue

            if amount is None:  # "add eggs" with no number: just make sure it is listed
                continue
            if action == "add" and row["amount"] is not None:
                conv = units.to_unit(amount, unit or row["unit"], row["unit"], name)
                if conv is None:
                    skipped.append({"item": name, "reason": f"I track it in {row['unit']}, so I can't add {unit}"})
                    continue
                new_amount, new_unit = round(row["amount"] + conv, 2), row["unit"]
            else:  # set, or adding to an item whose amount was unknown
                new_amount, new_unit = amount, unit
            conn.execute("UPDATE pantry SET amount=?, unit=?, updated_at=? WHERE owner=? AND item=?",
                         (new_amount, new_unit, now, owner, row["item"]))
            (added if action == "add" else changed).append(units.format_item(name, amount, unit, system)
                                                            if action == "add" else _display({**row, "amount": new_amount, "unit": new_unit}, system))
    sentences = []
    if added:
        sentences.append(f"Added {_join(added)} to your pantry.")
    if changed:
        sentences.append(f"Updated: {_join(changed)}.")
    if removed:
        sentences.append(f"Removed {_join(removed)} from your pantry.")
    for s in skipped:
        sentences.append(f"I couldn't change {s['item']}: {s['reason']}.")
    if not sentences:
        sentences.append("Your pantry already had that.")
    return {"status": "ok", "summary": " ".join(sentences), "skipped": skipped, **list_pantry()}


# ------------------------------------------------------- recipes vs. the pantry
def _need_in_row_unit(ing_amount: float, ing_unit: str, row: dict, name: str) -> float | None:
    return units.to_unit(ing_amount, ing_unit, row["unit"], name) if row["unit"] else None


def shortfall(pantry_rows: list[dict], recipe: dict, scale: float, system: str) -> list[dict]:
    """Ingredients the user has but not enough of, at this serving scale."""
    staples = {units.norm(s) for s in STAPLES}
    out = []
    for ing in recipe["ingredients"]:
        if units.norm(ing["name"]) in staples:
            continue
        amount, unit = ingredient_amount(ing)
        row = _find_row(pantry_rows, ing["name"])
        if amount is None or unit is None or row is None or row["amount"] is None:
            continue
        need = _need_in_row_unit(amount * scale, unit, row, ing["name"])
        if need is not None and row["amount"] < need - 1e-6:
            out.append({"item": ing["name"], "need": units.format_item(ing["name"], need, row["unit"], system),
                        "have": units.format_amount(row["amount"], row["unit"], system) if row["unit"] in ("g", "ml")
                        else units.format_item(ing["name"], row["amount"], row["unit"], system)})
    return out


def _plan(pantry_rows: list[dict], recipe: dict, scale: float, adaptations: list) -> dict:
    """What cooking this recipe would take from the pantry. Never invents amounts: anything that can't be
    compared exactly is reported as skipped, not guessed."""
    staples = {units.norm(s) for s in STAPLES}
    use, skipped = [], []
    for ing in recipe["ingredients"]:
        name = ing["name"]
        if units.norm(name) in staples:
            continue
        amount, unit = ingredient_amount(ing)
        for a in adaptations:  # a substitution means the replacement is what actually gets used
            if units.tokens_match(a["original"], name):
                name = a["replacement"]
        if amount is None or unit is None:
            skipped.append({"item": name, "reason": "the recipe gives no exact amount"})
            continue
        row = _find_row(pantry_rows, name)
        if row is None:
            skipped.append({"item": name, "reason": "not in your pantry"})
            continue
        if row["amount"] is None:
            skipped.append({"item": name, "reason": "no quantity recorded for it"})
            continue
        need = _need_in_row_unit(amount * scale, unit, row, name)
        if need is None:
            skipped.append({"item": name, "reason": f"can't compare {unit} with {row['unit']}"})
            continue
        used = min(row["amount"], need)
        use.append({"item": row["item"], "label": row["label"], "unit": row["unit"], "used": round(used, 2),
                    "left": round(row["amount"] - used, 2), "short": row["amount"] < need - 1e-6})
    return {"use": use, "skipped": skipped}


def _present(plan: dict, system: str) -> dict:
    return {"used": [units.format_item(u["label"], u["used"], u["unit"], system) + (" (all you had)" if u["short"] else "")
                     for u in plan["use"]],
            "skipped": plan["skipped"]}


# ------------------------------------------------------------ deduction workflow
def deduction_status(session_id: str) -> dict:
    ensure_session(session_id)
    owner = current_principal()
    with connect() as conn:
        row = _session(conn, session_id)
        rec = _recipe_of(row)
        if not rec or row["status"] != "completed":
            return {"state": None}
        state = row["deduction_state"] or "pending"
        out: dict = {"state": state}
        system = _units(conn, owner)
        if state == "pending":
            plan = _plan(_rows(conn, owner), rec, session_scale(row, rec), json.loads(row["adaptations_json"]))
            out.update(_present(plan, system))
        elif state == "applied":
            logs = conn.execute("SELECT * FROM pantry_log WHERE owner=? AND batch=?", (owner, row["deduction_batch"])).fetchall()
            out["used"] = [units.format_item(r["label"], r["delta"], r["unit"], system) for r in logs]
        return out


def apply_deduction(session_id: str) -> dict:
    """Subtracts the recipe's ingredients once. The claim, the pantry updates and the undo log share one transaction."""
    ensure_session(session_id)
    owner = current_principal()
    with connect() as conn:
        row = _session(conn, session_id)
        rec = _recipe_of(row)
        if not rec or row["status"] != "completed":
            raise CookingError("Finish the recipe first.")
        claimed = conn.execute("UPDATE sessions SET deduction_state='applied' WHERE id=? AND deduction_state IS NULL",
                               (session_id,)).rowcount
        if claimed != 1:
            raise CookingError("The ingredients were already handled for this recipe.")
        system = _units(conn, owner)
        plan = _plan(_rows(conn, owner), rec, session_scale(row, rec), json.loads(row["adaptations_json"]))
        batch, now = uuid.uuid4().hex, _now()
        for u in plan["use"]:
            if u["left"] <= 1e-6:
                conn.execute("DELETE FROM pantry WHERE owner=? AND item=?", (owner, u["item"]))
            else:
                conn.execute("UPDATE pantry SET amount=?, updated_at=? WHERE owner=? AND item=?", (u["left"], now, owner, u["item"]))
            conn.execute("INSERT INTO pantry_log(owner, batch, item, label, delta, unit, created_at) VALUES (?,?,?,?,?,?,?)",
                         (owner, batch, u["item"], u["label"], u["used"], u["unit"], now))
        conn.execute("UPDATE sessions SET deduction_batch=?, updated_at=? WHERE id=?", (batch, now, session_id))
    out = _present(plan, system)
    out["status"] = "subtracted"
    if not plan["use"]:
        out["note"] = "Nothing in your pantry could be matched exactly, so nothing was subtracted."
    return out


def skip_deduction(session_id: str) -> dict:
    ensure_session(session_id)
    with connect() as conn:
        n = conn.execute("UPDATE sessions SET deduction_state='skipped' WHERE id=? AND deduction_state IS NULL "
                         "AND status='completed'", (session_id,)).rowcount
    if n != 1:
        raise CookingError("There is nothing to skip.")
    return {"status": "skipped"}


def undo_deduction(session_id: str) -> dict:
    ensure_session(session_id)
    owner = current_principal()
    with connect() as conn:
        row = _session(conn, session_id)
        if row["deduction_state"] != "applied" or not row["deduction_batch"]:
            raise CookingError("There is nothing to undo.")
        system, now = _units(conn, owner), _now()
        logs = conn.execute("SELECT * FROM pantry_log WHERE owner=? AND batch=?", (owner, row["deduction_batch"])).fetchall()
        restored, lost = [], []
        for log in logs:
            cur = conn.execute("SELECT * FROM pantry WHERE owner=? AND item=?", (owner, log["item"])).fetchone()
            if cur is None:
                conn.execute("INSERT INTO pantry(owner, item, label, amount, unit, updated_at) VALUES (?,?,?,?,?,?)",
                             (owner, log["item"], log["label"], log["delta"], log["unit"], now))
            elif cur["amount"] is None:
                conn.execute("UPDATE pantry SET amount=?, unit=?, updated_at=? WHERE owner=? AND item=?",
                             (log["delta"], log["unit"], now, owner, log["item"]))
            else:
                conv = units.to_unit(log["delta"], log["unit"], cur["unit"], log["item"])
                if conv is None:
                    lost.append(log["label"])
                    continue
                conn.execute("UPDATE pantry SET amount=?, updated_at=? WHERE owner=? AND item=?",
                             (round(cur["amount"] + conv, 2), now, owner, log["item"]))
            restored.append(units.format_item(log["label"], log["delta"], log["unit"], system))
        conn.execute("DELETE FROM pantry_log WHERE owner=? AND batch=?", (owner, row["deduction_batch"]))
        conn.execute("UPDATE sessions SET deduction_state='skipped', deduction_batch=NULL, updated_at=? WHERE id=?", (now, session_id))
    return {"status": "undone", "restored": restored, "not_restored": lost}


# ------------------------------------------- plain-language commands (no model involved)
_WHERE = r"(?:pantry|fridge|freezer|cupboard|inventory)"
_ADD = re.compile(rf"^(?:please\s+|can you\s+|could you\s+)?(?:add|put|store|stock)\s+(?P<items>.+?)\s+(?:to|into|in|on)\s+(?:my\s+|the\s+){_WHERE}\s*[.!]?$", re.I)
_REMOVE = re.compile(rf"^(?:please\s+|can you\s+)?(?:remove|delete|take)\s+(?P<items>.+?)\s+(?:from|out of)\s+(?:my\s+|the\s+){_WHERE}\s*[.!]?$", re.I)
_OUT = re.compile(r"^(?:i|we)(?:'m|'re|\s+am|\s+are)?\s*(?:(?:have|'ve)\s+)?(?:just\s+)?(?:completely\s+|totally\s+)?(?:out\s+of|ran\s+out\s+of|run\s+out\s+of)\s+(?P<items>.+?)\s*[.!]?$", re.I)
_BOUGHT = re.compile(r"^(?:i|we)\s+(?:just\s+)?(?:bought|got|picked up)\s+(?P<items>.+?)\s*[.!]?$", re.I)
_SET = re.compile(rf"^(?:i|we)\s+(?:only\s+|now\s+|still\s+)?have\s+(?P<items>.+?)\s+(?:left\s+)?(?:in\s+(?:my|the)\s+{_WHERE}|left)\s*[.!]?$", re.I)
_LIST = re.compile(rf"\b(?:what(?:'s| is)?|show|list|check|read|tell me)\b[^.?!]*\b{_WHERE}\b", re.I)
_COOKING_TALK = re.compile(r"\b(cook|make|recipe|dinner|lunch|breakfast|meal|eat|what can)\b", re.I)


def parse_pantry_command(text: str) -> tuple[str, str] | None:
    """-> ('add' | 'set' | 'remove' | 'list', item text) for explicit pantry phrasings, else None (the model handles it)."""
    t = text.strip()
    if m := (_REMOVE.match(t) or _OUT.match(t)):
        return "remove", m["items"]
    if m := _ADD.match(t):
        return "add", m["items"]
    if _COOKING_TALK.search(t):  # "what can I cook with what's in my fridge?" is a recipe request
        return None
    if "?" not in t:
        if m := _BOUGHT.match(t):
            return "add", m["items"]
        if m := _SET.match(t):
            return "set", m["items"]
    if _LIST.search(t):
        return "list", ""
    return None


_YES = re.compile(r"^(?:yes|yeah|yep|yup|sure|ok(?:ay)?|please do|do it|go ahead|subtract(?: them| it| the ingredients)?|update (?:my )?pantry|yes please)\s*[.!]?$", re.I)
_NO = re.compile(r"^(?:no|nope|nah|skip(?: it)?|don'?t|leave it|not now|no thanks|no thank you)\s*[.!]?$", re.I)
_UNDO = re.compile(r"^(?:undo(?: that| it)?|put (?:them|it) back|revert(?: that)?|restore(?: them| it)?)\s*[.!]?$", re.I)


def parse_deduction_reply(text: str, state: str | None) -> str | None:
    """Short answers to the app's question. Only meaningful while a decision is pending (or to undo an applied one)."""
    t = text.strip()
    if state == "pending":
        return "apply" if _YES.match(t) else "skip" if _NO.match(t) else None
    if state == "applied" and _UNDO.match(t):
        return "undo"
    return None
