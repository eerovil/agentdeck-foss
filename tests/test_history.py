from agentdeck.providers.claude_code import history


def test_load_history_title_and_last_prompt(fake_config_dir):
    hist = history.load_history(fake_config_dir.path)
    live = hist[fake_config_dir.live_sid]
    assert live.title == "First prompt"  # first display wins
    assert live.last_prompt == "Second prompt"  # most recent
    assert live.project == "/tmp/proj"


def test_load_history_skips_malformed(fake_config_dir):
    hist = history.load_history(fake_config_dir.path)
    # only the two real session ids, malformed line ignored
    assert set(hist) == {fake_config_dir.live_sid, fake_config_dir.idle_sid}


def test_load_history_missing_file(tmp_path):
    assert history.load_history(tmp_path) == {}
