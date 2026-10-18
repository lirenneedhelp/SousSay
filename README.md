# Alexa+ Talk Sous

Voice-first AI sous-chef (browser demo of an Alexa+ kitchen display): recipe discovery from the
ingredients you have, hands-free step-by-step cooking, and multiple named timers.

## Run

    python -m venv venv && source venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env        # set ACCESS_CODE; use an AWS profile/role for Bedrock (Nova Lite) access
    uvicorn main:app --port 8000
    # open http://localhost:8000 in Chrome or Edge, enter your ACCESS_CODE
    # (speech recognition needs localhost or HTTPS)

The app will not start without `ACCESS_CODE` (and `SESSION_SECRET` in production). See `SECURITY.md` for the
security model and `DEPLOY.md` for HTTPS deployment, IAM permissions and operations.

Check the MCP server over real Streamable HTTP (no AWS needed): set `MCP_API_KEY` on the server, export the
same value, then `python bedrock_agent.py --smoke`

## Tests

    pip install -r requirements-dev.txt && pytest

The suite uses a scripted fake Bedrock, so it needs no AWS access. It does NOT test the real model.

## How it fits together

    browser (Web Speech + text box) -> POST /api/chat {message, session_id}
      -> main.run_turn: history + authoritative state in the prompt -> Bedrock Converse (Nova Lite)
      -> every toolUse executed through an in-memory MCP client against the SAME FastMCP server
      -> cooking.py (SQLite) -> reply text -> speechSynthesis
    browser polls GET /api/state every 2s: timer countdowns + one-time expiry notifications

- `mcp_tools.py`  the single definition of all 15 tools, served at `/mcp/` (Streamable HTTP).
- `cooking.py`    all logic and state: recipes, sessions, steps, timers, substitutions, safety.
- `db.py`         SQLite schema (`sessions`, `timers`, `messages`, `generated_recipes`).
- `data/`         curated recipes, substitutions and food-safety temperatures (edit the JSON to extend).

Design rules: the app, not the model, owns step position and timer deadlines (`due_at` is an
absolute timestamp; expiry is computed on read). The model never sees or supplies `session_id`.
AI-generated recipes are a last resort, validated by the app (shape limits, mandatory safe-temperature
step for meat/poultry/fish/eggs, unsafe-practice blocklist) and always labelled.

## Known limits

- Demo pantry is mocked (`cooking.MOCK_PANTRY`); there is no inventory editing.
- One shared access code, not per-user accounts. Run a single worker. See `SECURITY.md` for remaining risks.
- Timer alerts only fire while the page is open; background tabs may throttle polling and speech.

## Manual demo script (requires Bedrock)

1. "What can I cook?"  2. "I have eggs, canned tomatoes and onions."  3. "Let's make the shakshuka."
4. "Next step."  5. "Set a 20 second timer for the eggs."  6. "How long is left?"  7. "Repeat that."
8. "Is it safe to eat chicken at 150 degrees?"  9. "I don't have an onion." (substitution)
