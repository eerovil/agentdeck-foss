
from agentdeck.web.render import ctx_level, ktok


def test_ktok_compact():
    assert ktok(940) == "940"
    assert ktok(1200) == "1.2k"
    assert ktok(47000) == "47k"
    assert ktok(None) == ""


def test_ctx_level_thresholds():
    # The cost dot's limits (#4168): #4059 compacts near 200k, so 500k was never reached.
    assert ctx_level(50_000) == ""
    assert ctx_level(99_999) == ""
    assert ctx_level(100_000) == "warn"
    assert ctx_level(149_999) == "warn"
    assert ctx_level(150_000) == "crit"
    assert ctx_level(846_463) == "crit"
    assert ctx_level(None) == ""
