import copy
import json
import logging
import os
import re
import time
from typing import Any
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

import boto3
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastmcp import Client
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

import cooking
from cooking import CookingError
from db import init_db
from mcp_tools import mcp

load_dotenv()
log = logging.getLogger("talksous")
BASE_DIR = Path(__file__).parent

MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
MAX_TOOL_ITERATIONS = 6
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "30"))
SESSION_PATTERN = r"^[A-Za-z0-9_-]{8,64}$"

bedrock = boto3.client("bedrock-runtime", region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"))

# Self-hosted MCP server (Streamable HTTP) mounted at /mcp/. The chat endpoint below talks to the
# SAME server through an in-memory client, so there is no HTTP hop to ourselves (no deadlock).
mcp_app = mcp.http_app(transport="streamable-http", path="/")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    async with mcp_app.lifespan(app):
        yield


app = FastAPI(lifespan=lifespan)
app.mount("/mcp", mcp_app)
(BASE_DIR / "static").mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

SYSTEM_PROMPT = """You are Talk Sous, a hands-free voice sous-chef on an Alexa+ kitchen display.

Style: your words are spoken aloud. Use at most 2 short sentences, except when reading a recipe step or ingredient list. No markdown, lists, or emojis.

State: the CURRENT KITCHEN STATE block below is authoritative and maintained by the app. Trust it over your memory. Never invent timer values, step numbers, or recipe content; use tools.

Workflow:
- When asked what to cook, or when the user names ingredients, call search_recipes (pass the ingredients they named, or none to use their pantry). Offer the best one or two by name and say what is missing. For a missing ingredient, call suggest_substitution.
- Only if search_recipes finds no good match (nothing, or match_pct under 60) may you call create_recipe. Tell the user it is an AI-generated recipe and ask before starting it.
- When the user picks a recipe, call start_recipe, read the first step, and mention any safety note briefly.
- "Next" means next_step, "repeat" means get_current_step, "go back" means previous_step. If the user says they use a different ingredient, call apply_substitution.
- If a step has a timer_hint, offer it in one short sentence, and call set_timer when the user agrees. Also call set_timer whenever the user asks for a timer, with a clear name. Convert spoken durations to minutes and seconds yourself.
- Use list_timers to report remaining time and cancel_timer to cancel or dismiss one. Several timers can run at once.
- Questions during cooking: answer from the state and the recipe. If the user interrupts ("hold on", "the phone is ringing"), do not advance; say you will wait. Timers keep running.

Food safety: for any doneness, temperature, or storage question call food_safety_lookup instead of guessing. Never suggest undercooked poultry or ground meat, or leaving perishable food out more than 2 hours. If a tool returns an error, say so briefly in plain words and do not make up a result. If unsure, say so and suggest checking USDA guidance."""


# --------------------------------------------------------------------- helpers
_hits: dict[str, deque] = defaultdict(deque)


def check_rate_limit(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    now, window = time.monotonic(), _hits[ip]
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= RATE_LIMIT_PER_MIN:
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")
    window.append(now)


def _simplify_schema(node: Any) -> Any:
    """Bedrock/Nova handle plain JSON schema best: collapse Optional (anyOf with null) and drop noise."""
    if isinstance(node, dict):
        if "anyOf" in node:
            options = [o for o in node["anyOf"] if o.get("type") != "null"]
            if len(options) == 1:
                rest = {k: v for k, v in node.items() if k != "anyOf"}
                node = {**options[0], **rest}
        return {k: _simplify_schema(v) for k, v in node.items()
                if k not in ("title", "default", "additionalProperties")}
    if isinstance(node, list):
        return [_simplify_schema(v) for v in node]
    return node


_tool_specs: list | None = None


async def get_bedrock_tools() -> list:
    """Builds Bedrock tool specs from the MCP server's own tool list. session_id is hidden from the model."""
    global _tool_specs
    if _tool_specs is None:
        async with Client(mcp) as client:
            tools = await client.list_tools()
        specs = []
        for tool in tools:
            raw = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}
            schema: dict[str, Any] = _simplify_schema(copy.deepcopy(raw))
            schema.setdefault("type", "object")
            schema.setdefault("properties", {})
            schema["properties"].pop("session_id", None)
            schema["required"] = [r for r in schema.get("required", []) if r != "session_id"]
            if not schema["required"]:
                schema.pop("required")
            specs.append({"toolSpec": {"name": tool.name, "description": tool.description or "",
                                       "inputSchema": {"json": schema}}})
        _tool_specs = specs
    return _tool_specs


async def execute_tool(client: Client, tool_use: dict, session_id: str, known: set) -> dict:
    """Runs one toolUse through MCP. Always returns a toolResult block; failures are reported as errors."""
    name = str(tool_use.get("name", ""))
    block = {"toolUseId": tool_use["toolUseId"]}
    if name not in known:
        return {"toolResult": {**block, "status": "error", "content": [{"text": '{"error": "Unknown tool"}'}]}}
    args = dict(tool_use.get("input") or {})
    args["session_id"] = session_id  # always server-controlled, never model-controlled
    try:
        result = await client.call_tool(name, args, raise_on_error=False)
        text = "".join(getattr(c, "text", "") for c in result.content)
        if result.is_error:
            log.warning("Tool %s failed: %s", name, text)
            return {"toolResult": {**block, "status": "error",
                                   "content": [{"text": json.dumps({"error": text[:200]})}]}}
        try:
            failed = isinstance(json.loads(text), dict) and "error" in json.loads(text)
        except ValueError:
            failed = False
        return {"toolResult": {**block, "status": "error" if failed else "success", "content": [{"text": text}]}}
    except Exception:
        log.exception("Tool %s crashed", name)
        return {"toolResult": {**block, "status": "error",
                               "content": [{"text": '{"error": "The tool failed unexpectedly."}'}]}}


def clean_spoken_text(raw: str) -> str:
    text = re.sub(r"<thinking>.*?</thinking>", "", raw, flags=re.DOTALL)
    text = re.sub(r"</?thinking>", "", text)
    text = re.sub(r"[*#`]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


async def run_turn(session_id: str, user_text: str) -> tuple[str, list[str]]:
    cooking.ensure_session(session_id)
    messages = cooking.load_history(session_id) + [{"role": "user", "content": [{"text": user_text}]}]
    system = [{"text": SYSTEM_PROMPT + "\n\n" + cooking.state_summary(session_id)}]
    tools = await get_bedrock_tools()
    known = {t["toolSpec"]["name"] for t in tools}
    tools_called: list[str] = []
    reply = "Sorry, that took too many steps. Could you say it again?"

    async with Client(mcp) as client:
        for _ in range(MAX_TOOL_ITERATIONS):
            response = await run_in_threadpool(
                lambda: bedrock.converse(
                    modelId=MODEL_ID, messages=messages, system=system, toolConfig={"tools": tools},
                    inferenceConfig={"maxTokens": 512, "temperature": 0.3},
                )
            )
            output = response["output"]["message"]
            messages.append(output)
            blocks = output.get("content", [])
            tool_uses = [b["toolUse"] for b in blocks if "toolUse" in b]
            if not tool_uses:
                text = clean_spoken_text(" ".join(b["text"] for b in blocks if "text" in b))
                reply = text or "Done. What would you like to do next?"
                break
            results = []
            for tool_use in tool_uses:  # every toolUse needs a matching toolResult in one user message
                tools_called.append(tool_use.get("name", "?"))
                results.append(await execute_tool(client, tool_use, session_id, known))
            messages.append({"role": "user", "content": results})

    cooking.add_messages(session_id, [("user", user_text), ("assistant", reply)])
    return reply, tools_called


# ---------------------------------------------------------------------- routes
@app.get("/")
async def serve_ui():
    index_path = BASE_DIR / "static" / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="static/index.html not found.")
    return FileResponse(index_path)


@app.get("/api/health")
def health():
    return {"ok": True}


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    session_id: str = Field(pattern=SESSION_PATTERN)


@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest, request: Request):
    check_rate_limit(request)
    if not req.message.strip():
        raise HTTPException(status_code=422, detail="Message is empty.")
    try:
        reply, tools_called = await run_turn(req.session_id, req.message.strip())
    except CookingError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        log.exception("chat failed")
        raise HTTPException(status_code=502, detail="The assistant is unavailable right now. Please try again.")
    return {
        "response": reply,
        "tool_called": tools_called[-1] if tools_called else None,
        "tools_called": tools_called,
        "state": cooking.get_state(req.session_id),
    }


@app.get("/api/state")
def state_endpoint(session_id: str = Query(pattern=SESSION_PATTERN), pop_expired: bool = True):
    """Polled by the UI. Timer expiry is computed from stored deadlines on every read, and each
    expiry is reported once in `newly_expired`."""
    try:
        return cooking.get_state(session_id, pop_expired=pop_expired)
    except CookingError as e:
        raise HTTPException(status_code=400, detail=str(e))


class DismissRequest(BaseModel):
    session_id: str = Field(pattern=SESSION_PATTERN)
    timer_id: int


@app.post("/api/timers/cancel")
def cancel_timer_endpoint(req: DismissRequest):
    try:
        return cooking.cancel_timer(req.session_id, timer_id=req.timer_id)
    except CookingError as e:
        raise HTTPException(status_code=400, detail=str(e))