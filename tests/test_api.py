import json

import pytest
from fastapi.testclient import TestClient

import cooking
import main
from conftest import SID, FakeBedrock, text, tools


@pytest.fixture
def client():
    main._hits.clear()
    with TestClient(main.app) as c:
        yield c


def use_bedrock(monkeypatch, script, **kw):
    fake = FakeBedrock(script, **kw)
    monkeypatch.setattr(main, "bedrock", fake)
    return fake


def chat(client, message="hello", session_id=SID):
    return client.post("/api/chat", json={"message": message, "session_id": session_id})


def tool_results(call):
    """toolResult blocks in the last user message of a recorded converse call."""
    return [b["toolResult"] for b in call["messages"][-1]["content"] if "toolResult" in b]


def test_plain_text_turn_strips_thinking_and_persists_history(client, monkeypatch):
    fake = use_bedrock(monkeypatch, [text("<thinking>plan</thinking>Hi chef!"), text("Still here.")])
    r = chat(client, "hey")
    assert r.status_code == 200 and r.json()["response"] == "Hi chef!" and r.json()["tool_called"] is None
    chat(client, "again?")
    second = fake.calls[1]
    assert [m["role"] for m in second["messages"]] == ["user", "assistant", "user"]  # history carried over
    assert "CURRENT KITCHEN STATE" in second["system"][0]["text"]


def test_tool_call_runs_through_mcp_and_updates_state(client, monkeypatch):
    use_bedrock(monkeypatch, [tools(("start_recipe", {"recipe": "lemon soy chicken"})), text("Starting. Mix the marinade.")])
    r = chat(client, "let's make the lemon soy chicken").json()
    assert r["tool_called"] == "start_recipe" and r["tools_called"] == ["start_recipe"]
    assert r["state"]["session"]["recipe"]["id"] == "lemon-soy-chicken"
    assert r["state"]["session"]["step"]["step_number"] == 1


def test_state_is_injected_into_next_turn_prompt(client, monkeypatch):
    fake = use_bedrock(monkeypatch, [tools(("start_recipe", {"recipe": "shakshuka"})), text("ok"), text("ok")])
    chat(client, "start shakshuka")
    chat(client, "what step am I on?")
    assert "Step 1 of 4" in fake.calls[2]["system"][0]["text"]


def test_model_cannot_choose_or_forge_session_id(client, monkeypatch):
    use_bedrock(monkeypatch, [tools(("start_recipe", {"recipe": "shakshuka", "session_id": "attacker-session-9"})), text("ok")])
    chat(client)
    assert cooking.session_view(SID)["recipe"]["id"] == "shakshuka"
    assert cooking.session_view("attacker-session-9")["recipe"] is None


def test_multiple_tool_uses_in_one_turn_all_get_results(client, monkeypatch):
    fake = use_bedrock(monkeypatch, [
        tools(("set_timer", {"label": "rice", "minutes": 15}), ("set_timer", {"label": "chicken", "minutes": 30})),
        text("Two timers are running.")])
    r = chat(client, "timers for rice and chicken").json()
    results = tool_results(fake.calls[1])
    assert [x["toolUseId"] for x in results] == ["use0", "use1"] and all(x["status"] == "success" for x in results)
    assert r["tools_called"] == ["set_timer", "set_timer"]
    assert [t["label"] for t in r["state"]["timers"]] == ["rice", "chicken"]


def test_tool_error_is_reported_to_model_as_error(client, monkeypatch):
    fake = use_bedrock(monkeypatch, [tools(("start_recipe", {"recipe": "moon pie"})), text("I couldn't find that.")])
    assert chat(client).status_code == 200
    res = tool_results(fake.calls[1])[0]
    assert res["status"] == "error" and "couldn't find" in res["content"][0]["text"]


def test_bad_arguments_and_unknown_tools_do_not_crash_the_turn(client, monkeypatch):
    fake = use_bedrock(monkeypatch, [
        tools(("set_timer", {"label": "x"}), ("set_timer", {"label": "x", "minutes": "soon"}), ("launch_missiles", {})),
        text("Sorry, that didn't work.")])
    assert chat(client).status_code == 200
    statuses = [x["status"] for x in tool_results(fake.calls[1])]
    assert statuses == ["error", "error", "error"]
    assert "Traceback" not in json.dumps(fake.calls[1]["messages"])


