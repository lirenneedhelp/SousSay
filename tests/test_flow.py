import pytest

import cooking
import main
import pantry
from conftest import SID, FakeBedrock, make_client, text, tools


@pytest.fixture
def client():
    with make_client() as c:
        yield c


def spy(monkeypatch, script=None, **kw):
    fake = FakeBedrock(script or [text("must not be called")], **kw)
    monkeypatch.setattr(main, "bedrock", fake)
    return fake


def chat(client, message):
    return client.post("/api/chat", json={"message": message, "session_id": SID})


# ------------------------------------------------------------------ phrase parsing
@pytest.mark.parametrize("phrase,expected", [
    ("next", "next"), ("Next step", "next"), ("okay, next", "next"), ("continue", "next"), ("done", "next"), ("what's next?", "next"),
    ("repeat that", "repeat"), ("say that again", "repeat"), ("what's the current step", "repeat"), ("again", "repeat"),
    ("go back", "previous"), ("previous step", "previous"), ("back", "previous"),
])
def test_navigation_phrases(phrase, expected):
    assert cooking.parse_navigation(phrase) == expected


@pytest.mark.parametrize("phrase", ["what step am I on", "next time use less salt", "I'm done with the onions, what now", "tell me about step three",
                                    "go back to the beginning and explain", "set a timer"])
def test_longer_or_different_requests_are_not_navigation(phrase):
    assert cooking.parse_navigation(phrase) is None


@pytest.mark.parametrize("phrase,expected", [
    ("let's make the lemon soy chicken", ("lemon-soy-chicken", None)), ("I'll cook shakshuka for 4", ("shakshuka", 4)),
    ("start the veggie stir fry", ("veggie-stir-fry", None)), ("can we do the red lentil soup for six people", ("red-lentil-soup", 6)),
    ("let's go with the lemon garlic salmon", ("lemon-garlic-salmon", None)), ("make fluffy pancakes", ("fluffy-pancakes", None)),
])
def test_recipe_choices_are_recognised(phrase, expected):
    assert cooking.match_recipe_in_text(SID, phrase) == expected


@pytest.mark.parametrize("phrase", ["how long does shakshuka take?", "what goes in the lemon soy chicken", "let's make the chicken",
                                    "shakshuka", "I like shakshuka", "make shakshuka or pancakes", "tell me the ingredients for shakshuka"])
def test_questions_and_ambiguous_mentions_are_left_to_the_model(phrase):
    assert cooking.match_recipe_in_text(SID, phrase) is None


# ------------------------------------------------------------------ chat behaviour
def test_stepping_with_no_recipe_says_so_instead_of_inventing_steps(client, monkeypatch):
    fake = spy(monkeypatch)
    for phrase in ("next", "repeat that", "go back"):
        r = chat(client, phrase).json()
        assert r["response"].startswith("We haven't started a recipe yet") and r["state"]["session"]["recipe"] is None
    assert fake.calls == []


def test_starting_a_recipe_by_name_is_done_by_the_app(client, monkeypatch):
    pantry.update_pantry("add", ["3 chicken breast", "1 lemon", "500 g rice", "200 ml soy sauce", "30 g ginger"])
    fake = spy(monkeypatch)
    r = chat(client, "let's make the lemon soy chicken").json()
    assert r["response"].startswith("Starting Lemon Soy Chicken with Rice for 2 servings. Safety: Chicken must reach 165")
    assert "Step 1 of 5: Mix the juice" in r["response"]
    assert r["state"]["session"]["recipe"]["id"] == "lemon-soy-chicken" and r["state"]["session"]["step"]["step_number"] == 1
    assert fake.calls == []


def test_starting_with_servings_and_reporting_shortfalls(client, monkeypatch):
    pantry.update_pantry("add", ["2 chicken breast", "1 lemon", "500 g rice", "200 ml soy sauce", "30 g ginger"])
    spy(monkeypatch)
    r = chat(client, "I'll cook the lemon soy chicken for 4").json()
    assert "for 4 servings" in r["response"] and "You're short on chicken breast and lemon." in r["response"]
    assert r["state"]["session"]["servings"] == 4


def test_next_repeat_and_back_walk_the_recipe(client, monkeypatch):
    spy(monkeypatch)
    chat(client, "start shakshuka")
    assert chat(client, "next").json()["response"].startswith("Step 2 of 4: Stir in the cumin")
    assert chat(client, "repeat that").json()["response"].startswith("Step 2 of 4: Stir in the cumin")
    assert chat(client, "go back").json()["response"].startswith("Step 1 of 4: Soften")
    assert "Already at the first step." in chat(client, "back").json()["response"]
    assert chat(client, "next").json()["state"]["session"]["step"]["step_number"] == 2


