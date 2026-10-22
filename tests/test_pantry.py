import json
import threading

import pytest

import cooking
import main
import pantry
import units
from conftest import SID, FakeBedrock, make_client, text, tools
from cooking import CookingError, SessionNotFound

LEMON_SOY_PANTRY = ["3 chicken breast", "1 lemon", "500 g rice", "200 ml soy sauce", "30 g ginger"]


def finish_recipe(recipe="lemon soy chicken", servings=None, sid=SID):
    cooking.start_recipe(sid, recipe, servings)
    while cooking.next_step(sid).get("status") != "completed":
        pass


def amounts():
    return {i["item"]: (i["amount"], i["unit"]) for i in pantry.list_pantry()["items"]}


# ================================================================== units and parsing
@pytest.mark.parametrize("phrase,expected", [
    ("6 eggs", ("eggs", 6.0, "piece")), ("500 g of rice", ("rice", 500.0, "g")), ("200ml soy sauce", ("soy sauce", 200.0, "ml")),
    ("500g rice", ("rice", 500.0, "g")), ("2 cups rice", ("rice", 369.08, "g")), ("2 cups cooked rice", ("cooked rice", 317.03, "g")),
    ("a dozen eggs", ("eggs", 12.0, "piece")), ("half a lemon", ("lemon", 0.5, "piece")), ("two onions", ("onions", 2.0, "piece")),
    ("1 1/2 cups milk", ("milk", 354.88, "ml")), ("3 cloves garlic", ("garlic", 3.0, "clove")), ("a can of tomatoes", ("tomatoes", 1.0, "can")),
    ("2 lbs ground beef", ("ground beef", 907.18, "g")), ("1.5 kg flour", ("flour", 1500.0, "g")), ("20 eggs", ("eggs", 20.0, "piece")),
    ("lemons", ("lemons", None, None)), ("0 eggs", ("eggs", 0.0, "piece")), ("anchovies", ("anchovies", None, None)),
    ("one onion", ("onion", 1.0, "piece")), ("2 cups", ("", None, None)),
])
def test_item_parsing(phrase, expected):
    assert units.parse_item(phrase) == expected


def test_densities_pick_the_most_specific_matching_ingredient():
    assert units.to_unit(473.176, "ml", "g", "rice") == pytest.approx(369.08, abs=0.1)         # not the cooked-rice density
    assert units.to_unit(473.176, "ml", "g", "cooked rice") == pytest.approx(317.03, abs=0.1)
    assert units.to_unit(1, "g", "ml", "mystery powder") is None and units.to_unit(2, "can", "g", "x") is None
    assert units.to_unit(3, "clove", "clove") == 3


def test_formatting_follows_the_unit_system():
    assert units.format_amount(450, "g", "metric") == "450 g" and units.format_amount(1500, "g", "metric") == "1.5 kg"
    assert units.format_amount(450, "g", "us") == "15.9 oz" and units.format_amount(907.18, "g", "us") == "2 lb"
    assert units.format_amount(250, "ml", "metric") == "250 ml" and units.format_amount(1500, "ml", "metric") == "1.5 l"
    assert units.format_amount(5, "ml", "us") == "1 tsp" and units.format_amount(45, "ml", "us") == "3 tbsp"
    assert units.format_amount(236.588, "ml", "us") == "1 cup" and units.format_amount(300, "ml", "us") == "1 1/4 cups"
    assert units.format_item("egg", 6, "piece") == "6 eggs" and units.format_item("tomato", 1, "piece") == "1 tomato"
    assert units.format_item("tomato", 2, "piece") == "2 tomatoes" and units.format_item("canned tomatoes", 2, "can") == "2 cans of tomatoes"
    assert units.format_item("garlic", 4, "clove") == "4 cloves of garlic" and units.format_item("lemons", None, None) == "lemons"


def test_unit_system_defaults_to_the_users_country():
    assert [units.units_for_region(r) for r in ("US", "us", "LR", "SG", "GB", "AU", None, "")] == \
           ["us", "us", "us", "metric", "metric", "metric", "metric", "metric"]


def test_all_curated_recipes_have_numeric_amounts_and_valid_units():
    allowed = {"g", "ml", *units.COUNT_UNITS}
    for rec in cooking._recipes().values():
        assert rec["serves"] >= 1
        for ing in rec["ingredients"]:
            assert ing["amount"] > 0 and ing["unit"] in allowed and "qty" not in ing, (rec["id"], ing)


