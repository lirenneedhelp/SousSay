import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient  # noqa: F401

import cooking
import main
from conftest import SID, FakeBedrock, make_client, text, tools


@pytest.fixture
def client():
    with make_client() as c:
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
    monkeypatch.setattr(main, "settings", replace(main.settings, chat_per_min=2))
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
    use_bedrock(monkeypatch, [tools(("start_recipe", {"recipe": "shakshuka"}), ("set_timer", {"label": "sauce", "minutes": 10})), text("ok")])
    with make_client() as c1:
        chat(c1)
    clock.advance(120)
    with make_client() as c2:  # brand-new client and app lifespan, same cookie, same database file
        s = c2.get("/api/state", params={"session_id": SID, "pop_expired": False}).json()
    assert s["session"]["recipe"]["id"] == "shakshuka"
    assert s["timers"][0]["label"] == "sauce" and s["timers"][0]["remaining_s"] == 480


def test_health_and_index(client):
    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/").status_code == 200


# ----------------------------------------------------- the model must not invent timers
def test_timer_questions_are_answered_from_the_database_without_the_model(client, monkeypatch, clock):
    fake = use_bedrock(monkeypatch, [tools(("set_timer", {"label": "rice", "minutes": 15}), ("set_timer", {"label": "curry", "seconds": 20})), text("Both timers are running.")])
    chat(client, "set two timers")
    clock.advance(30)
    spy = use_bedrock(monkeypatch, [text("must never be called")])
    for question in ("do I have any active timers", "where's my timer", "how long left", "what active timers do I have right now"):
        r = chat(client, question).json()
        assert "rice has 14 minutes and 30 seconds left" in r["response"] and "curry is done" in r["response"]
        assert r["tools_called"] == []
    assert spy.calls == []
    assert "no timers" not in cooking.describe_timers(SID).lower()


def test_no_timers_is_reported_honestly(client, monkeypatch):
    spy = use_bedrock(monkeypatch, [text("You have a timer for curry.")])
    assert chat(client, "do i have any active timers").json()["response"] == "You don't have any timers running."
    assert spy.calls == []


def test_commands_that_mention_timers_still_go_to_the_model(client, monkeypatch):
    spy = use_bedrock(monkeypatch, [text("ok")], repeat_last=True)
    for command in ("cancel the rice timer", "stop the timer", "start a timer"):  # (a plain "set a 5 minute timer" is handled by the app)
        chat(client, command)
    assert len(spy.calls) == 3


def test_unbacked_timer_set_claim_is_replaced_and_not_remembered(client, monkeypatch):
    use_bedrock(monkeypatch, [text("I've set a 20 minute timer for Thai Beef Curry Cooking."), text("ok")])
    r = chat(client, "please put 20 minutes on the curry").json()
    assert "couldn't start that timer" in r["response"] and r["state"]["timers"] == []
    assert "Thai Beef" not in str(cooking.load_history(SID))  # the false claim never enters the conversation history


def test_backed_set_claim_and_offers_are_left_alone(client, monkeypatch):
    use_bedrock(monkeypatch, [tools(("set_timer", {"label": "rice", "minutes": 15})), text("I've set a 15 minute timer for rice.")])
    r = chat(client, "15 minutes for the rice").json()
    assert r["response"] == "I've set a 15 minute timer for rice." and len(r["state"]["timers"]) == 1
    use_bedrock(monkeypatch, [text("Want me to set a timer for 20 minutes?")])
    assert chat(client, "what next").json()["response"] == "Want me to set a timer for 20 minutes?"


def test_unbacked_cancel_claim_is_replaced(client, monkeypatch):
    use_bedrock(monkeypatch, [text("I've cancelled the rice timer."), tools(("set_timer", {"label": "rice", "minutes": 5})), text("ok")])
    assert "couldn't cancel" in chat(client, "get rid of the rice thing").json()["response"]


@pytest.mark.parametrize("message", ["timers for rice and chicken", "a 10 minute timer for pasta", "twenty minutes on the curry",
                                     "what timer should I use for the eggs", "what timers does this recipe need",
                                     "can you add a timer", "timer for rice"])
def test_implicit_timer_commands_are_not_mistaken_for_status_questions(message):
    assert main.is_timer_status_query(message) is False


@pytest.mark.parametrize("message", ["do i have any active timers", "where's my timer", "how long left", "how much longer",
                                     "what active timers do i have right now", "is the rice timer done", "check my timers", "time left?"])
def test_real_timer_status_questions_are_recognised(message):
    assert main.is_timer_status_query(message) is True


def test_plain_timer_request_is_done_by_the_app_without_the_model(client, monkeypatch):
    spy = use_bedrock(monkeypatch, [text("must never be called")])
    r = chat(client, "set a 8 minute timer for eggs").json()
    assert r["response"] == "Okay, eggs timer set for 8 minutes." and spy.calls == []
    assert [(t["label"], t["remaining_s"]) for t in r["state"]["timers"]] == [("eggs", 480)]
    r = chat(client, "set a 90 second timer").json()
    assert "kitchen timer set for 1 minute and 30 seconds" in r["response"] and len(r["state"]["timers"]) == 2
    assert "eggs" in chat(client, "do i have any timers").json()["response"]
    assert [m["role"] for m in cooking.load_history(SID)][:2] == ["user", "assistant"]


def test_plain_timer_request_with_a_bad_duration_explains_why(client, monkeypatch):
    spy = use_bedrock(monkeypatch, [text("nope")])
    for phrase in ("set a 0 minute timer for eggs", "set a 5000 hour timer for eggs"):
        r = chat(client, phrase).json()
        assert "couldn't start that timer" in r["response"] and "between 1 second and 12 hours" in r["response"]
    assert spy.calls == [] and chat(client, "do i have any timers").json()["response"] == "You don't have any timers running."
