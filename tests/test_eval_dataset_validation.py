"""The evaluation dataset has to stay valid on its own, without running anything.

A broken case would only surface much later, as a failed experiment blamed on the
pipeline. Here it surfaces as a failed test.
"""

from app.eval_dataset import load_cases, validate_dataset

CASES = load_cases()


def test_dataset_has_no_errors() -> None:
    assert validate_dataset(CASES) == []


def test_dataset_covers_answerable_cases() -> None:
    assert [case for case in CASES if case.should_answer]


def test_dataset_covers_refusal_cases() -> None:
    assert [case for case in CASES if not case.should_answer and not case.should_clarify]


def test_dataset_covers_clarification_cases() -> None:
    assert [case for case in CASES if case.should_clarify]
