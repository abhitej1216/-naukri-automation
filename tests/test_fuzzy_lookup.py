"""Tests for the question→answer matcher in autoapply.py."""

import sqlite3

import pytest

import autoapply


@pytest.fixture(autouse=True)
def _clear_cache():
    autoapply._ANSWERS_CACHE.clear()
    yield
    autoapply._ANSWERS_CACHE.clear()


@pytest.fixture
def answers():
    return {
        "What is your current CTC in Lacs per annum?": "625000",
        "What is your expected CTC in Lacs per annum?": "1000000",
        "How many years of experience do you have in IICS ?": "2-3 years",
        "Are your comfortable from 5 Days from Onsite?": "Yes",
        "Are you comfortable in 24*7 Shift timings?": "Yes",
        "Email ID": "me@example.com",
        "Total experience?": "2.3 years",
        "Are you serving your notice period? If yes, when is your Last Working Day?": (
            "no"
        ),
    }


def test_normalization_strips_numbering_and_punctuation():
    assert (
        autoapply._normalize_question("1. Do you have Python?") == "do you have python"
    )
    assert autoapply._normalize_question("Q2) Email-ID!") == "email id"


def test_ratio_notation_is_normalized():
    assert autoapply._normalize_question("24*7 shift") == "24x7 shift"
    assert autoapply._normalize_question("24/7 shift") == "24x7 shift"


def test_exact_match_ignores_case_and_punctuation(answers):
    assert autoapply.fuzzy_lookup("email id", answers) == "me@example.com"
    assert autoapply.fuzzy_lookup("Email ID?", answers) == "me@example.com"


def test_matches_stored_question_with_extra_detail(answers):
    assert autoapply.fuzzy_lookup("What is your current CTC?", answers) == "625000"


def test_distinguishes_current_and_expected_ctc(answers):
    assert autoapply.fuzzy_lookup("What is your expected CTC?", answers) == "1000000"
    assert autoapply.fuzzy_lookup("What is your current CTC?", answers) == "625000"


def test_rejects_different_technology(answers):
    assert (
        autoapply.fuzzy_lookup(
            "How many years of experience do you have in Python?", answers
        )
        is None
    )


def test_rejects_different_location(answers):
    assert (
        autoapply.fuzzy_lookup("Are you comfortable working from Bangalore?", answers)
        is None
    )


def test_rejects_generic_notice_period_question(answers):
    assert autoapply.fuzzy_lookup("What is your notice period?", answers) is None


def test_matches_paraphrase_with_extra_words(answers):
    assert (
        autoapply.fuzzy_lookup("What is your total work experience?", answers)
        == "2.3 years"
    )


def test_matches_ratio_notation_variant(answers):
    assert (
        autoapply.fuzzy_lookup("Are you comfortable with 24x7 shift timings?", answers)
        == "Yes"
    )


def test_save_answer_is_visible_immediately(tmp_path, monkeypatch):
    monkeypatch.setattr(autoapply, "ANSWERS_DB", str(tmp_path / "answers.db"))
    known = autoapply.load_answers()

    assert autoapply.fuzzy_lookup("What is your current CTC?", known) is None

    autoapply.save_answer("What is your current CTC in Lacs per annum?", "625000")

    # The dict handed out earlier must see the new answer without a reload.
    assert autoapply.fuzzy_lookup("What is your current CTC?", known) == "625000"


def test_save_answer_counts_first_use(tmp_path, monkeypatch):
    db_path = str(tmp_path / "answers.db")
    monkeypatch.setattr(autoapply, "ANSWERS_DB", db_path)

    autoapply.save_answer("Question?", "Answer")
    autoapply.save_answer("Question?", "Answer")

    conn = sqlite3.connect(db_path)
    try:
        usage_count = conn.execute("SELECT usage_count FROM answers").fetchone()[0]
    finally:
        conn.close()
    assert usage_count == 2
