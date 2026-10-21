import asyncio
import copy
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, ConnectTimeoutError, NoCredentialsError, ReadTimeoutError
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastmcp import Client
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

import cooking
import db
from cooking import CookingError, SessionNotFound
from mcp_tools import mcp
from security import (COOKIE_NAME, BodyLimitMiddleware, McpAuthMiddleware, RateLimiter, SecurityHeadersMiddleware,
                      codes_match, issue_cookie, load_settings, origin_allowed, verify_cookie)

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("talksous")
BASE_DIR = Path(__file__).parent

settings = load_settings()  # refuses to start without ACCESS_CODE (and SESSION_SECRET in production)
limiter = RateLimiter()

MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
MAX_TOOL_ITERATIONS = 6
SESSION_PATTERN = r"^[A-Za-z0-9_-]{8,64}$"

# Credentials come from the standard AWS provider chain (IAM role, profile, or environment), never from code.
bedrock = boto3.client(
    "bedrock-runtime",
    region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
    config=Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 2, "mode": "standard"}),
)

# Self-hosted MCP server (Streamable HTTP) mounted at /mcp/. The chat endpoint below talks to the
# SAME server through an in-memory client, so there is no HTTP hop to ourselves (no deadlock).
mcp_app = mcp.http_app(
    transport="streamable-http", path="/",
    allowed_hosts=list(settings.allowed_hosts), allowed_origins=list(settings.allowed_origins),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    async with mcp_app.lifespan(app):
        yield


# No interactive docs or OpenAPI schema in any environment: they would advertise every route.
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/mcp", mcp_app)
# add_middleware stacks outward: TrustedHost runs first, then headers, body limit, MCP auth.
app.add_middleware(McpAuthMiddleware, get_settings=lambda: settings, limiter=limiter)
app.add_middleware(BodyLimitMiddleware, get_settings=lambda: settings)
app.add_middleware(SecurityHeadersMiddleware, get_settings=lambda: settings)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
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

Timers: never say you set, started, cancelled, or have a timer unless a tool result in this turn confirmed it or the state block lists it. Earlier conversation messages may be wrong about timers; the state block is the truth. To start a timer you must call set_timer; to cancel one you must call cancel_timer.

Food safety: for any doneness, temperature, or storage question call food_safety_lookup instead of guessing. Never suggest undercooked poultry or ground meat, or leaving perishable food out more than 2 hours. If a tool returns an error, say so briefly in plain words and do not make up a result. If unsure, say so and suggest checking USDA guidance."""


# --------------------------------------------------------------------- helpers
def client_ip(request: Request) -> str:
    """Real client IP only if uvicorn was started with --proxy-headers behind a trusted proxy."""
    return request.client.host if request.client else "unknown"


def require_user(request: Request) -> str:
    """Authenticates the signed cookie and returns the caller's principal."""
    client_id = verify_cookie(settings, request.cookies.get(COOKIE_NAME))
    if not client_id:
        raise HTTPException(status_code=401, detail="Please sign in.")
    return f"u:{client_id}"


def require_same_origin(request: Request) -> None:
    if not origin_allowed(settings, request.headers.get("origin"), request.headers.get("host")):
        raise HTTPException(status_code=403, detail="Cross-origin request blocked.")


def too_many(detail: str = "Too many requests. Please slow down.") -> HTTPException:
    return HTTPException(status_code=429, detail=detail, headers={"Retry-After": "30"})


def to_http_error(exc: Exception) -> HTTPException:
    """Maps any failure in a chat turn to a safe, useful response. Details go to the server log only."""
    if isinstance(exc, SessionNotFound):
        return HTTPException(status_code=404, detail="Unknown session.")
    if isinstance(exc, CookingError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, asyncio.TimeoutError):
        log.error("Chat turn timed out after %ss", settings.turn_timeout_s)
        return HTTPException(status_code=504, detail="That took too long. Please try again.")
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        # The message explains the cause (bad model id, region, malformed request). It is logged server-side
        # only, truncated; users still just see the generic response.
        log.error("Bedrock error: %s: %s", code or "unknown", str(exc.response.get("Error", {}).get("Message", ""))[:300])
        if code in ("ThrottlingException", "TooManyRequestsException", "ServiceQuotaExceededException"):
            return too_many("The assistant is busy. Please try again in a moment.")
        if code in ("AccessDeniedException", "ResourceNotFoundException", "ValidationException",
                    "UnrecognizedClientException", "ExpiredTokenException", "UnauthorizedException"):
            return HTTPException(status_code=503, detail="The assistant is not set up correctly. Please tell the host.")
        if code == "ModelTimeoutException":
            return HTTPException(status_code=504, detail="The assistant took too long. Please try again.")
        return HTTPException(status_code=503, detail="The assistant is temporarily unavailable. Please try again.")
    if isinstance(exc, NoCredentialsError):
        log.error("Bedrock call failed: no AWS credentials found")
        return HTTPException(status_code=503, detail="The assistant is not set up correctly. Please tell the host.")
    if isinstance(exc, (ReadTimeoutError, ConnectTimeoutError)):
        log.error("Bedrock call timed out: %s", type(exc).__name__)
        return HTTPException(status_code=504, detail="The assistant took too long. Please try again.")
    if isinstance(exc, (KeyError, TypeError, IndexError)):
        log.exception("Unusable model response")
        return HTTPException(status_code=502, detail="The assistant gave an unusable answer. Please try again.")
    if isinstance(exc, BotoCoreError):
        log.error("Bedrock transport error: %s", type(exc).__name__)
        return HTTPException(status_code=503, detail="The assistant is temporarily unavailable. Please try again.")
    log.exception("Unexpected chat failure")
    return HTTPException(status_code=502, detail="Something went wrong. Please try again.")


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


_COMMAND = re.compile(r"\b(set|start|cancel|stop|delete|remove|dismiss|add|create|make|reset|pause|need|should|recommend|suggest|recipe)\b"
                      r"|\d+\s*(min|sec|hour|hr)|\b(an?|one|two|three|five|ten|fifteen|twenty|thirty)\s+(\w+\s+)?(minutes?|seconds?|hours?)\b", re.I)
_TIMER_STATUS = re.compile(
    r"\b(do|did|have) i (still )?have\b[^.?!]*\btimers?\b|\b(what|which|any)\b[^.?!]*\btimers?\b"
    r"|\bwhere('?s| is| are)\b[^.?!]*\btimers?\b|\b(check|status of|list|show)\b[^.?!]*\btimers?\b"
    r"|\b(is|are)\b[^.?!]*\btimers?\b[^.?!]*\b(running|done|going|ready|still|finished)\b"
    r"|\bhow (much|long)\b[^.?!]*\b(left|longer|remaining)\b|\btime (left|remaining)\b", re.I)


def is_timer_status_query(text: str) -> bool:
    """'Do I have any timers?' / 'how long left?' are answered from the database, not by the model."""
    return bool(_TIMER_STATUS.search(text)) and not _COMMAND.search(text)


_OFFER = re.compile(r"\?|\b(would you like|want me to|shall i|should i|do you want)\b", re.I)
_SET_CLAIM = re.compile(r"\b(i['’]?ve|i have|i|just|already)\s+(set|started|created)\b[^.?!]{0,60}\btimer"
                        r"|\btimer\b[^.?!]{0,40}\b(is|has been|was|'s)\s+(now\s+)?(set|started|running)\b"
                        r"|\b(setting|starting)\s+(a|the|your)\b[^.?!]{0,30}\btimer", re.I)
_CANCEL_CLAIM = re.compile(r"\b(i['’]?ve|i have|i)\s+(cancel+ed|stopped|dismissed|removed)\b[^.?!]{0,40}\btimer"
                           r"|\btimer\b[^.?!]{0,40}\b(is|has been|was)\s+(now\s+)?(cancel+ed|stopped|dismissed|removed)\b", re.I)


def guard_unbacked_claims(reply: str, ok_tools: set[str]) -> str:
    """The model sometimes says it did something it never did. If it claims to have set or cancelled a timer
    without a successful tool call this turn, replace the claim with an honest message."""
    if _OFFER.search(reply):
        return reply
    if _SET_CLAIM.search(reply) and "set_timer" not in ok_tools:
        log.warning("Replaced an unbacked 'timer set' claim")
        return "I couldn't start that timer. Please say it again with a name and a length, like: set a 15 minute timer for rice."
    if _CANCEL_CLAIM.search(reply) and "cancel_timer" not in ok_tools:
        log.warning("Replaced an unbacked 'timer cancelled' claim")
        return "I couldn't cancel that timer. Say which one, like: cancel the rice timer."
    return reply


def clean_spoken_text(raw: str) -> str:
    text = re.sub(r"<thinking>.*?</thinking>", "", raw, flags=re.DOTALL)
    text = re.sub(r"</?thinking>", "", text)
    text = re.sub(r"[*#`]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


async def run_turn(session_id: str, user_text: str) -> tuple[str, list[str]]:
    cooking.ensure_session(session_id)
    if is_timer_status_query(user_text):
        reply = cooking.describe_timers(session_id)
        cooking.add_messages(session_id, [("user", user_text), ("assistant", reply)])
        return reply, []
    messages = cooking.load_history(session_id) + [{"role": "user", "content": [{"text": user_text}]}]
    system = [{"text": SYSTEM_PROMPT + "\n\n" + cooking.state_summary(session_id)}]
    tools = await get_bedrock_tools()
    known = {t["toolSpec"]["name"] for t in tools}
    tools_called: list[str] = []
    ok_tools: set[str] = set()
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
                if results[-1]["toolResult"]["status"] == "success":
                    ok_tools.add(str(tool_use.get("name")))
            messages.append({"role": "user", "content": results})

    reply = guard_unbacked_claims(reply, ok_tools)
    cooking.add_messages(session_id, [("user", user_text), ("assistant", reply)])
    return reply, tools_called


# ---------------------------------------------------------------------- routes
@app.get("/")
async def serve_ui():
    index_path = BASE_DIR / "static" / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="static/index.html not found.")
    return FileResponse(index_path, headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
async def health():
    """Liveness plus a database check. Reveals nothing about configuration or infrastructure."""
    try:
        ok = await run_in_threadpool(db.ping)
    except Exception:
        log.exception("Health check failed")
        ok = False
    return JSONResponse({"status": "ok" if ok else "degraded"}, status_code=200 if ok else 503)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields are rejected, not ignored
    code: str = Field(min_length=1, max_length=128)


@app.post("/api/login", dependencies=[Depends(require_same_origin)])
async def login(req: LoginRequest, request: Request, response: Response):
    if not limiter.allow(f"login:{client_ip(request)}", settings.login_per_min):
        raise too_many("Too many sign-in attempts. Wait a minute and try again.")
    if not codes_match(req.code, settings.access_code):
        raise HTTPException(status_code=401, detail="Incorrect access code.")
    response.set_cookie(COOKIE_NAME, issue_cookie(settings), max_age=settings.cookie_ttl_s, httponly=True,
                        secure=settings.cookie_secure, samesite="strict", path="/")
    return {"ok": True}


@app.post("/api/logout", dependencies=[Depends(require_same_origin)])
async def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=settings.cookie_secure, samesite="strict")
    return {"ok": True}


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields are rejected, not ignored
    message: str = Field(min_length=1, max_length=500)
    session_id: str = Field(pattern=SESSION_PATTERN)


@app.post("/api/chat", dependencies=[Depends(require_same_origin)])
async def chat_endpoint(req: ChatRequest, request: Request, principal: str = Depends(require_user)):
    # Model calls are the expensive, abusable operation: limit per user, per IP, and globally per hour.
    if not (limiter.allow(f"chat:{principal}", settings.chat_per_min)
            and limiter.allow(f"chat-ip:{client_ip(request)}", settings.chat_per_min * 3)):
        raise too_many()
    if not limiter.allow("chat-global", settings.global_chat_per_hour, 3600):
        raise too_many("The demo has reached its hourly limit. Please try again later.")
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="Message is empty.")
    try:
        with cooking.acting_as(principal):
            reply, tools_called = await asyncio.wait_for(run_turn(req.session_id, message), settings.turn_timeout_s)
            state = cooking.get_state(req.session_id)
    except Exception as exc:
        raise to_http_error(exc)
    return {
        "response": reply,
        "tool_called": tools_called[-1] if tools_called else None,
        "tools_called": tools_called,
        "state": state,
    }


@app.get("/api/state")
def state_endpoint(session_id: str = Query(pattern=SESSION_PATTERN), pop_expired: bool = True,
                   principal: str = Depends(require_user)):
    """Polled by the UI. Timer expiry is computed from stored deadlines on every read, and each
    expiry is reported once in `newly_expired`."""
    if not limiter.allow(f"state:{principal}", settings.state_per_min):
        raise too_many()
    try:
        with cooking.acting_as(principal):
            return cooking.get_state(session_id, pop_expired=pop_expired)
    except Exception as exc:
        raise to_http_error(exc)


class DismissRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields are rejected, not ignored
    session_id: str = Field(pattern=SESSION_PATTERN)
    timer_id: int = Field(ge=1, le=2**62)


@app.post("/api/timers/cancel", dependencies=[Depends(require_same_origin)])
def cancel_timer_endpoint(req: DismissRequest, principal: str = Depends(require_user)):
    if not limiter.allow(f"cancel:{principal}", 60):
        raise too_many()
    try:
        with cooking.acting_as(principal):
            return cooking.cancel_timer(req.session_id, timer_id=req.timer_id)
    except Exception as exc:
        raise to_http_error(exc)