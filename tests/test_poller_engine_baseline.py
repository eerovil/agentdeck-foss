"""Attributing a failing verification run to the branch or to its base (#1737)."""

from __future__ import annotations

from agentdeck.poller_engine import baseline

EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _workspace(
    *,
    head: str = "branchsha",
    base_head: str = "basesha",
    clean: bool = False,
    provable: bool = True,
) -> dict:
    return {
        "head": head,
        "base_head": base_head,
        "base_ref": "origin/master",
        "repo_identity": "repo-id",
        "command_fingerprint": "cmd-id",
        "staged_sha256": EMPTY,
        "tracked_diff_sha256": EMPTY,
        "untracked_sha256": EMPTY,
        "untracked_count": 0,
        "provable": provable,
        "clean": clean,
    }


def _job(
    *,
    failures: tuple[str, ...] = (),
    complete: bool = True,
    workspace: dict | None = None,
    result_class: str = "failure",
    environment: str = "local:deps1",
) -> dict:
    return {
        "id": "job-1",
        "result_class": result_class,
        "environment": environment,
        "failures": list(failures),
        "failures_complete": complete,
        "finished_at": 100.0,
        "tested_workspace": workspace if workspace is not None else _workspace(),
    }


def _base_entry(job: dict) -> dict:
    """The stored base result a branch job would find, built the way the host builds it."""
    entry = baseline.baseline_entry(job)
    assert entry is not None
    return entry


# ---- reading a failure list ------------------------------------------------


def test_a_failure_list_is_read_from_the_summary_pytest_prints_last():
    text = (
        "tests/test_a.py::test_one FAILED\n"
        "=========== short test summary info ============\n"
        "FAILED tests/test_a.py::test_one - AssertionError: nope\n"
        "ERROR tests/test_b.py::test_two\n"
        "========== 1 failed, 2 errors in 4.10s =========\n"
    )

    failures, complete = baseline.extract_failures(text)

    assert failures == ("tests/test_a.py::test_one", "tests/test_b.py::test_two")
    assert complete is True


def test_an_all_pass_run_is_a_complete_empty_failure_list():
    """Pytest prints the summary section only when it has something to put in it.

    Reading an all-pass suite as "list unreadable" would leave the feature with no clean-base
    case at all -- the green baseline is exactly the one every card wants to compare against.
    """
    all_pass = (
        "........................................................ [100%]\n"
        "=============================== warnings summary ===============================\n"
        "  /venv/lib/python3.12/site-packages/fastapi/testclient.py:1: "
        "StarletteDeprecationWarning: ...\n"
        "-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html\n"
        "6302 passed, 5 skipped, 12 warnings in 251.51s (0:04:11)\n"
    )

    assert baseline.extract_failures(all_pass) == ((), False), (
        "there is nothing in this output to read a failure list out of"
    )
    assert baseline.extract_failures(all_pass, succeeded=True) == ((), True)


def test_a_green_run_becomes_a_baseline_the_way_a_red_one_does():
    green = _job(
        result_class="success",
        failures=(),
        complete=True,
        workspace=_workspace(head="basesha", base_head="basesha", clean=True),
    )

    entry = baseline.baseline_entry(green)

    assert entry is not None
    assert entry["failures"] == []


def test_output_without_the_summary_header_is_never_called_complete():
    # The bounded tail did not reach the header, or the command was not pytest at all.
    # Either way the list cannot be trusted, and an untrusted list must not compare.
    failures, complete = baseline.extract_failures(
        "FAILED tests/test_a.py::test_one - AssertionError\n"
    )

    assert failures == ()
    assert complete is False


def test_a_run_with_more_failures_than_the_bound_reports_an_incomplete_list():
    lines = "\n".join(
        f"FAILED tests/test_{index}.py::test_it" for index in range(baseline.MAX_FAILURES + 5)
    )

    failures, complete = baseline.extract_failures(f"short test summary info\n{lines}\n")

    assert len(failures) == baseline.MAX_FAILURES
    assert complete is False