def test_timer_hints_are_offered_and_yes_starts_exactly_that_timer(client, monkeypatch):
    spy(monkeypatch)
    r = chat(client, "make the lemon soy chicken").json()
    assert "Want me to set a timer" not in r["response"]                      # step 1 has no wait
    r = chat(client, "next").json()                                          # step 2: marinate 30 minutes
    assert r["response"].endswith("Want me to set a timer for 30 minutes called chicken marinade?")
    done = chat(client, "yes").json()
    assert done["response"] == "Okay, chicken marinade timer set for 30 minutes."
    assert [(t["label"], t["remaining_s"]) for t in done["state"]["timers"]] == [("chicken marinade", 1800)]
    assert "Want me to set a timer" not in chat(client, "repeat that").json()["response"]   # already running: no second offer


def test_yes_without_an_offer_goes_to_the_model(client, monkeypatch):
    fake = spy(monkeypatch, [text("Yes to what?")])
    assert chat(client, "yes").json()["response"] == "Yes to what?" and len(fake.calls) == 1


def test_yes_after_the_step_changed_does_not_set_a_stale_timer(client, monkeypatch):
    fake = spy(monkeypatch, [text("Sure.")], repeat_last=True)
    chat(client, "make the lemon soy chicken"); chat(client, "next")           # offer for the marinade
    chat(client, "next")                                                     # step 3 (rice) offers a different timer
    r = chat(client, "yes").json()
    assert [t["label"] for t in r["state"]["timers"]] == ["rice"]            # the rice offer, not the stale marinade one


def test_finishing_by_voice_asks_about_the_pantry(client, monkeypatch):
    pantry.update_pantry("add", ["3 chicken breast", "1 lemon", "500 g rice", "200 ml soy sauce", "30 g ginger"])
    spy(monkeypatch)
    chat(client, "make the lemon soy chicken")
    for _ in range(4):
        chat(client, "next")
    r = chat(client, "next").json()
    assert r["response"] == "That was the last step. Enjoy! Want me to subtract the ingredients from your pantry?"
    assert chat(client, "yes").json()["response"].startswith("Done. I subtracted 2 chicken breasts")


def test_substitutions_are_mentioned_in_the_step(client, monkeypatch):
    spy(monkeypatch)
    chat(client, "make the lemon soy chicken")
    cooking.apply_substitution(SID, "lemon", "lime")
    assert "(Use lime instead of lemon.)" in chat(client, "repeat that").json()["response"]


def test_a_recipe_question_still_reaches_the_model(client, monkeypatch):
    fake = spy(monkeypatch, [text("About 30 minutes.")])
    assert chat(client, "how long does shakshuka take?").json()["response"] == "About 30 minutes." and len(fake.calls) == 1


def test_invented_steps_with_no_recipe_are_replaced(client, monkeypatch):
    spy(monkeypatch, [text('The next step is: "While the chicken is baking, cook the rice according to the package instructions."')])
    r = chat(client, "what do I do now").json()
    assert r["response"].startswith("We haven't started a recipe yet") and "baking" not in r["response"]
    assert "baking" not in str(cooking.load_history(SID))                    # and the invention never becomes history


def test_real_steps_read_by_the_model_after_a_tool_call_are_kept(client, monkeypatch):
    spy(monkeypatch, [tools(("start_recipe", {"recipe": "shakshuka"})), text("Starting. The first step is to soften the onion and pepper.")])
    r = chat(client, "please get going with that egg and tomato dish").json()
    assert r["response"].startswith("Starting. The first step") and r["state"]["session"]["recipe"]["id"] == "shakshuka"


def test_an_old_offer_never_starts_a_timer_for_a_different_step():
    cooking.start_recipe(SID, "lemon soy chicken")
    cooking.next_step(SID); cooking.next_step(SID)                       # now on the rice step (timer hint: rice)
    cooking.add_messages(SID, [("user", "next"), ("assistant", "Step 2 of 5: ... Want me to set a timer for 30 minutes called chicken marinade?")])
    assert cooking.pending_timer_offer(SID) is None                      # the offer is for another step
    cooking.add_messages(SID, [("user", "next"), ("assistant", "Step 3 of 5: ... Want me to set a timer for 15 minutes called rice?")])
    assert cooking.pending_timer_offer(SID) == ("rice", 900)
    cooking.set_timer(SID, "rice", minutes=15)
    assert cooking.pending_timer_offer(SID) is None                      # already running: nothing to offer