# ============================================================================ pantry
def test_pantry_starts_empty_and_belongs_to_the_user_not_the_session():
    assert pantry.list_pantry()["items"] == [] and pantry.describe_pantry() == "Your pantry is empty."
    pantry.update_pantry("add", ["6 eggs"])
    cooking.start_recipe("some-other-session", "shakshuka")  # a different cooking session, same user
    assert amounts()["egg"] == (6.0, "piece")


def test_add_accumulates_converts_units_and_set_replaces():
    pantry.update_pantry("add", ["500 g rice", "6 eggs"])
    out = pantry.update_pantry("add", ["2 cups rice", "a dozen eggs"])
    assert amounts()["rice"][0] == pytest.approx(869.08, abs=0.1) and amounts()["egg"] == (18.0, "piece")
    assert out["summary"].startswith("Added")
    pantry.update_pantry("set", ["1 kg rice"])
    assert amounts()["rice"] == (1000.0, "g")
    pantry.update_pantry("set", ["0 eggs"])
    assert "egg" not in amounts()


def test_remove_deletes_or_subtracts_and_reports_unknown_items():
    pantry.update_pantry("add", ["6 eggs", "500 g rice"])
    pantry.update_pantry("remove", ["2 eggs"])
    assert amounts()["egg"] == (4.0, "piece")
    out = pantry.update_pantry("remove", ["eggs", "saffron"])
    assert "egg" not in amounts() and out["skipped"][0]["item"] == "saffron" and "rice" in amounts()


def test_incompatible_units_are_refused_not_guessed():
    pantry.update_pantry("add", ["500 g rice", "6 eggs"])
    out = pantry.update_pantry("add", ["2 eggs", "3 cans rice", "10 g eggs"])
    assert amounts()["egg"] == (8.0, "piece") and amounts()["rice"] == (500.0, "g")
    assert len(out["skipped"]) == 2 and all("can't add" in s["reason"] for s in out["skipped"])


def test_items_without_a_quantity_are_listed_but_never_deducted():
    pantry.update_pantry("add", ["eggs", "lemon"])
    assert amounts()["egg"] == (None, None)
    pantry.update_pantry("add", ["6 eggs"])
    assert amounts()["egg"] == (6.0, "piece")


def test_pantry_limits_and_validation():
    for bad in (("steal", ["eggs"]), ("add", []), ("add", [""]), ("add", None)):
        with pytest.raises(CookingError):
            pantry.update_pantry(*bad)
    assert "couldn't read" in pantry.update_pantry("add", ["x" * 61 + " 5"])["skipped"][0]["reason"]
    with pytest.raises(CookingError):
        pantry.update_pantry("add", [f"item{i}" for i in range(pantry.MAX_BATCH + 1)])
    for i in range(0, pantry.MAX_ITEMS, 10):
        pantry.update_pantry("add", [f"thing{j} {i}" for j in range(10)])
    out = pantry.update_pantry("add", ["one more thing"])
    assert len(pantry.list_pantry()["items"]) == pantry.MAX_ITEMS and "full" in out["skipped"][0]["reason"]


def test_each_user_has_a_separate_pantry():
    with cooking.acting_as("u:alice"):
        pantry.update_pantry("add", ["6 eggs"])
    with cooking.acting_as("u:bob"):
        assert pantry.list_pantry()["items"] == []
        pantry.update_pantry("add", ["1 lemon"])
    with cooking.acting_as("u:alice"):
        assert list(amounts()) == ["egg"]


def test_pantry_functions_fail_closed_without_a_principal():
    token = cooking._principal.set(None)
    try:
        for fn in (pantry.list_pantry, pantry.rows, pantry.get_units, lambda: pantry.update_pantry("add", ["eggs"])):
            with pytest.raises(SessionNotFound):
                fn()
    finally:
        cooking._principal.reset(token)