def test_only_the_last_summary_in_a_log_is_read():
    text = (
        "short test summary info\nFAILED tests/old.py::test_stale\n"
        "short test summary info\nFAILED tests/new.py::test_fresh\n"
    )

    failures, complete = baseline.extract_failures(text)

    assert failures == ("tests/new.py::test_fresh",)
    assert complete is True


# ---- what may become a base result -----------------------------------------


def test_a_clean_provable_run_becomes_a_reusable_base_result():
    job = _job(
        failures=("tests/test_a.py::test_one",),
        workspace=_workspace(head="basesha", base_head="basesha", clean=True),
    )

    entry = baseline.baseline_entry(job)

    assert entry is not None
    assert entry["head"] == "basesha"
    assert entry["failures"] == ["tests/test_a.py::test_one"]
    assert entry["key"] == baseline.baseline_key(
        repo_identity="repo-id",
        head="basesha",
        command_fingerprint="cmd-id",
        environment="local:deps1",
    )


def test_a_run_over_an_edited_tree_is_not_a_base_result():
    # Nothing else can ever be at that state, so its result describes no commit.
    job = _job(workspace=_workspace(clean=False))

    assert baseline.baseline_entry(job) is None


def test_a_run_whose_failure_list_is_incomplete_is_not_a_base_result():
    job = _job(complete=False, workspace=_workspace(clean=True))

    assert baseline.baseline_entry(job) is None


def test_an_infrastructure_result_is_not_a_base_result():
    job = _job(result_class="infrastructure", workspace=_workspace(clean=True))

    assert baseline.baseline_entry(job) is None


def test_a_run_in_an_unnameable_environment_is_not_a_base_result():
    job = _job(environment="", workspace=_workspace(clean=True))

    assert baseline.baseline_entry(job) is None


def test_the_key_changes_with_the_commit_the_command_and_the_environment():
    base = {
        "repo_identity": "repo-id",
        "head": "basesha",
        "command_fingerprint": "cmd-id",
        "environment": "local:deps1",
    }
    key = baseline.baseline_key(**base)

    assert key != baseline.baseline_key(**{**base, "head": "othersha"})
    assert key != baseline.baseline_key(**{**base, "command_fingerprint": "other-cmd"})
    assert key != baseline.baseline_key(**{**base, "environment": "local:deps2"})
    assert baseline.baseline_key(**{**base, "repo_identity": ""}) == ""


# ---- the four comparisons --------------------------------------------------


def test_a_failure_against_a_green_base_is_this_branch_s_regression():
    green_base = _base_entry(
        _job(
            result_class="success",
            workspace=_workspace(head="basesha", base_head="basesha", clean=True),
        )
    )
    branch = _job(failures=("tests/test_a.py::test_one",))

    comparison = baseline.compare(branch, green_base)

    assert comparison.verdict == baseline.BRANCH_REGRESSION
    assert comparison.branch_only == ("tests/test_a.py::test_one",)
    assert comparison.blocks is True


def test_a_failure_the_base_already_has_is_pre_existing():
    red_base = _base_entry(
        _job(
            failures=("tests/test_a.py::test_one",),
            workspace=_workspace(head="basesha", base_head="basesha", clean=True),
        )
    )
    branch = _job(failures=("tests/test_a.py::test_one",))

    comparison = baseline.compare(branch, red_base)

    assert comparison.verdict == baseline.PRE_EXISTING
    assert comparison.pre_existing == ("tests/test_a.py::test_one",)
    assert comparison.branch_only == ()
    assert comparison.blocks is False


def test_one_new_failure_beside_a_known_one_is_still_a_regression():
    red_base = _base_entry(
        _job(
            failures=("tests/test_a.py::test_one",),
            workspace=_workspace(head="basesha", base_head="basesha", clean=True),
        )
    )
    branch = _job(failures=("tests/test_a.py::test_one", "tests/test_c.py::test_new"))

    comparison = baseline.compare(branch, red_base)

    assert comparison.verdict == baseline.BRANCH_REGRESSION
    assert comparison.branch_only == ("tests/test_c.py::test_new",)
    assert comparison.pre_existing == ("tests/test_a.py::test_one",)
    assert comparison.blocks is True


