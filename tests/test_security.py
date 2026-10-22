import asyncio
import logging
import os
import re
import socket
import sqlite3
import stat
import threading
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import uvicorn
from botocore.exceptions import ClientError, NoCredentialsError, ReadTimeoutError
from fastmcp import Client

import cooking
import db
import main
from conftest import SID, TEST_ENV, FakeBedrock, make_client, text, tools
from cooking import CookingError, SessionNotFound
from security import COOKIE_NAME, ConfigError, issue_cookie, load_settings, verify_cookie

ROOT = Path(__file__).resolve().parent.parent
GOOD = {"ACCESS_CODE": "a-long-access-code", "SESSION_SECRET": "x" * 32}


def use_bedrock(monkeypatch, script, **kw):
    fake = FakeBedrock(script, **kw)
    monkeypatch.setattr(main, "bedrock", fake)
    return fake


def chat(client, message="hello", session_id=SID, **kw):
    return client.post("/api/chat", json={"message": message, "session_id": session_id}, **kw)


def with_settings(monkeypatch, **changes):
    monkeypatch.setattr(main, "settings", replace(main.settings, **changes))


# ===================================================================== configuration
def test_startup_requires_access_code():
    for env in ({}, {"SESSION_SECRET": "x" * 32}, {"ACCESS_CODE": "short", "SESSION_SECRET": "x" * 32}):
        with pytest.raises(ConfigError):
            load_settings(env)


def test_production_requires_strong_session_secret():
    with pytest.raises(ConfigError):
        load_settings({"ACCESS_CODE": "a-long-access-code"})
    with pytest.raises(ConfigError):
        load_settings({"ACCESS_CODE": "a-long-access-code", "SESSION_SECRET": "too-short"})


def test_production_defaults_are_secure():
    s = load_settings(GOOD)
    assert s.app_env == "production" and s.cookie_secure is True
    assert s.mcp_api_key is None  # /mcp is disabled unless a key is configured
    assert s.allowed_hosts == ("localhost", "127.0.0.1")


def test_development_generates_secret_and_allows_http_cookie():
    s = load_settings({"APP_ENV": "development", "ACCESS_CODE": "abcdef"})
    assert len(s.session_secret) >= 32 and s.cookie_secure is False


def test_invalid_env_and_weak_mcp_key_rejected():
    with pytest.raises(ConfigError):
        load_settings({**GOOD, "APP_ENV": "staging"})
    with pytest.raises(ConfigError):
        load_settings({**GOOD, "MCP_API_KEY": "short"})


# ========================================================================== cookies
def test_cookie_roundtrip_tamper_expiry_and_wrong_secret():
    s = main.settings
    value = issue_cookie(s, "alice")
    assert verify_cookie(s, value) == "alice"
    assert verify_cookie(s, value[:-2] + ("AA" if not value.endswith("AA") else "BB")) is None
    assert verify_cookie(s, value.replace("alice", "mallory", 1)) is None
    assert verify_cookie(replace(s, session_secret="y" * 40), value) is None
    assert verify_cookie(s, issue_cookie(s, "alice", now=time.time() - s.cookie_ttl_s - 10)) is None
    for junk in (None, "", "a.b", "a.b.c.d", "x" * 500, "..", "alice.notanumber.sig"):
        assert verify_cookie(s, junk) is None


# ============================================================= authentication / login
def test_protected_routes_reject_anonymous_requests():
    anon = make_client(None)
    assert chat(anon).status_code == 401
    assert anon.get("/api/state", params={"session_id": SID}).status_code == 401
    assert anon.post("/api/timers/cancel", json={"session_id": SID, "timer_id": 1}).status_code == 401


def test_forged_tampered_expired_and_foreign_cookies_are_rejected(monkeypatch):
    s = main.settings
    anon = make_client(None)
    bad = [issue_cookie(s, "alice")[:-3] + "xyz",
           issue_cookie(replace(s, session_secret="z" * 40), "alice"),
           issue_cookie(s, "alice", now=time.time() - s.cookie_ttl_s - 5),
           "alice.9999999999.forged"]
    for cookie in bad:
        r = anon.get("/api/state", params={"session_id": SID}, headers={"Cookie": f"{COOKIE_NAME}={cookie}"})
        assert r.status_code == 401