# ============================================================================= units
def test_units_default_metric_and_can_be_switched_per_user():
    assert pantry.get_units() == "metric" and pantry.list_pantry()["units_set"] is False
    pantry.update_pantry("add", ["500 g rice", "250 ml milk"])
    assert [i["display"] for i in pantry.list_pantry()["items"]] == ["250 ml milk", "500 g rice"]
    pantry.set_units("us")
    assert [i["display"] for i in pantry.list_pantry()["items"]] == ["1 cup milk", "1.1 lb rice"]
    assert pantry.list_pantry()["units_set"] is True and amounts()["rice"] == (500.0, "g")  # storage never changes
    with cooking.acting_as("u:other"):
        assert pantry.get_units() == "metric"
    with pytest.raises(CookingError):
        pantry.set_units("imperial")


# ========================================================================== servings
def test_servings_scale_ingredient_lists():
    base = cooking.start_recipe(SID, "lemon soy chicken")
    assert base["servings"] == 2 and "185 g rice" in base["ingredients"]
    four = cooking.start_recipe(SID, "lemon soy chicken", servings=4)
    assert four["servings"] == 4 and "369 g rice" in four["ingredients"] and "4 chicken breasts" in four["ingredients"]
    six = cooking.set_servings(SID, 6)
    assert "554 g rice" in six["ingredients"] and cooking.session_view(SID)["servings"] == 6
    pantry.set_units("us")
    assert "3 tbsp soy sauce" in cooking.set_servings(SID, 2)["ingredients"]


@pytest.mark.parametrize("bad", [0, 25, -1, "many", 2.5])
def test_servings_are_validated(bad):
    with pytest.raises(CookingError):
        cooking.start_recipe(SID, "shakshuka", bad)
    cooking.start_recipe(SID, "shakshuka")
    with pytest.raises(CookingError):
        cooking.set_servings(SID, bad)


def test_set_servings_needs_an_active_recipe():
    with pytest.raises(CookingError):
        cooking.set_servings(SID, 3)


@pytest.mark.parametrize("phrase,n", [("cook for 4", 4), ("make it for four people", 4), ("4 servings", 4), ("for 6", 6),
                                      ("scale to 3", 3), ("we're cooking for two", 2), ("change servings to 5", 5)])
def test_servings_phrases_are_parsed(phrase, n):
    assert cooking.parse_servings_command(phrase) == n


@pytest.mark.parametrize("phrase", ["4", "cook for 4 minutes", "for the eggs", "how many servings", "make it spicy", "set a 4 minute timer"])
def test_other_phrases_are_not_servings(phrase):
    assert cooking.parse_servings_command(phrase) is None


def test_shortfall_is_reported_at_the_chosen_scale():
    pantry.update_pantry("add", LEMON_SOY_PANTRY)
    assert cooking.start_recipe(SID, "lemon soy chicken")["short_in_pantry"] == []
    short = cooking.set_servings(SID, 8)["short_in_pantry"]
    assert {s["item"] for s in short} == {"chicken breast", "lemon", "rice"}  # soy sauce and ginger still cover 8 servings
    assert any(s["item"] == "chicken breast" and "8" in s["need"] and s["have"].startswith("3") for s in short)


# ========================================================================= deduction
def test_deduction_is_pending_only_after_completion_and_previews_exact_amounts():
    pantry.update_pantry("add", LEMON_SOY_PANTRY)
    assert pantry.deduction_status(SID)["state"] is None
    cooking.start_recipe(SID, "lemon soy chicken")
    assert pantry.deduction_status(SID)["state"] is None
    with pytest.raises(CookingError):
        pantry.apply_deduction(SID)  # not finished yet
    finish_recipe()
    status = pantry.deduction_status(SID)
    assert status["state"] == "pending" and "2 chicken breasts" in status["used"] and "185 g rice" in status["used"]
    assert amounts()["chicken breast"] == (3.0, "piece")  # a preview changes nothing


def test_apply_subtracts_scaled_amounts_once_and_removes_emptied_items():
    pantry.update_pantry("add", LEMON_SOY_PANTRY)
    finish_recipe()
    out = pantry.apply_deduction(SID)
    assert out["status"] == "subtracted" and out["skipped"] == []
    left = amounts()
    assert left["chicken breast"] == (1.0, "piece") and "lemon" not in left
    assert left["rice"][0] == pytest.approx(315.46, abs=0.05) and left["soy sauce"][0] == pytest.approx(155.64, abs=0.05)
    assert left["ginger"][0] == pytest.approx(24.09, abs=0.05)
    with pytest.raises(CookingError):
        pantry.apply_deduction(SID)  # exactly once
    assert amounts() == left
    assert pantry.deduction_status(SID)["state"] == "applied"


