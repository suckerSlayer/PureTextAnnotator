import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acceptance.run_acceptance import (
    CASE_COUNTS,
    evaluate_rematch_cases,
    generate_rematch_cases,
)


def test_formal_rematch_corpus_meets_acceptance_target() -> None:
    cases = generate_rematch_cases()

    assert len(cases) == 120
    assert sum(CASE_COUNTS.values()) == 120
    assert {case.category for case in cases} == set(CASE_COUNTS)

    results = evaluate_rematch_cases(cases)
    unique = results["unique_unchanged_quote"]

    assert unique["case_count"] >= 50
    assert unique["automatic_correct"] == unique["case_count"]
    assert unique["correct_rate_percent"] >= 95.0
    assert results["combined_wrong_case_ids"] == []
    assert results["strategy_outcomes"]["combined_anchor"] == {
        "automatic_correct": 80,
        "invalid_recognized": 10,
        "pending_confirmation": 30,
    }
