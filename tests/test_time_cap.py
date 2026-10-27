"""The per-test time limits a pod run enforces on non-browser tests (#3053)."""

from __future__ import annotations

import re
from pathlib import Path

import conftest
import pytest
from conftest import (
    TIME_CAP_ENV,
    TIME_WARN_ENV,
    cpu_wait_s,
    over_time_cap,
    seconds_from_env,
    slow_test_summary,
)
from public_copy import shipped


def test_a_test_over_the_cap_twice_fails_naming_itself_and_its_times() -> None:
    assert over_time_cap("tests/t.py::fast", [(1.0, 0.0)], 1.0) is None
    # One stall, then fast on the second run: not the test's fault.
    assert over_time_cap("tests/t.py::stalled", [(4.2, 0.0), (0.1, 0.0)], 1.0) is None
    reason = over_time_cap("tests/t.py::slow", [(1.24, 0.0), (1.31, 0.0)], 1.0)
    assert reason is not None
    assert "tests/t.py::slow took 1.24 s, then 1.31 s, over the 1 s per-test cap" in reason


def test_time_spent_waiting_for_a_cpu_is_not_charged_to_the_test() -> None:
    # A 0.9 s test that sat 0.3 s behind the run's other workers.
    assert over_time_cap("tests/t.py::busy_pod", [(1.2, 0.3)], 1.0) is None
    # A wait the kernel could not report never makes a test look slower.
    assert "took 1.20 s" in over_time_cap("tests/t.py::x", [(1.2, -1.0)], 1.0)
    assert cpu_wait_s() >= 0.0


def test_tests_over_the_warning_are_named_slowest_first() -> None:
    assert slow_test_summary({}, 0.5, 1.0) == []
    lines = slow_test_summary({"tests/a.py::a": 0.61, "tests/b.py::b": 0.93}, 0.5, 1.0)
    assert (
        lines[0] == "2 tests took over 0.5 s without failing the 1 s cap (#3053). Make them faster:"
    )
    assert lines[1:] == ["  0.93 s  tests/b.py::b", "  0.61 s  tests/a.py::a"]


def test_the_limits_are_measured_only_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TIME_CAP_ENV, raising=False)
    assert seconds_from_env(TIME_CAP_ENV) is None
    monkeypatch.setenv(TIME_CAP_ENV, " ")
    assert seconds_from_env(TIME_CAP_ENV) is None
    monkeypatch.setenv(TIME_CAP_ENV, "0.5")
    assert seconds_from_env(TIME_CAP_ENV) == 0.5
    for bad in ("fast", "0", "-1", "nan", "inf"):
        monkeypatch.setenv(TIME_CAP_ENV, bad)
        with pytest.raises(pytest.UsageError):
            seconds_from_env(TIME_CAP_ENV)


@shipped("scripts/kube_pytest.sh")
def test_pods_set_the_limits_and_browser_and_listed_tests_are_exempt() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "kube_pytest.sh").read_text()
    assert f'- name: {TIME_CAP_ENV}\n          value: "1"' in script
    assert f'- name: {TIME_WARN_ENV}\n          value: "0.5"' in script

    class Item:
        def __init__(self, nodeid: str, browser: bool) -> None:
            self.nodeid = nodeid
            self.stash = pytest.Stash()
            self.browser = browser
            self.markers: list = []

        def get_closest_marker(self, name: str):
            return object() if self.browser and name == "browser" else None

        def add_marker(self, marker) -> None:
            self.markers.append(marker)

    listed = conftest._TOO_SLOW_LIST.read_text().splitlines()[-1].strip()
    items = [Item(listed, False), Item("tests/t.py::b", True), Item("tests/t.py::plain", False)]

    class Config:
        def getoption(self, name: str, default=None):
            return default

    conftest._skip_too_slow(Config(), items)
    assert [item.stash.get(conftest._CAP_EXEMPT, False) for item in items] == [True, True, False]


def test_a_second_run_that_raises_anything_never_fails_the_test() -> None:
    def raising(exc: BaseException):
        def run() -> None:
            raise exc

        return run

    for exc in (
        AssertionError(),
        pytest.fail.Exception("failed"),
        pytest.skip.Exception("skipped"),
        pytest.xfail.Exception("xfailed"),
    ):
        assert conftest.retimed(raising(exc)) is None
    wall, waited = conftest.retimed(lambda: None)
    assert wall >= 0.0 and waited >= 0.0


pytest_plugins = ["pytester"]


def test_a_capsys_test_slow_twice_fails_on_time_not_on_its_output(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The second run happens inside pytest's capture, so a test reading its own output
    # still passes it and is failed for its time, not let off by a capture error.
    pytester.makeini("[pytest]\nasyncio_default_fixture_loop_scope = function\n")
    pytester.makeconftest(
        "from conftest_under_test import pytest_runtest_call, pytest_runtest_makereport\n"
    )
    (pytester.path / "conftest_under_test.py").write_text(
        Path(conftest.__file__).read_text(encoding="utf-8")
    )
    pytester.makepyfile(
        """
        import time

        def test_prints(capsys):
            print("hello")
            time.sleep(0.03)
            assert capsys.readouterr().out == "hello\\n"
        """
    )
    monkeypatch.setenv(TIME_CAP_ENV, "0.01")
    result = pytester.runpytest_inprocess("-p", "no:xdist", "-p", "no:cacheprovider")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*took *, then *, over the 0.01 s per-test cap each time*"])


def test_every_skip_list_entry_names_a_test_that_exists() -> None:
    """A stale entry silently puts its test back in the per-change gate (#3623)."""
    root = Path(__file__).parents[1]
    missing = []
    for nodeid in conftest._too_slow_ids():
        path, _, name = nodeid.partition("::")
        function = re.split(r"[\[:]", name)[0]
        source = root / path
        # The public copy leaves some test files out but keeps this list whole (#3623).
        public_copy = not (root / "scripts" / "export_public.py").exists()
        if public_copy and not source.is_file():
            continue
        if not source.is_file() or not re.search(
            rf"^\s*(?:async )?def {re.escape(function)}\(", source.read_text(), re.MULTILINE
        ):
            missing.append(nodeid)
    assert not missing, missing