def test_login_wrong_code_gives_no_cookie_and_generic_error():
    anon = make_client(None)
    r = anon.post("/api/login", json={"code": "wrong-code"})
    assert r.status_code == 401 and "set-cookie" not in r.headers and r.json()["detail"] == "Incorrect access code."


def test_login_success_sets_hardened_cookie_and_unlocks_api(monkeypatch):
    with_settings(monkeypatch, cookie_secure=True)
    anon = make_client(None)
    r = anon.post("/api/login", json={"code": TEST_ENV["ACCESS_CODE"]})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie and "max-age" in cookie
    assert TEST_ENV["ACCESS_CODE"] not in r.headers["set-cookie"] and TEST_ENV["SESSION_SECRET"] not in cookie
    assert "strict-transport-security" in r.headers
    value = r.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    assert anon.get("/api/state", params={"session_id": SID}, headers={"Cookie": f"{COOKIE_NAME}={value}"}).status_code == 200


def test_login_is_rate_limited(monkeypatch):
    with_settings(monkeypatch, login_per_min=3)
    anon = make_client(None)
    codes = [anon.post("/api/login", json={"code": f"guess-{i}"}).status_code for i in range(5)]
    assert codes == [401, 401, 401, 429, 429]
    assert anon.post("/api/login", json={"code": "x"}).headers.get("retry-after")


def test_login_validates_payload():
    anon = make_client(None)
    for body in ({}, {"code": ""}, {"code": "x" * 129}, {"code": 123}, {"nope": 1}):
        assert anon.post("/api/login", json=body).status_code == 422
    assert anon.post("/api/login", content=b"not json", headers={"content-type": "application/json"}).status_code == 422


def test_logout_clears_cookie():
    r = make_client().post("/api/logout")
    assert r.status_code == 200 and "max-age=0" in r.headers["set-cookie"].lower()


# ===================================================== cross-user isolation (sessions)
def test_users_cannot_read_modify_or_cancel_each_others_sessions(monkeypatch, clock):
    alice, bob = make_client("alice"), make_client("bob")
    use_bedrock(monkeypatch, [tools(("start_recipe", {"recipe": "shakshuka"}), ("set_timer", {"label": "eggs", "minutes": 5})), text("ok")])
    assert chat(alice).status_code == 200
    timer_id = alice.get("/api/state", params={"session_id": SID}).json()["timers"][0]["id"]

    r = bob.get("/api/state", params={"session_id": SID})
    assert r.status_code == 404 and "shakshuka" not in r.text.lower() and "eggs" not in r.text.lower()

    spy = use_bedrock(monkeypatch, [text("must never run")])
    assert chat(bob, "what is she cooking?").status_code == 404
    assert spy.calls == []  # rejected before any model call is paid for

    assert bob.post("/api/timers/cancel", json={"session_id": SID, "timer_id": timer_id}).status_code == 404

    after = alice.get("/api/state", params={"session_id": SID}).json()
    assert after["session"]["recipe"]["id"] == "shakshuka" and after["timers"][0]["status"] == "running"

    use_bedrock(monkeypatch, [text("hello bob")])
    assert chat(bob, "hi", session_id="bobs-own-session-1").status_code == 200


def test_unknown_and_foreign_sessions_are_indistinguishable():
    with cooking.acting_as("u:alice"):
        cooking.start_recipe(SID, "shakshuka")
    bob = make_client("bob")
    foreign = bob.get("/api/state", params={"session_id": SID})
    fresh = make_client("bob").get("/api/state", params={"session_id": "never-seen-before-1"})
    assert foreign.status_code == 404 and fresh.status_code == 200  # fresh ids are simply created for the caller


