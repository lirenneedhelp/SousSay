"""The single definition of every tool. The MCP server exposes these over Streamable HTTP, and
the chat endpoint calls the very same server through an in-memory MCP client, so there is one
schema and one execution path. Access to the HTTP endpoint requires the MCP bearer key.

`session_id` is a parameter on every tool so external MCP clients can use them, but the chat
endpoint strips it from the schema shown to the model and injects the real value itself."""
import json

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request

import cooking
import pantry
from cooking import CookingError

mcp = FastMCP("TalkSous")


MCP_CLIENT_PRINCIPAL = "mcp"


def _over_http() -> bool:
    """True when this call arrived on the HTTP endpoint (which McpAuthMiddleware has already authenticated)."""
    try:
        get_http_request()
        return True
    except RuntimeError:
        return False


def _run(fn, *args, **kwargs) -> str:
    """Runs a service function as the right principal and returns JSON.
    - HTTP callers act as the single 'mcp' principal and can only touch sessions it created.
    - In-process callers (our chat endpoint) inherit the signed-in browser user's principal from the
      request context; with no principal at all, cooking.ensure_session refuses (fail closed)."""
    try:
        if _over_http():
            with cooking.acting_as(MCP_CLIENT_PRINCIPAL):
                return json.dumps(fn(*args, **kwargs))
        return json.dumps(fn(*args, **kwargs))
    except CookingError as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_pantry(session_id: str) -> str:
    """Lists what is in the user's pantry, with amounts, in their unit system."""
    return _run(lambda: pantry.list_pantry())


@mcp.tool()
def update_pantry(session_id: str, action: str, items: list[str]) -> str:
    """Changes the user's pantry. action is 'add' (adds to what is there), 'set' (replaces the amount) or 'remove'
    (removes the item, or subtracts an amount). items are plain phrases like '6 eggs' or '500 g rice'. Only call this
    when the user asks to change their pantry, and report only what the result says happened."""
    return _run(pantry.update_pantry, action, items)


@mcp.tool()
def search_recipes(session_id: str, ingredients: list[str] | None = None, max_results: int = 3) -> str:
    """Finds curated recipes ranked by how many ingredients the user has. Pass the ingredients the user
    named; leave empty to use their pantry. Returns have/missing lists and match_pct per recipe."""
    return _run(cooking.search_recipes, session_id, ingredients, max_results)


@mcp.tool()
def suggest_substitution(session_id: str, ingredient: str) -> str:
    """Suggests curated substitutes for a missing ingredient, with quantity notes and cautions."""
    return _run(cooking.suggest_substitution, session_id, ingredient)


@mcp.tool()
def apply_substitution(session_id: str, original: str, replacement: str) -> str:
    """Records, for the active recipe, that the user is using `replacement` instead of `original`."""
    return _run(cooking.apply_substitution, session_id, original, replacement)


@mcp.tool()
def create_recipe(session_id: str, title: str, ingredients: list[dict[str, str]], steps: list[str],
                  safety_notes: list[str] | None = None) -> str:
    """LAST-RESORT fallback when search_recipes has no good match. Creates an AI-generated recipe.
    ingredients: [{"name": "...", "qty": "..."}]. steps: 3 to 12 short instructions. Any meat, poultry,
    fish or egg dish MUST include a step with its safe internal temperature. The app validates it."""
    return _run(cooking.create_recipe, session_id, title, ingredients, steps, safety_notes)


@mcp.tool()
def start_recipe(session_id: str, recipe: str, servings: int | None = None) -> str:
    """Starts cooking a recipe by id or name for the given number of servings (default: the recipe's own).
    Returns scaled ingredients, any pantry shortfalls, the first step and safety notes."""
    return _run(cooking.start_recipe, session_id, recipe, servings)


@mcp.tool()
def set_servings(session_id: str, servings: int) -> str:
    """Changes how many servings the active recipe is for. Ingredient amounts scale; step text and times do not."""
    return _run(cooking.set_servings, session_id, servings)


@mcp.tool()
def pantry_deduction(session_id: str, action: str) -> str:
    """After a recipe is finished: action 'preview' is not needed (the app shows it); 'apply' subtracts the recipe's
    ingredients from the pantry once, 'skip' declines, 'undo' reverses an applied subtraction. Only call this
    when the user has answered the app's question or asked to undo."""
    actions = {"apply": pantry.apply_deduction, "skip": pantry.skip_deduction, "undo": pantry.undo_deduction}
    if action not in actions:
        return json.dumps({"error": "Action must be apply, skip or undo."})
    return _run(actions[action], session_id)


@mcp.tool()
def get_current_step(session_id: str) -> str:
    """Repeats the current step of the active recipe."""
    return _run(cooking.get_current_step, session_id)


@mcp.tool()
def next_step(session_id: str) -> str:
    """Advances to the next step of the active recipe."""
    return _run(cooking.next_step, session_id)


@mcp.tool()
def previous_step(session_id: str) -> str:
    """Goes back to the previous step of the active recipe."""
    return _run(cooking.previous_step, session_id)


@mcp.tool()
def stop_recipe(session_id: str) -> str:
    """Abandons the active recipe. Timers keep running."""
    return _run(cooking.stop_recipe, session_id)


@mcp.tool()
def set_timer(session_id: str, label: str, minutes: float = 0, seconds: int = 0) -> str:
    """Starts a named kitchen timer (e.g. 'rice', 'chicken'). Give minutes and/or seconds.
    Multiple timers can run at once. The app tracks the deadline and notifies the user when it ends."""
    return _run(cooking.set_timer, session_id, label, minutes, seconds)


@mcp.tool()
def list_timers(session_id: str) -> str:
    """Lists running and expired timers with the remaining time for each."""
    return _run(cooking.list_timers, session_id)


@mcp.tool()
def cancel_timer(session_id: str, timer_id: int | None = None, label: str | None = None) -> str:
    """Cancels a running timer, or dismisses an expired one, by id or by label."""
    return _run(cooking.cancel_timer, session_id, timer_id, label)


@mcp.tool()
def convert_measurements(session_id: str, amount: float, from_unit: str, to_unit: str,
                         ingredient_context: str = "") -> str:
    """Converts cooking units (cups, tbsp, tsp, ml, g, oz, lb) and temperatures (F, C).
    ingredient_context is needed to convert between volume and weight."""
    return _run(cooking.convert_measurements, amount, from_unit, to_unit, ingredient_context)


@mcp.tool()
def food_safety_lookup(session_id: str, food: str) -> str:
    """Looks up the safe minimum internal temperature and handling rules for a food. Use for any
    doneness or storage safety question instead of answering from memory."""
    return _run(cooking.food_safety_lookup, food)