def test_deduction_uses_the_chosen_servings():
    pantry.update_pantry("add", ["10 chicken breast", "5 lemon", "1 kg rice", "500 ml soy sauce", "100 g ginger"])
    finish_recipe(servings=4)
    pantry.apply_deduction(SID)
    left = amounts()
    assert left["chicken breast"][0] == 6.0 and left["rice"][0] == pytest.approx(1000 - 369.08, abs=0.1)


def test_running_short_uses_everything_and_says_so():
    pantry.update_pantry("add", ["1 chicken breast", "1 lemon", "100 g rice"])
    finish_recipe()
    out = pantry.apply_deduction(SID)
    assert "1 chicken breast (all you had)" in out["used"] and "100 g rice (all you had)" in out["used"]
    assert "rice" not in amounts() and "chicken breast" not in amounts()


def test_unmatched_unknown_and_incomparable_items_are_skipped_not_guessed():
    pantry.update_pantry("add", ["chicken breast", "3 cans rice", "1 lemon"])   # unknown qty; cans can't compare with grams
    finish_recipe()
    out = pantry.apply_deduction(SID)
    reasons = {s["item"]: s["reason"] for s in out["skipped"]}
    assert "no quantity" in reasons["chicken breast"] and "can't compare" in reasons["rice"] and "not in your pantry" in reasons["soy sauce"]
    assert amounts()["rice"] == (3.0, "can") and amounts()["chicken breast"] == (None, None)
    assert "lemon" not in amounts()  # the one exact match was subtracted


def test_substitutions_deduct_the_replacement():
    pantry.update_pantry("add", ["1 lemon", "2 lime", "500 g rice", "3 chicken breast", "200 ml soy sauce", "30 g ginger"])
    cooking.start_recipe(SID, "lemon soy chicken")
    cooking.apply_substitution(SID, "lemon", "lime")
    while cooking.next_step(SID).get("status") != "completed":
        pass
    pantry.apply_deduction(SID)
    assert amounts()["lemon"] == (1.0, "piece") and amounts()["lime"] == (1.0, "piece")


def test_undo_restores_exactly_and_cannot_be_reapplied():
    pantry.update_pantry("add", LEMON_SOY_PANTRY)
    before = amounts()
    finish_recipe()
    pantry.apply_deduction(SID)
    pantry.update_pantry("add", ["100 g rice"])  # edits made after the deduction are kept
    out = pantry.undo_deduction(SID)
    assert out["not_restored"] == [] and "2 chicken breasts" in out["restored"]
    after = amounts()
    assert after["lemon"] == before["lemon"] and after["chicken breast"] == before["chicken breast"]
    assert after["rice"][0] == pytest.approx(before["rice"][0] + 100, abs=0.1)
    with pytest.raises(CookingError):
        pantry.undo_deduction(SID)
    with pytest.raises(CookingError):
        pantry.apply_deduction(SID)


def test_skip_leaves_the_pantry_alone_and_is_final():
    pantry.update_pantry("add", LEMON_SOY_PANTRY)
    finish_recipe()
    before = amounts()
    assert pantry.skip_deduction(SID)["status"] == "skipped"
    assert amounts() == before and pantry.deduction_status(SID)["state"] == "skipped"
    for fn in (pantry.apply_deduction, pantry.skip_deduction, pantry.undo_deduction):
        with pytest.raises(CookingError):
            fn(SID)


def test_restarting_a_recipe_resets_the_decision():
    pantry.update_pantry("add", LEMON_SOY_PANTRY)
    finish_recipe()
    pantry.apply_deduction(SID)
    finish_recipe()
    assert pantry.deduction_status(SID)["state"] == "pending"