def test_service_layer_fails_closed_without_a_principal():
    token = cooking._principal.set(None)
    try:
        for fn in (lambda: cooking.list_timers(SID), lambda: cooking.start_recipe(SID, "shakshuka"),
                   lambda: cooking.load_history(SID), lambda: cooking.pop_newly_expired(SID),
                   lambda: cooking.get_state(SID)):
            with pytest.raises(SessionNotFound):
                fn()
    finally:
        cooking._principal.reset(token)


def test_session_ownership_is_enforced_in_every_service_function():
    with cooking.acting_as("u:alice"):
        cooking.start_recipe(SID, "shakshuka")
        cooking.set_timer(SID, "eggs", minutes=5)
    with cooking.acting_as("u:bob"):
        calls = [lambda: cooking.next_step(SID), lambda: cooking.list_timers(SID), lambda: cooking.set_timer(SID, "x", minutes=1),
                 lambda: cooking.cancel_timer(SID, timer_id=1), lambda: cooking.stop_recipe(SID),
                 lambda: cooking.search_recipes(SID, ["egg"]), lambda: cooking.add_messages(SID, [("user", "hi")]),
                 lambda: cooking.apply_substitution(SID, "egg", "tofu"), lambda: cooking.get_state(SID, pop_expired=True)]
        for call in calls:
            with pytest.raises(SessionNotFound):
                call()
    with cooking.acting_as("u:alice"):
        assert cooking.list_timers(SID)["timers"][0]["label"] == "eggs"


