import pytest

import cooking
from cooking import CookingError
from conftest import SID


def test_invalid_session_id_rejected():
    for bad in ["", "short", "has space here", "x" * 65, "semi;colon-id"]:
        with pytest.raises(CookingError):
            cooking.ensure_session(bad)


# ------------------------------------------------------------------ search
def test_search_ranks_by_ingredient_coverage():
    r = cooking.search_recipes(SID, ["chicken", "rice", "lemon", "ginger", "soy sauce"])
    assert r["results"][0]["id"] == "lemon-soy-chicken"
    assert r["results"][0]["match_pct"] == 100 and r["results"][0]["missing"] == []
    assert "note" not in r


def test_search_uses_the_users_pantry_when_no_ingredients_given():
    import pantry
    assert "empty" in cooking.search_recipes(SID)["note"]
    pantry.update_pantry("add", ["2 chicken breast", "1 lemon", "500 g rice", "200 ml soy sauce", "30 g ginger"])
    r = cooking.search_recipes(SID)
    assert r["used_inventory"] is True
    assert r["results"][0]["id"] == "lemon-soy-chicken" and r["results"][0]["short"] == []


def test_search_accepts_comma_string_and_plurals():
    r = cooking.search_recipes(SID, "eggs, canned tomatoes and onion")
    assert r["results"][0]["id"] == "shakshuka"


def test_search_no_match_signals_fallback():
    r = cooking.search_recipes(SID, ["dragonfruit"])
    assert r["results"] == [] and "create_recipe" in r["note"]


def test_search_staples_do_not_count_as_missing():
    top = cooking.search_recipes(SID, ["spaghetti", "garlic", "chili flakes", "parsley"])["results"][0]
    assert top["id"] == "garlic-chili-spaghetti" and top["missing"] == []  # olive oil is a staple


# ------------------------------------------------------------ start / steps
def test_start_recipe_rejects_empty_and_unknown():
    with pytest.raises(CookingError):
        cooking.start_recipe(SID, "")
    with pytest.raises(CookingError):
        cooking.start_recipe(SID, "moon pie")


def test_start_recipe_ambiguous_lists_candidates():
    with pytest.raises(CookingError) as e:
        cooking.start_recipe(SID, "chicken")
    assert "several" in str(e.value) and "Lemon Soy Chicken" in str(e.value)


def test_step_navigation_and_completion():
    start = cooking.start_recipe(SID, "lemon soy chicken")
    assert start["current_step"]["step_number"] == 1 and start["safety_notes"]
    assert cooking.next_step(SID)["step_number"] == 2
    assert cooking.get_current_step(SID)["step_number"] == 2
    assert cooking.previous_step(SID)["step_number"] == 1
    assert "first step" in cooking.previous_step(SID)["note"]
    for _ in range(5):
        last = cooking.next_step(SID)
    assert last["status"] == "completed"
    assert cooking.next_step(SID)["status"] == "completed"
    assert cooking.session_view(SID)["status"] == "completed"
    assert cooking.previous_step(SID)["step_number"] == 5  # can step back after finishing


def test_steps_require_active_recipe():
    for fn in (cooking.next_step, cooking.previous_step, cooking.get_current_step):
        with pytest.raises(CookingError):
            fn(SID)


def test_step_exposes_timer_hint():
    cooking.start_recipe(SID, "lemon-soy-chicken")
    step = cooking.next_step(SID)
    assert step["timer_hint"] == {"label": "chicken marinade", "duration_s": 1800}


def test_sessions_are_isolated():
    cooking.start_recipe("session-aaaa-1", "shakshuka")
    assert cooking.session_view("session-bbbb-1")["recipe"] is None


def test_progress_is_read_from_database_only():
    cooking.start_recipe(SID, "shakshuka")
    cooking.next_step(SID)
    cooking._recipes.cache_clear()  # nothing may depend on process memory
    assert cooking.session_view(SID)["step"]["step_number"] == 2


# ------------------------------------------------------------------ timers
def test_two_named_timers_expire_independently(clock):
    a = cooking.set_timer(SID, "rice", minutes=15)["timer"]
    b = cooking.set_timer(SID, "chicken", minutes=30)["timer"]
    assert a["id"] != b["id"] and a["remaining_s"] == 900
    clock.advance(901)
    timers = {t["label"]: t for t in cooking.list_timers(SID)["timers"]}
    assert timers["rice"]["status"] == "expired" and timers["chicken"]["status"] == "running"
    assert timers["chicken"]["remaining_s"] == 1800 - 901
    assert [t["label"] for t in cooking.pop_newly_expired(SID)] == ["rice"]
    assert cooking.pop_newly_expired(SID) == []  # reported exactly once
    clock.advance(1000)
    assert [t["label"] for t in cooking.pop_newly_expired(SID)] == ["chicken"]


