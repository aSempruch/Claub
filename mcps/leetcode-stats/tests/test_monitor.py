"""Tests for the session monitor's pure logic."""

from monitor import diff_counts, ext_for_language, parse_args, poll_interval


# ---- argv parsing (the monitor's only source of identity) ----

def _base_argv():
    return [
        "--session=2026-07-28-1905-two-sum", "--problem=two-sum",
        "--session-dir=/w/leetcode-sessions/2026-07-28-1905-two-sum",
        "--question-id=146", "--lang-id=11", "--language=python3", "--lock-fd=3",
    ]


def test_parse_args_extracts_identity_and_baselines():
    args = parse_args(_base_argv() + [
        "--baseline-synced-ts=900", "--baseline-submission-ts=100",
    ])

    assert args.problem == "two-sum"
    assert args.session_dir == "/w/leetcode-sessions/2026-07-28-1905-two-sum"
    assert args.question_id == 146
    assert args.lang_id == 11
    assert args.language == "python3"
    assert args.baseline_synced_ts == 900
    assert args.baseline_submission_ts == 100


def test_parse_args_baselines_default_to_none():
    """A fresh problem has neither prior code nor prior submissions."""
    args = parse_args(_base_argv())

    assert args.baseline_synced_ts is None
    assert args.baseline_submission_ts is None


# ---- snapshot extensions follow the solve's language ----

def test_ext_for_language_maps_known_languages():
    assert ext_for_language("python3") == ".py"
    assert ext_for_language("java") == ".java"
    assert ext_for_language("cpp") == ".cpp"
    assert ext_for_language("typescript") == ".ts"


def test_ext_for_language_falls_back_for_unknown():
    assert ext_for_language("brainfuck") == ".txt"


# ---- poll_interval: tiered purely on time since last observed change ----

def test_poll_interval_is_fast_while_actively_changing():
    assert poll_interval(0) == 5
    assert poll_interval(59) == 5


def test_poll_interval_steps_down_after_a_minute_without_change():
    assert poll_interval(60) == 15
    assert poll_interval(299) == 15


def test_poll_interval_floors_at_thirty_seconds():
    assert poll_interval(300) == 30
    assert poll_interval(10_000) == 30


# ---- diff_counts ----

def test_diff_counts_pure_addition():
    assert diff_counts("a\nb\n", "a\nb\nc\n") == (1, 0)


def test_diff_counts_pure_deletion():
    assert diff_counts("a\nb\nc\n", "a\nc\n") == (0, 1)


def test_diff_counts_modified_line_counts_as_both():
    # A changed line is one removal plus one addition.
    assert diff_counts("a\nb\n", "a\nB\n") == (1, 1)


def test_diff_counts_from_empty_baseline():
    assert diff_counts("", "a\nb\nc\n") == (3, 0)


def test_diff_counts_identical_is_zero():
    assert diff_counts("a\nb\n", "a\nb\n") == (0, 0)
