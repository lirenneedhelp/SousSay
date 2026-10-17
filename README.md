# Alexa+ Talk Sous

Voice-first AI sous-chef (browser demo of an Alexa+ kitchen display): recipe discovery from the
ingredients you have, hands-free step-by-step cooking, and multiple named timers.

## Run

    python -m venv venv && source venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env        # add AWS credentials with Bedrock access to Amazon Nova Lite
    uvicorn main:app --port 8000
    # open http://localhost:8000 in Chrome or Edge (speech recognition needs localhost or HTTPS)

Check the MCP server over real Streamable HTTP (no AWS needed): `python bedrock_agent.py --smoke`

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
- No authentication. `/api/chat` has a per-IP rate limit and length cap, but anyone who can reach the
  server can use your Bedrock quota, and `/mcp/` is open. Bind to localhost or put it behind auth/VPN
  before exposing it. Behind a reverse proxy the rate limiter sees one IP.
- Rate limit and Bedrock client are per-process; run a single worker.
- `/mcp` (no trailing slash) redirects with 307; MCP clients should use `/mcp/`.
- The Tailwind Play CDN needs internet on the demo device (fine for a demo, not for production).
- Timer alerts only fire while the page is open; background tabs may throttle polling and speech.

## Manual demo script (requires Bedrock)

1. "What can I cook?"  2. "I have eggs, canned tomatoes and onions."  3. "Let's make the shakshuka."
4. "Next step."  5. "Set a 20 second timer for the eggs."  6. "How long is left?"  7. "Repeat that."
8. "Is it safe to eat chicken at 150 degrees?"  9. "I don't have an onion." (substitution)