def test_timer_ids_never_reused_and_survive_new_recipe(clock):
    first = cooking.set_timer(SID, "a", seconds=30)["timer"]["id"]
    cooking.cancel_timer(SID, timer_id=first)
    cooking.start_recipe(SID, "shakshuka")  # must not wipe timers
    second = cooking.set_timer(SID, "b", seconds=30)["timer"]["id"]
    assert second > first
    assert [t["label"] for t in cooking.list_timers(SID)["timers"]] == ["b"]


def test_timer_validation():
    for kwargs in ({"minutes": 0, "seconds": 0}, {"minutes": 721}, {"minutes": -5}):
        with pytest.raises(CookingError):
            cooking.set_timer(SID, "x", **kwargs)
    with pytest.raises(CookingError):
        cooking.set_timer(SID, "  ", minutes=1)
    with pytest.raises(CookingError):
        cooking.set_timer(SID, "x", minutes="soon")


def test_cancel_by_label_by_id_and_ambiguity(clock):
    cooking.set_timer(SID, "pasta", minutes=5)
    t2 = cooking.set_timer(SID, "pasta sauce", minutes=5)["timer"]["id"]
    with pytest.raises(CookingError) as e:
        cooking.cancel_timer(SID, label="pasta")
    assert "Several" in str(e.value)
    assert cooking.cancel_timer(SID, timer_id=t2)["status"] == "timer_cancelled"
    assert cooking.cancel_timer(SID, label="pasta")["status"] == "timer_cancelled"
    with pytest.raises(CookingError):
        cooking.cancel_timer(SID, label="pasta")


def test_dismissing_expired_timer(clock):
    tid = cooking.set_timer(SID, "eggs", seconds=10)["timer"]["id"]
    clock.advance(11)
    cooking.cancel_timer(SID, timer_id=tid)
    assert cooking.list_timers(SID)["timers"] == [] and cooking.pop_newly_expired(SID) == []


def test_running_timer_cap():
    for i in range(cooking.MAX_RUNNING_TIMERS):
        cooking.set_timer(SID, f"t{i}", minutes=5)
    with pytest.raises(CookingError):
        cooking.set_timer(SID, "one too many", minutes=5)


def test_timers_are_per_session(clock):
    cooking.set_timer("session-aaaa-1", "mine", minutes=1)
    assert cooking.list_timers("session-bbbb-1")["timers"] == []


# ----------------------------------------------------------------- adaptation
def test_suggest_and_apply_substitution():
    assert cooking.suggest_substitution(SID, "lemon")["options"][0]["sub"] == "lime"
    assert cooking.suggest_substitution(SID, "unobtainium")["found"] is False
    with pytest.raises(CookingError):
        cooking.apply_substitution(SID, "lemon", "lime")  # no active recipe
    cooking.start_recipe(SID, "lemon-soy-chicken")
    with pytest.raises(CookingError):
        cooking.apply_substitution(SID, "saffron", "turmeric")  # not in recipe
    out = cooking.apply_substitution(SID, "chicken breast", "chicken thighs")
    assert out["safety_check"]["min_internal_temp_f"] == 165
    assert "use chicken thighs instead of chicken breast" in cooking.state_summary(SID)
    assert cooking.get_current_step(SID)["adaptations"]


# ------------------------------------------------------- AI generation guardrails
GOOD = dict(title="Garlic Chicken Bowl",
            ingredients=[{"name": "chicken breast", "qty": "2"}, {"name": "garlic", "qty": "3 cloves"}],
            steps=["Slice the chicken breast thinly and season it.",
                   "Sear in a hot pan until it reaches 165°F (74°C) inside.",
                   "Add garlic for 30 seconds and serve."])


def test_generated_recipe_requires_safe_temperature_step():
    bad = {**GOOD, "steps": ["Slice the chicken breast thinly.", "Fry it until golden brown.", "Add garlic and serve."]}
    with pytest.raises(CookingError) as e:
        cooking.create_recipe(SID, **bad)
    assert "165" in str(e.value)


def test_generated_recipe_is_labelled_stored_and_startable():
    out = cooking.create_recipe(SID, **GOOD)
    assert out["ai_generated"] and any("165" in n for n in out["safety_notes"])
    started = cooking.start_recipe(SID, out["recipe_id"])
    assert started["ai_generated"] is True
    assert "(AI-generated)" in cooking.state_summary(SID)
    assert cooking.search_recipes("session-other-1", ["garlic"])["results"][0]["ai_generated"] is False  # private to session


