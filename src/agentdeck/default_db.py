"""Where the deck keeps its database when the config names no other place.

A module of its own, importing nothing, so the test suite can point every default
config at a temporary file (#3153) without `scripts/select_suite.py` reading its
shared setup as running `config.py` and everything behind it for every test.
"""

# Spelled without the literal file name, which the test selector would read as the
# dotted name of `agentdeck/db.py` and so as an import of it.
DEFAULT_DB_PATH = "~/.local/share/agentdeck/" + "agentdeck" + ".db"
