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
        "What is your current CTC in Lacs per annum?": "650000",
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
    assert autoapply.fuzzy_lookup("What is your current CTC?", answers) == "650000"


def test_distinguishes_current_and_expected_ctc(answers):
    assert autoapply.fuzzy_lookup("What is your expected CTC?", answers) == "1000000"
    assert autoapply.fuzzy_lookup("What is your current CTC?", answers) == "650000"


def test_skill_specific_years_of_experience_share_answer():
    stored = {"How many years of experience do you have in Python?": "2.5 years"}
    assert (
        autoapply.fuzzy_lookup(
            "How many years of experience do you have in SQL?", stored
        )
        == "2.5 years"
    )
    assert (
        autoapply.fuzzy_lookup(
            "How many years of experience do you have in Blockchain?", stored
        )
        == "2.5 years"
    )


def test_tab_configuration_uses_current_naukri_tabs(monkeypatch):
    assert [tab["label"] for tab in autoapply.TABS] == [
        "Applies",
        "Handpicked for you",
        "Profile",
        "Preferences",
        "You might like",
    ]
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert autoapply.select_tabs() == [autoapply.TABS[0]]


def test_current_ctc_default_overrides_stale_saved_answer():
    stored = {"What is your current CTC in Lacs per annum?": "500000"}
    assert autoapply.answer_for_question("What is your current CTC?", stored) == "650000"
    assert autoapply.profile_fallback_answer("What is your expected CTC?") is None


def test_profile_defaults_override_stale_experience_and_location_answers():
    stored = {
        "How many years of experience in Python?": "1 year",
        "What is your current location?": "Delhi",
    }
    assert (
        autoapply.answer_for_question(
            "How many years of experience in Blockchain?", stored
        )
        == "2.5 years"
    )
    assert autoapply.answer_for_question("What is your current city?", stored) == "Noida"


def test_date_of_birth_fallback_matches_common_phrasings(monkeypatch):
    monkeypatch.setattr(autoapply, "DEFAULT_DATE_OF_BIRTH", "28121999")
    assert autoapply.profile_fallback_answer("What is your DOB?") == "28121999"
    assert autoapply.profile_fallback_answer("Date of birth") == "28121999"
    assert autoapply.profile_fallback_answer("Please enter your birth date") == "28121999"
    assert autoapply.profile_fallback_answer("What is your joining date?") is None


def test_profile_defaults_cover_experience_and_location_questions():
    assert autoapply.profile_fallback_answer("Years of experience in Blockchain?") == "2.5 years"
    assert autoapply.profile_fallback_answer("Total Exp:") == "2.5 years"
    assert autoapply.profile_fallback_answer("What is your current city?") == "Noida"
    assert autoapply.profile_fallback_answer("What is your preferred location?") == "Noida"
    assert (
        autoapply.profile_fallback_answer(
            "Are you comfortable for Pune location (Kharadi)?"
        )
        == "Yes"
    )
    assert autoapply.profile_fallback_answer("What is your notice period?") is None


def test_total_exp_abbreviation_matches_total_experience_answer():
    stored = {"Total experience?": "2.5 years"}
    assert autoapply.fuzzy_lookup("Total Exp:", stored) == "2.5 years"
    assert autoapply.fuzzy_lookup("What is your total experience?", stored) == "2.5 years"


def test_skill_experience_match_does_not_match_total_experience():
    stored = {"How many years of experience do you have in Python?": "2.5 years"}
    assert autoapply.fuzzy_lookup("What is your total experience?", stored) is None


def test_conflicting_skill_experience_answers_are_not_guessed():
    stored = {
        "How many years of experience do you have in Python?": "2.5 years",
        "How many years of experience do you have in SQL?": "4 years",
    }
    assert autoapply.fuzzy_lookup("How many years of experience in Java?", stored) is None


def test_experience_match_generalizes_across_technologies(answers):
    assert (
        autoapply.fuzzy_lookup(
            "How many years of experience do you have in Python?", answers
        )
        == "2-3 years"
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


def test_radio_option_match_requires_clear_match():
    assert autoapply._match_option("Yes", ["Yes", "No"]) == 0
    assert autoapply._match_option("Yes", ["Yes, with relocation", "Yes, remote only"]) is None
    assert autoapply._match_option("Unrelated", ["Yes", "No"]) is None


def test_save_answer_is_visible_immediately(tmp_path, monkeypatch):
    monkeypatch.setattr(autoapply, "ANSWERS_DB", str(tmp_path / "answers.db"))
    known = autoapply.load_answers()

    assert autoapply.fuzzy_lookup("What is your current CTC?", known) is None

    autoapply.save_answer("What is your current CTC in Lacs per annum?", "650000")

    # The dict handed out earlier must see the new answer without a reload.
    assert autoapply.fuzzy_lookup("What is your current CTC?", known) == "650000"


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