@pytest.mark.parametrize("step", [
    "Rinse the raw chicken under the tap before cooking.",
    "Let the chicken thaw on the counter for 6 hours.",
    "Serve the chicken pink in the middle.",
])
def test_generated_recipe_blocks_unsafe_practices(step):
    bad = {**GOOD, "steps": [GOOD["steps"][0], step, "Cook to 165°F (74°C) and serve."]}
    with pytest.raises(CookingError):
        cooking.create_recipe(SID, **bad)


def test_generated_recipe_shape_limits():
    for patch in ({"title": "ab"}, {"steps": ["too short"]}, {"ingredients": [{"name": "salt", "qty": "1"}]},
                  {"steps": ["x" * 301, "y" * 20, "z" * 20]}):
        with pytest.raises(CookingError):
            cooking.create_recipe(SID, **{**GOOD, **patch})


def test_generated_recipe_per_session_cap():
    veg = dict(title="Plain Veg Mix", ingredients=[{"name": "carrot", "qty": "1"}, {"name": "pea", "qty": "1"}],
               steps=["Chop the carrots into small pieces.", "Steam everything until tender.", "Season and serve warm."])
    for _ in range(cooking.MAX_GENERATED_PER_SESSION):
        cooking.create_recipe(SID, **veg)
    with pytest.raises(CookingError):
        cooking.create_recipe(SID, **veg)


# --------------------------------------------------------------- food safety
def test_food_safety_lookup():
    assert cooking.food_safety_lookup("chicken breast")["min_internal_temp_f"] == 165
    assert cooking.food_safety_lookup("ground beef")["min_internal_temp_f"] == 160
    assert cooking.food_safety_lookup("pork chop")["min_internal_temp_f"] == 145
    assert cooking.food_safety_lookup("salmon")["min_internal_temp_c"] == 63
    unknown = cooking.food_safety_lookup("tofu")
    assert unknown["found"] is False and "2 hours" in unknown["general_rules"]


# ---------------------------------------------------------------- conversion
def test_convert_measurements():
    assert cooking.convert_measurements(2, "cups", "grams", "flour")["converted"] == "240.0 g"
    assert cooking.convert_measurements(1, "tbsp", "ml")["converted"] == "14.79 ml"
    assert cooking.convert_measurements(350, "fahrenheit", "celsius")["converted"] == "177°C"
    assert cooking.convert_measurements(100, "C", "F")["converted"] == "212°F"
    assert cooking.convert_measurements(500, "g", "oz")["converted"].endswith("oz")
    with pytest.raises(CookingError):
        cooking.convert_measurements(1, "cup", "grams", "unicorn dust")


# ------------------------------------------------------------------ history
def test_history_starts_with_user_and_is_bounded():
    cooking.add_messages(SID, [("user", "hi"), ("assistant", "hello")] * 8)
    hist = cooking.load_history(SID, limit=5)
    assert hist[0]["role"] == "user" and len(hist) == 4  # odd leading assistant row dropped


def test_generated_recipe_allows_until_no_longer_pink():
    ok = {**GOOD, "steps": [GOOD["steps"][0], "Cook the chicken until no longer pink, to 165°F (74°C).", "Serve with garlic."]}
    assert cooking.create_recipe(SID, **ok)["status"] == "created"


@pytest.mark.parametrize("phrase,expected", [
    ("set a 8 minute timer for eggs", ("eggs", 480)),
    ("Set an 8 minute timer for the eggs please", ("eggs", 480)),
    ("set a timer for 15 minutes for rice", ("rice", 900)),
    ("start a timer for rice for 15 minutes", ("rice", 900)),
    ("set a five minute timer for pasta", ("pasta", 300)),
    ("set a twenty five minute timer for the roast", ("roast", 1500)),
    ("set a 90 second timer", ("kitchen", 90)),
    ("set a 1 hour 30 minute timer for the stew", ("stew", 5400)),
    ("set a timer for an hour and a half for the roast", ("roast", 5400)),
    ("set a timer for half an hour called soup", ("soup", 1800)),
    ("set a 20 minute timer for Thai beef curry", ("thai beef curry", 1200)),
    ("can you set an 8-minute timer for eggs?", ("eggs", 480)),
])
def test_plain_timer_requests_are_parsed(phrase, expected):
    assert cooking.parse_set_timer(phrase) == expected


@pytest.mark.parametrize("phrase", [
    "set a timer", "set timers for rice and chicken", "set a 5 minute timer for eggs and a 10 minute timer for rice",
    "cancel the 8 minute timer", "how long left on the 8 minute timer", "what timer should I set for 8 minutes",
    "8 minutes for the eggs", "how long should I set the timer", "tell me about timers",
])
def test_ambiguous_or_other_requests_are_left_to_the_model(phrase):
    assert cooking.parse_set_timer(phrase) is None