def test_a_baseline_for_a_different_commit_does_not_answer_for_this_one():
    stale = _base_entry(
        _job(
            failures=("tests/test_a.py::test_one",),
            workspace=_workspace(head="oldbasesha", base_head="oldbasesha", clean=True),
        )
    )
    branch = _job(failures=("tests/test_a.py::test_one",))

    comparison = baseline.compare(branch, stale)

    assert comparison.verdict == baseline.COMPARISON_UNAVAILABLE
    assert comparison.blocks is True
    assert "no trustworthy result" in comparison.reason


def test_no_baseline_at_all_leaves_the_failure_this_branch_s_problem():
    comparison = baseline.compare(_job(failures=("tests/test_a.py::test_one",)), None)

    assert comparison.verdict == baseline.COMPARISON_UNAVAILABLE
    assert comparison.blocks is True


def test_an_incomplete_branch_failure_list_never_compares():
    red_base = _base_entry(
        _job(
            failures=("tests/test_a.py::test_one",),
            workspace=_workspace(head="basesha", base_head="basesha", clean=True),
        )
    )
    branch = _job(failures=("tests/test_a.py::test_one",), complete=False)

    comparison = baseline.compare(branch, red_base)

    assert comparison.verdict == baseline.COMPARISON_UNAVAILABLE
    assert "in full" in comparison.reason


def test_a_checkout_with_no_resolvable_base_commit_never_compares():
    branch = _job(failures=("tests/test_a.py::test_one",), workspace=_workspace(base_head=""))

    comparison = baseline.compare(branch, None)

    assert comparison.verdict == baseline.COMPARISON_UNAVAILABLE
    assert "base commit" in comparison.reason


def test_a_failure_that_names_no_test_is_not_waved_through_as_pre_existing():
    # A collection error or a crashed runner exits non-zero with an empty summary. Comparing
    # an empty set against a red base would otherwise make every failure "pre-existing".
    red_base = _base_entry(
        _job(
            failures=("tests/test_a.py::test_one",),
            workspace=_workspace(head="basesha", base_head="basesha", clean=True),
        )
    )
    branch = _job(failures=())

    comparison = baseline.compare(branch, red_base)

    assert comparison.verdict == baseline.COMPARISON_UNAVAILABLE
    assert comparison.blocks is True


# ---- what the card and the next worker are told ----------------------------


def test_each_verdict_is_named_in_words_the_brief_can_carry():
    pre_existing = baseline.Comparison(
        baseline.PRE_EXISTING,
        pre_existing=("tests/test_a.py::test_one",),
        base_head="basesha0000000",
        base_job_id="job-base",
    )
    regression = baseline.Comparison(
        baseline.BRANCH_REGRESSION,
        branch_only=("tests/test_c.py::test_new",),
        pre_existing=("tests/test_a.py::test_one",),
        base_head="basesha0000000",
    )
    unavailable = baseline.Comparison(
        baseline.COMPARISON_UNAVAILABLE, reason="no trustworthy result for base abc"
    )

    assert "pre-existing base failure" in baseline.brief(pre_existing)
    assert "tests/test_a.py::test_one" in baseline.brief(pre_existing)
    assert "job-base" in baseline.brief(pre_existing)
    assert "branch regression" in baseline.brief(regression)
    assert "tests/test_c.py::test_new" in baseline.brief(regression)
    assert "do not chase these" in baseline.brief(regression)
    assert "comparison unavailable" in baseline.brief(unavailable)
    assert "no trustworthy result for base abc" in baseline.brief(unavailable)


def test_a_comparison_survives_the_round_trip_through_the_card_record():
    comparison = baseline.Comparison(
        baseline.BRANCH_REGRESSION,
        branch_only=("tests/test_c.py::test_new",),
        base_head="basesha",
    )

    restored = baseline.Comparison.from_record(comparison.as_record())

    assert restored == comparison
    assert baseline.Comparison.from_record({}) is None
    assert baseline.Comparison.from_record("not a record") is None