def test_per_principal_session_cap_rolls_back(monkeypatch):
    monkeypatch.setattr(cooking, "MAX_SESSIONS_PER_PRINCIPAL", 3)
    with cooking.acting_as("u:capped"):
        for i in range(3):
            cooking.ensure_session(f"capped-session-{i}")
        with pytest.raises(CookingError):
            cooking.ensure_session("capped-session-3")
    with sqlite3.connect(db.db_path()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE owner='u:capped'").fetchone()[0] == 3


# ================================================================ request hardening
def test_interactive_docs_and_schema_are_not_exposed():
    c = make_client()
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert c.get(path).status_code == 404


def test_security_headers_present_and_csp_is_strict():
    r = make_client().get("/")
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp and "http" not in csp.replace("https:", "")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer" and "microphone=(self)" in r.headers["permissions-policy"]
    assert make_client().get("/api/health").headers["cache-control"] == "no-store"
    assert "strict-transport-security" not in r.headers  # only when served over HTTPS


def test_index_is_self_contained_for_the_csp():
    html = (ROOT / "static" / "index.html").read_text()
    assert "<script>" not in html and not re.search(r"\son\w+=", html) and "style=" not in html
    assert not re.search(r"https?://", html), "page must not load third-party resources"


def test_unexpected_host_header_is_rejected():
    r = make_client().get("/api/health", headers={"Host": "evil.example"})
    assert r.status_code == 400


def test_oversized_bodies_are_rejected_before_parsing():
    c = make_client()
    assert c.post("/api/chat", content=b"a" * 20_000, headers={"content-type": "application/json"}).status_code == 413
    streamed = iter([b'{"message": "', b"a" * 12_000, b"b" * 12_000, b'"}'])  # chunked: no Content-Length to trust
    assert c.post("/api/chat", content=streamed, headers={"content-type": "application/json"}).status_code == 413
    big_login = make_client(None).post("/api/login", content=b"b" * 20_000, headers={"content-type": "application/json"})
    assert big_login.status_code == 413


def test_malformed_and_oversize_fields_are_rejected_safely():
    c = make_client()
    for body in ({}, {"message": "hi"}, {"session_id": SID}, {"message": "x" * 501, "session_id": SID},
                 {"message": "hi", "session_id": "../../etc/passwd"}, {"message": "hi", "session_id": "short"},
                 {"message": ["a"], "session_id": SID}, {"message": "hi", "session_id": SID, "timer_id": None}):
        r = c.post("/api/chat", json=body)
        assert r.status_code == 422 and "Traceback" not in r.text
    assert c.post("/api/chat", content=b"{broken", headers={"content-type": "application/json"}).status_code == 422
    assert c.post("/api/timers/cancel", json={"session_id": SID, "timer_id": -5}).status_code == 422
    assert c.post("/api/timers/cancel", json={"session_id": SID, "timer_id": 10**30}).status_code == 422
    assert c.get("/api/state", params={"session_id": "bad id!"}).status_code == 422


def test_cross_origin_posts_are_blocked_but_same_origin_and_allowlisted_work(monkeypatch):
    use_bedrock(monkeypatch, [text("ok")], repeat_last=True)
    c = make_client()
    assert chat(c, headers={"Origin": "https://evil.example"}).status_code == 403
    assert chat(c, headers={"Origin": "null"}).status_code == 403
    assert make_client(None).post("/api/login", json={"code": TEST_ENV["ACCESS_CODE"]},
                                  headers={"Origin": "https://evil.example"}).status_code == 403
    assert chat(c, headers={"Origin": "http://testserver"}).status_code == 200
    assert chat(c).status_code == 200  # non-browser clients send no Origin
    with_settings(monkeypatch, allowed_origins=("https://demo.example.com",))
    assert chat(c, headers={"Origin": "https://demo.example.com"}).status_code == 200


def test_no_cors_headers_are_emitted():
    r = make_client().get("/api/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers
    pre = make_client().options("/api/chat", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in pre.headers


@pytest.mark.parametrize("path", ["/static/../main.py", "/static/%2e%2e/main.py", "/static/..%2fmain.py", "/static/../.env",
                                  "/static/talksous.db", "/main.py", "/cooking.py", "/.env", "/data/recipes.json",
                                  "/data/talksous.db", "/static/", "/static", "/tests/conftest.py"])
def test_source_data_and_env_files_are_not_served(path):
    r = make_client().get(path)
    assert r.status_code in (301, 307, 404)
    assert "ACCESS_CODE" not in r.text and "SESSION_SECRET" not in r.text and "def " not in r.text


def test_static_directory_contains_only_frontend_assets():
    assert {p.suffix for p in (ROOT / "static").iterdir() if p.is_file()} <= {".html", ".js", ".css"}


# ===================================================================== rate limits
def test_chat_is_limited_per_user_and_users_do_not_share_budgets(monkeypatch):
    use_bedrock(monkeypatch, [text("ok")], repeat_last=True)
    with_settings(monkeypatch, chat_per_min=2)
    alice, bob = make_client("alice"), make_client("bob")
    assert [chat(alice, session_id="alice-session-1").status_code for _ in range(3)] == [200, 200, 429]
    assert chat(bob, session_id="bob-session-001").status_code == 200


def test_global_hourly_cap_protects_the_bedrock_bill(monkeypatch):
    use_bedrock(monkeypatch, [text("ok")], repeat_last=True)
    with_settings(monkeypatch, global_chat_per_hour=2)
    codes = [chat(make_client(f"user{i}"), session_id=f"session-user-{i}").status_code for i in range(4)]
    assert codes == [200, 200, 429, 429]


def test_state_polling_is_limited(monkeypatch):
    with_settings(monkeypatch, state_per_min=3)
    c = make_client()
    codes = [c.get("/api/state", params={"session_id": SID}).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]


def test_rejected_requests_do_not_consume_model_budget(monkeypatch):
    use_bedrock(monkeypatch, [text("ok")], repeat_last=True)
    with_settings(monkeypatch, global_chat_per_hour=1)
    make_client(None).post("/api/chat", json={"message": "hi", "session_id": SID})  # unauthenticated
    chat(make_client(), "x" * 501)  # invalid
    assert chat(make_client()).status_code == 200


# ============================================================ Bedrock failure handling
def client_error(code, message="Rate exceeded for arn:aws:bedrock:us-east-1:123456789012:foundation-model/x"):
    return ClientError({"Error": {"Code": code, "Message": message}}, "Converse")


@pytest.mark.parametrize("exc,status,phrase", [
    (client_error("ThrottlingException"), 429, "busy"),
    (client_error("AccessDeniedException"), 503, "not set up correctly"),
    (client_error("ResourceNotFoundException"), 503, "not set up correctly"),
    (client_error("ValidationException"), 503, "not set up correctly"),
    (client_error("ModelTimeoutException"), 504, "too long"),
    (client_error("ServiceUnavailableException"), 503, "temporarily unavailable"),
    (NoCredentialsError(), 503, "not set up correctly"),
    (ReadTimeoutError(endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com"), 504, "too long"),
    (RuntimeError("boom: secret-token-123"), 502, "Something went wrong"),
])
def test_bedrock_failures_become_safe_actionable_errors(monkeypatch, caplog, exc, status, phrase):
    use_bedrock(monkeypatch, [exc])
    r = chat(make_client())
    assert r.status_code == status and phrase in r.json()["detail"]
    for leak in ("arn:aws", "123456789012", "secret-token", "Traceback", "boto", "Bedrock", "foundation-model"):
        assert leak not in r.text
    assert cooking.load_history(SID) == []  # a failed turn leaves no half-saved conversation


def test_malformed_model_response_is_handled(monkeypatch):
    class Broken:
        def converse(self, **_):
            return {"output": {}}
    monkeypatch.setattr(main, "bedrock", Broken())
    r = chat(make_client())
    assert r.status_code == 502 and "unusable" in r.json()["detail"]


def test_slow_model_hits_the_turn_timeout(monkeypatch):
    class Slow:
        def converse(self, **_):
            time.sleep(0.6)
            return {"output": {"message": text("late")}}
    monkeypatch.setattr(main, "bedrock", Slow())
    with_settings(monkeypatch, turn_timeout_s=0.1)
    r = chat(make_client())
    assert r.status_code == 504 and "too long" in r.json()["detail"]
    time.sleep(0.7)  # let the abandoned worker finish; it must not have saved anything
    assert cooking.load_history(SID) == []


def test_failure_after_a_tool_ran_leaves_consistent_state(monkeypatch):
    use_bedrock(monkeypatch, [tools(("set_timer", {"label": "rice", "minutes": 15})), RuntimeError("model died")])
    c = make_client()
    assert chat(c).status_code == 502
    state = c.get("/api/state", params={"session_id": SID}).json()
    assert [t["label"] for t in state["timers"]] == ["rice"]  # the real action happened exactly once
    assert cooking.load_history(SID) == []


def test_secrets_are_not_written_to_logs(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    anon = make_client(None)
    anon.post("/api/login", json={"code": "my-wrong-guess-777"})
    anon.post("/api/login", json={"code": TEST_ENV["ACCESS_CODE"]})
    use_bedrock(monkeypatch, [client_error("AccessDeniedException"), RuntimeError("x")])
    chat(make_client())
    chat(make_client())
    for secret in (TEST_ENV["ACCESS_CODE"], TEST_ENV["SESSION_SECRET"], TEST_ENV["MCP_API_KEY"], "my-wrong-guess-777"):
        assert secret not in caplog.text


# ============================================================= health and database
def test_health_reports_ok_and_degraded_without_details(monkeypatch):
    c = make_client(None)  # health is public
    assert c.get("/api/health").json() == {"status": "ok"}

    def broken():
        raise sqlite3.OperationalError("unable to open database file /secret/path/talksous.db")
    monkeypatch.setattr(db, "ping", broken)
    r = c.get("/api/health")
    assert r.status_code == 503 and r.json() == {"status": "degraded"} and "secret" not in r.text


def test_database_file_is_owner_only():
    db.init_db()
    assert stat.S_IMODE(os.stat(db.db_path()).st_mode) == 0o600


def test_legacy_database_is_migrated_and_old_sessions_become_inaccessible(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.executescript("""CREATE TABLE sessions (id TEXT PRIMARY KEY, recipe_id TEXT, recipe_json TEXT,
        step_index INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'idle',
        adaptations_json TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL, updated_at REAL NOT NULL);
        INSERT INTO sessions(id, created_at, updated_at) VALUES ('legacy-session-1', 1, 1);""")
    conn.commit(); conn.close()
    monkeypatch.setenv("TALKSOUS_DB", str(legacy))
    db.init_db(); db.init_db()  # idempotent
    with cooking.acting_as("u:anyone"):
        with pytest.raises(SessionNotFound):
            cooking.ensure_session("legacy-session-1")


def test_expired_timer_is_reported_exactly_once_under_concurrency(clock):
    cooking.set_timer(SID, "eggs", seconds=10)
    clock.advance(11)
    reported, errors = [], []

    def poll():
        try:
            with cooking.acting_as(f"u:{'testclient'}"):
                reported.extend(cooking.pop_newly_expired(SID))
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=poll) for _ in range(12)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert not errors and len(reported) == 1


def test_concurrent_timer_creation_gives_unique_ids():
    ids, errors = [], []

    def make(i):
        try:
            with cooking.acting_as("u:testclient"):
                ids.append(cooking.set_timer(SID, f"t{i}", minutes=5)["timer"]["id"])
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=make, args=(i,)) for i in range(cooking.MAX_RUNNING_TIMERS)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert not errors and len(set(ids)) == len(ids) == cooking.MAX_RUNNING_TIMERS


# ======================================================== secrets are not in the repo
SECRET_PATTERNS = [r"AKIA[0-9A-Z]{16}", r"aws_secret_access_key\s*[=:]\s*[A-Za-z0-9/+]{30,}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"]
SKIP_DIRS = {"venv", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".git", "tests"}


def repo_files():
    for path in ROOT.rglob("*"):
        if path.is_file() and not (set(path.relative_to(ROOT).parts) & SKIP_DIRS) and path.name != ".env" \
                and path.suffix not in (".db", ".png", ".ico", ".zip"):
            yield path


def test_no_secrets_in_frontend_or_repo_files():
    for path in repo_files():
        body = path.read_text(errors="ignore")
        for pattern in SECRET_PATTERNS:
            assert not re.search(pattern, body), f"{path} looks like it contains a secret"
        for value in (TEST_ENV["ACCESS_CODE"], TEST_ENV["SESSION_SECRET"], TEST_ENV["MCP_API_KEY"]):
            assert value not in body
    for asset in (ROOT / "static").iterdir():
        body = asset.read_text(errors="ignore")
        assert not re.search(r"SESSION_SECRET|MCP_API_KEY|aws_secret|AKIA|Bearer ", body)


def test_env_example_has_placeholders_only():
    for line in (ROOT / ".env.example").read_text().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            assert not re.fullmatch(r"[A-Za-z0-9/+_\-]{20,}", value.strip()) or value.startswith(("change-me", "your-")), key


def test_gitignore_and_dockerignore_exclude_secrets_and_data():
    for name in (".gitignore", ".dockerignore"):
        text_ = (ROOT / name).read_text()
        for needed in (".env", "*.db"):
            assert needed in text_, f"{needed} missing from {name}"


# ========================================= MCP over a real HTTP server (live integration)
@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setenv("TALKSOUS_DB", str(tmp_path_factory.mktemp("live") / "live.db"))
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True); thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True; thread.join(timeout=5); mp.undo()


MCP_KEY = TEST_ENV["MCP_API_KEY"]


def mcp_call(url, tool, args, key=MCP_KEY):
    async def go():
        async with Client(url + "/mcp/", auth=key) as c:
            res = await c.call_tool(tool, args, raise_on_error=False)
            return res.is_error, "".join(getattr(b, "text", "") for b in res.content)
    return asyncio.run(go())


def test_mcp_endpoint_rejects_missing_wrong_and_cookie_credentials(live_server):
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    hdrs = {"accept": "application/json, text/event-stream"}
    assert httpx.post(live_server + "/mcp/", json=body, headers=hdrs).status_code == 401
    r = httpx.post(live_server + "/mcp/", json=body, headers={**hdrs, "authorization": "Bearer nope-nope-nope-nope-nope"})
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    browser_cookie = {"cookie": f"{COOKIE_NAME}={issue_cookie(main.settings, 'alice')}"}
    assert httpx.post(live_server + "/mcp/", json=body, headers={**hdrs, **browser_cookie}).status_code == 401
    assert httpx.post(live_server + "/mcp", json=body, headers=hdrs).status_code == 401  # no slash: still gated
    with pytest.raises(Exception):
        asyncio.run(Client(live_server + "/mcp/").__aenter__())


def test_mcp_is_disabled_when_no_key_is_configured(live_server, monkeypatch):
    with_settings(monkeypatch, mcp_api_key=None)
    r = httpx.post(live_server + "/mcp/", json={}, headers={"authorization": f"Bearer {MCP_KEY}"})
    assert r.status_code == 404


def test_authorised_mcp_client_can_discover_and_call_tools(live_server):
    async def go():
        async with Client(live_server + "/mcp/", auth=MCP_KEY) as c:
            return {t.name for t in await c.list_tools()}
    names = asyncio.run(go())
    assert {"search_recipes", "start_recipe", "next_step", "set_timer", "list_timers", "cancel_timer", "food_safety_lookup",
            "create_recipe", "get_pantry", "update_pantry", "set_servings", "pantry_deduction"} <= names and len(names) == 18
    sid = "mcp-session-0001"
    assert mcp_call(live_server, "start_recipe", {"session_id": sid, "recipe": "shakshuka"})[0] is False
    assert mcp_call(live_server, "set_timer", {"session_id": sid, "label": "eggs", "seconds": 30})[0] is False
    err, out = mcp_call(live_server, "list_timers", {"session_id": sid})
    assert not err and '"eggs"' in out and '"running"' in out


def test_mcp_client_cannot_touch_browser_sessions_and_vice_versa(live_server):
    with cooking.acting_as("u:alice"):
        cooking.start_recipe("alice-kitchen-001", "shakshuka")
        cooking.set_timer("alice-kitchen-001", "alice eggs", minutes=5)
    for tool, args in (("list_timers", {}), ("next_step", {}), ("set_timer", {"label": "x", "minutes": 1})):
        err, out = mcp_call(live_server, tool, {"session_id": "alice-kitchen-001", **args})
        assert "Unknown session" in out and "alice eggs" not in out

    assert mcp_call(live_server, "set_timer", {"session_id": "mcp-owned-0001", "label": "ops", "seconds": 30})[0] is False
    with cooking.acting_as("u:alice"):
        with pytest.raises(SessionNotFound):
            cooking.list_timers("mcp-owned-0001")
    assert make_client("alice").get("/api/state", params={"session_id": "mcp-owned-0001"}).status_code == 404


def test_mcp_enforces_host_allowlist(live_server):
    r = httpx.post(live_server + "/mcp/", json={}, headers={"host": "evil.example", "authorization": f"Bearer {MCP_KEY}"})
    assert r.status_code in (400, 403, 421)


def test_browser_and_live_server_serve_the_frontend(live_server):
    r = httpx.get(live_server + "/")
    assert r.status_code == 200 and "Alexa+ Kitchen Display" in r.text
    for asset in ("/static/app.js", "/static/app.css"):
        assert httpx.get(live_server + asset).status_code == 200


def test_mcp_pantry_is_private_to_the_mcp_principal(live_server):
    err, out = mcp_call(live_server, "update_pantry", {"session_id": "mcp-pantry-0001", "action": "add", "items": ["6 eggs"]})
    assert not err and "6 eggs" in out
    err, out = mcp_call(live_server, "get_pantry", {"session_id": "mcp-pantry-0001"})
    assert "6 eggs" in out
    with cooking.acting_as("u:alice"):
        import pantry
        assert pantry.list_pantry()["items"] == []  # a browser user never sees the MCP client's pantry
        pantry.update_pantry("add", ["2 lemons"])
    assert "lemon" not in mcp_call(live_server, "get_pantry", {"session_id": "mcp-pantry-0001"})[1]