def test_runaway_tool_loop_is_capped(client, monkeypatch):
    fake = use_bedrock(monkeypatch, [tools(("list_timers", {}))], repeat_last=True)
    r = chat(client)
    assert r.status_code == 200 and "too many steps" in r.json()["response"]
    assert len(fake.calls) == main.MAX_TOOL_ITERATIONS


def test_bedrock_failure_returns_generic_502(client, monkeypatch):
    use_bedrock(monkeypatch, [RuntimeError("secret-arn:aws:iam::123 denied")])
    r = chat(client)
    assert r.status_code == 502 and "secret" not in r.text and "arn" not in r.text
    assert cooking.load_history(SID) == []  # failed turn leaves no half-saved history


def test_request_validation(client):
    assert chat(client, "hi", session_id="short").status_code == 422
    assert chat(client, "").status_code == 422
    assert chat(client, "x" * 501).status_code == 422
    assert chat(client, "   ").status_code == 422
    assert client.get("/api/state", params={"session_id": "bad id!"}).status_code == 422


def test_rate_limit(client, monkeypatch):
    use_bedrock(monkeypatch, [text("ok")], repeat_last=True)
    monkeypatch.setattr(main, "RATE_LIMIT_PER_MIN", 2)
    codes = [chat(client).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_bedrock_tool_specs_hide_session_id_and_are_plain_schema():
    import asyncio
    specs = asyncio.run(main.get_bedrock_tools())
    names = {s["toolSpec"]["name"] for s in specs}
    assert {"search_recipes", "start_recipe", "next_step", "set_timer", "cancel_timer", "food_safety_lookup"} <= names
    for s in specs:
        schema = s["toolSpec"]["inputSchema"]["json"]
        dumped = json.dumps(schema)
        assert schema["type"] == "object" and "session_id" not in dumped and '"null"' not in dumped
    set_timer = next(s for s in specs if s["toolSpec"]["name"] == "set_timer")["toolSpec"]["inputSchema"]["json"]
    assert set_timer["required"] == ["label"]


def test_timer_expiry_is_reported_once_by_state_endpoint(client, monkeypatch, clock):
    use_bedrock(monkeypatch, [tools(("set_timer", {"label": "eggs", "seconds": 30})), text("Timer set.")])
    chat(client)
    s = client.get("/api/state", params={"session_id": SID}).json()
    assert s["timers"][0]["remaining_s"] == 30 and s["newly_expired"] == []
    clock.advance(31)
    s = client.get("/api/state", params={"session_id": SID}).json()
    assert [t["label"] for t in s["newly_expired"]] == ["eggs"] and s["timers"][0]["status"] == "expired"
    assert client.get("/api/state", params={"session_id": SID}).json()["newly_expired"] == []
    assert client.post("/api/timers/cancel", json={"session_id": SID, "timer_id": s["timers"][0]["id"]}).status_code == 200
    assert client.get("/api/state", params={"session_id": SID}).json()["timers"] == []


def test_session_and_timers_survive_server_restart(monkeypatch, clock):
    main._hits.clear()
    use_bedrock(monkeypatch, [tools(("start_recipe", {"recipe": "shakshuka"}), ("set_timer", {"label": "sauce", "minutes": 10})), text("ok")])
    with TestClient(main.app) as c1:
        chat(c1)
    clock.advance(120)
    with TestClient(main.app) as c2:  # brand-new app lifespan, same database file
        s = c2.get("/api/state", params={"session_id": SID, "pop_expired": False}).json()
    assert s["session"]["recipe"]["id"] == "shakshuka"
    assert s["timers"][0]["label"] == "sauce" and s["timers"][0]["remaining_s"] == 480


def test_health_and_index(client):
    assert client.get("/api/health").json() == {"ok": True}
    assert client.get("/").status_code == 200
