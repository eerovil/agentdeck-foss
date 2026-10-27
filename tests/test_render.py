
from agentdeck.web.render import ctx_level, ktok


def test_ktok_compact():
    assert ktok(940) == "940"
    assert ktok(1200) == "1.2k"
    assert ktok(47000) == "47k"
    assert ktok(None) == ""


def test_ctx_level_thresholds():
    # green (no modifier) below warn, amber past the 1M-window halfway mark, red
    # in the near-full zone.
    assert ctx_level(50_000) == ""
    assert ctx_level(499_999) == ""
    assert ctx_level(500_000) == "warn"
    assert ctx_level(799_999) == "warn"
    assert ctx_level(800_000) == "crit"
    assert ctx_level(846_463) == "crit"
    assert ctx_level(None) == ""