def test_concurrent_apply_subtracts_exactly_once():
    pantry.update_pantry("add", LEMON_SOY_PANTRY)
    finish_recipe()
    results, errors = [], []

    def go():
        try:
            with cooking.acting_as("u:testclient"):
                results.append(pantry.apply_deduction(SID)["status"])
        except CookingError as e:
            errors.append(str(e))

    threads = [threading.Thread(target=go) for _ in range(10)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert results == ["subtracted"] and len(errors) == 9
    assert amounts()["chicken breast"] == (1.0, "piece")


def test_other_users_cannot_decide_or_change_a_session_that_is_not_theirs():
    with cooking.acting_as("u:alice"):
        pantry.update_pantry("add", LEMON_SOY_PANTRY)
        finish_recipe()
    with cooking.acting_as("u:bob"):
        for fn in (lambda: pantry.apply_deduction(SID), lambda: pantry.skip_deduction(SID), lambda: pantry.undo_deduction(SID),
                   lambda: pantry.deduction_status(SID), lambda: cooking.set_servings(SID, 4)):
            with pytest.raises(SessionNotFound):
                fn()
    with cooking.acting_as("u:alice"):
        assert pantry.deduction_status(SID)["state"] == "pending" and amounts()["chicken breast"] == (3.0, "piece")


# ================================================================== plain phrasings
@pytest.mark.parametrize("phrase,expected", [
    ("add 6 eggs to my pantry", ("add", "6 eggs")), ("Please add eggs and milk to the fridge", ("add", "eggs and milk")),
    ("put two lemons in my pantry.", ("add", "two lemons")), ("I'm out of lemons", ("remove", "lemons")),
    ("we ran out of milk", ("remove", "milk")), ("I have run out of eggs and rice", ("remove", "eggs and rice")),
    ("remove the eggs from my pantry", ("remove", "the eggs")), ("take 2 eggs out of my fridge", ("remove", "2 eggs")),
    ("I bought 6 eggs and milk", ("add", "6 eggs and milk")), ("I only have 3 eggs left", ("set", "3 eggs")),
    ("I have 2 lemons in my pantry", ("set", "2 lemons")), ("what's in my pantry?", ("list", "")), ("show me my fridge", ("list", "")),
])
def test_pantry_phrases_are_recognised(phrase, expected):
    assert pantry.parse_pantry_command(phrase) == expected


@pytest.mark.parametrize("phrase", [
    "I have chicken, rice and lemon", "what can I cook with what's in my fridge?", "I got chicken, what can I cook?",
    "I bought chicken for dinner", "add a timer", "set a 5 minute timer", "what can I make", "I'm hungry", "remove the timer",
])
def test_recipe_requests_are_not_mistaken_for_pantry_commands(phrase):
    assert pantry.parse_pantry_command(phrase) is None


@pytest.mark.parametrize("phrase,state,expected", [
    ("yes", "pending", "apply"), ("Yes please", "pending", "apply"), ("sure!", "pending", "apply"), ("go ahead", "pending", "apply"),
    ("no", "pending", "skip"), ("no thanks", "pending", "skip"), ("skip it", "pending", "skip"),
    ("undo", "applied", "undo"), ("put them back", "applied", "undo"),
    ("yes", None, None), ("yes", "applied", None), ("undo", "pending", None), ("yes the pasta looks great", "pending", None),
    ("no more garlic please", "pending", None),
])
def test_yes_no_undo_answers_only_count_when_the_app_is_waiting(phrase, state, expected):
    assert pantry.parse_deduction_reply(phrase, state) == expected


# ================================================================ chat integration
@pytest.fixture
def client():
    with make_client() as c:
        yield c


def spy(monkeypatch, script=None, **kw):
    fake = FakeBedrock(script or [text("must not be called")], **kw)
    monkeypatch.setattr(main, "bedrock", fake)
    return fake


def chat(client, message, session_id=SID):
    return client.post("/api/chat", json={"message": message, "session_id": session_id})


def test_plain_pantry_requests_are_done_by_the_app(client, monkeypatch):
    fake = spy(monkeypatch)
    r = chat(client, "add 6 eggs and 500 g of rice to my pantry").json()
    assert r["response"] == "Added 6 eggs and 500 g rice to your pantry."
    assert "eggs" in chat(client, "what's in my pantry?").json()["response"]
    assert "Removed eggs" in chat(client, "I'm out of eggs").json()["response"]
    assert "egg" not in amounts() and "rice" in amounts() and fake.calls == []


def test_chat_pantry_requests_report_problems_in_words(client, monkeypatch):
    spy(monkeypatch)
    chat(client, "add 500 g rice to my pantry")
    assert "can't add" in chat(client, "add 3 cans rice to my pantry").json()["response"]
    assert "isn't in your pantry" in chat(client, "remove saffron from my pantry").json()["response"]


def test_servings_by_voice_while_cooking(client, monkeypatch):
    spy(monkeypatch, [tools(("start_recipe", {"recipe": "lemon soy chicken"})), text("Starting.")])
    chat(client, "let's make the lemon soy chicken")
    fake = spy(monkeypatch)
    r = chat(client, "cook for 4").json()
    assert "scaled to 4 servings" in r["response"] and r["state"]["session"]["servings"] == 4 and fake.calls == []
    assert "scale" not in chat(client, "set a 4 minute timer for eggs").json()["response"].lower()


def test_servings_phrase_without_a_recipe_goes_to_the_model(client, monkeypatch):
    fake = spy(monkeypatch, [text("What would you like to cook?")])
    assert chat(client, "cook for 4").json()["response"] == "What would you like to cook?" and len(fake.calls) == 1


def test_finishing_a_recipe_asks_and_the_answer_is_handled_by_the_app(client, monkeypatch):
    pantry.update_pantry("add", LEMON_SOY_PANTRY)
    cooking.start_recipe(SID, "lemon soy chicken")
    for _ in range(4):
        cooking.next_step(SID)
    spy(monkeypatch, [tools(("next_step", {})), text("Enjoy your meal!")])
    r = chat(client, "please go ahead and move to the following step").json()   # not a plain "next": the model path
    assert r["response"] == "Enjoy your meal! Want me to subtract the ingredients from your pantry?"
    assert r["state"]["pantry_deduction"]["state"] == "pending" and "2 chicken breasts" in r["state"]["pantry_deduction"]["used"]

    fake = spy(monkeypatch)
    done = chat(client, "yes").json()
    assert done["response"].startswith("Done. I subtracted 2 chicken breasts") and done["response"].endswith("Say undo to put it back.")
    assert amounts()["chicken breast"] == (1.0, "piece") and done["state"]["pantry_deduction"]["state"] == "applied"
    assert "put" in chat(client, "undo").json()["response"] and amounts()["chicken breast"] == (3.0, "piece")
    assert fake.calls == []  # the whole yes / undo exchange never touched the model


def test_declining_leaves_the_pantry_untouched(client, monkeypatch):
    pantry.update_pantry("add", LEMON_SOY_PANTRY)
    finish_recipe()
    spy(monkeypatch)
    assert chat(client, "no thanks").json()["response"] == "Okay, I left your pantry as it was."
    assert amounts()["chicken breast"] == (3.0, "piece")


def test_an_invented_pantry_claim_is_replaced(client, monkeypatch):
    spy(monkeypatch, [text("I've added eggs to your pantry.")])
    assert "couldn't change your pantry" in chat(client, "stock up on eggs please").json()["response"]
    assert "egg" not in amounts()
    spy(monkeypatch, [tools(("update_pantry", {"action": "add", "items": ["6 eggs"]})), text("I've added 6 eggs to your pantry.")])
    assert chat(client, "stock up on 6 eggs please").json()["response"] == "I've added 6 eggs to your pantry."
    assert amounts()["egg"] == (6.0, "piece")


def test_model_tools_change_the_same_pantry(client, monkeypatch):
    spy(monkeypatch, [tools(("update_pantry", {"action": "add", "items": ["2 lemons", "500 g rice"]}), ("get_pantry", {})), text("Done.")])
    chat(client, "restock the kitchen with lemons and rice")
    assert amounts() == {"lemon": (2.0, "piece"), "rice": (500.0, "g")}


# ============================================================== HTTP API and security
def post(client, path, body, **kw):
    return client.post(path, json=body, **kw)


def test_pantry_endpoints_roundtrip_and_units(client):
    assert client.get("/api/pantry").json() == {"units": "metric", "units_set": False, "items": []}
    r = post(client, "/api/pantry", {"action": "add", "items": ["6 eggs", "500 g rice"]}).json()
    assert [i["display"] for i in r["items"]] == ["6 eggs", "500 g rice"]
    assert post(client, "/api/preferences", {"units": "us"}).json() == {"units": "us"}
    got = client.get("/api/pantry").json()
    assert got["units_set"] is True and "1.1 lb rice" in [i["display"] for i in got["items"]]
    assert post(client, "/api/pantry", {"action": "remove", "items": ["eggs"]}).json()["summary"].startswith("Removed")
    state = client.get("/api/state", params={"session_id": SID}).json()
    assert state["units"] == "us" and state["pantry_deduction"] == {"state": None}


def test_servings_and_decision_endpoints(client):
    pantry.update_pantry("add", LEMON_SOY_PANTRY)
    cooking.start_recipe(SID, "lemon soy chicken")
    assert post(client, "/api/servings", {"session_id": SID, "servings": 4}).json()["servings"] == 4
    assert client.get("/api/state", params={"session_id": SID}).json()["session"]["servings"] == 4
    while cooking.next_step(SID).get("status") != "completed":
        pass
    assert post(client, "/api/pantry/deduct", {"session_id": SID, "decision": "apply"}).json()["status"] == "subtracted"
    assert post(client, "/api/pantry/deduct", {"session_id": SID, "decision": "apply"}).status_code == 400
    assert post(client, "/api/pantry/deduct", {"session_id": SID, "decision": "undo"}).json()["status"] == "undone"


@pytest.mark.parametrize("path,body", [
    ("/api/pantry", {"action": "steal", "items": ["eggs"]}), ("/api/pantry", {"action": "add", "items": []}),
    ("/api/pantry", {"action": "add", "items": ["x" * 81]}), ("/api/pantry", {"action": "add", "items": ["e"] * 21}),
    ("/api/pantry", {"action": "add", "items": [""]}), ("/api/pantry", {"action": "add", "items": [5]}),
    ("/api/pantry", {"action": "add", "items": ["eggs"], "owner": "u:someone-else"}), ("/api/pantry", {"items": ["eggs"]}),
    ("/api/preferences", {"units": "imperial"}), ("/api/preferences", {"units": "us", "owner": "x"}),
    ("/api/servings", {"session_id": SID, "servings": 0}), ("/api/servings", {"session_id": SID, "servings": 25}),
    ("/api/servings", {"session_id": SID, "servings": "four"}), ("/api/servings", {"session_id": "bad id", "servings": 2}),
    ("/api/pantry/deduct", {"session_id": SID, "decision": "maybe"}), ("/api/pantry/deduct", {"session_id": SID}),
])
def test_pantry_endpoints_reject_malformed_input(client, path, body):
    r = post(client, path, body)
    assert r.status_code == 422 and "Traceback" not in r.text


def test_pantry_endpoints_require_sign_in_and_same_origin():
    anon = make_client(None)
    assert anon.get("/api/pantry").status_code == 401
    for path, body in (("/api/pantry", {"action": "add", "items": ["eggs"]}), ("/api/preferences", {"units": "us"}),
                       ("/api/servings", {"session_id": SID, "servings": 2}), ("/api/pantry/deduct", {"session_id": SID, "decision": "skip"})):
        assert post(anon, path, body).status_code == 401
        assert post(make_client(), path, body, headers={"Origin": "https://evil.example"}).status_code == 403


def test_pantry_is_isolated_between_users_over_http():
    alice, bob = make_client("alice"), make_client("bob")
    post(alice, "/api/pantry", {"action": "add", "items": ["6 eggs"]})
    post(alice, "/api/preferences", {"units": "us"})
    assert bob.get("/api/pantry").json() == {"units": "metric", "units_set": False, "items": []}
    post(bob, "/api/pantry", {"action": "remove", "items": ["eggs"]})
    assert [i["item"] for i in alice.get("/api/pantry").json()["items"]] == ["egg"]
    with cooking.acting_as("u:alice"):
        pantry.update_pantry("add", LEMON_SOY_PANTRY)
        finish_recipe()
    for path, body in (("/api/servings", {"session_id": SID, "servings": 4}), ("/api/pantry/deduct", {"session_id": SID, "decision": "apply"})):
        assert post(bob, path, body).status_code == 404
    assert alice.get("/api/state", params={"session_id": SID}).json()["pantry_deduction"]["state"] == "pending"
    assert bob.get("/api/state", params={"session_id": SID}).status_code == 404


def test_pantry_writes_are_rate_limited(client):
    codes = [post(client, "/api/pantry", {"action": "add", "items": [f"thing {i}"]}).status_code for i in range(62)]
    assert codes.count(200) == 60 and codes[-1] == 429
