"""A comparison is only worth printing when the two runs are comparable.

No model, no database, no Langfuse: the reports are built by hand and what matters is
what the summary counts and what the comparison refuses to subtract. The traps are the
ones a real run hits — a case that failed and shrank the denominator, and a smoke run
diffed against a full one.
"""

import json

import pytest

from app.eval_experiment import (
    SCORE_NAMES,
    ExperimentError,
    behavior,
    build_report,
    experiment_answer_shape,
    experiment_expected_behavior,
    experiment_required_terms_match,
    load_report,
    print_comparison,
    print_report,
    resolve_report,
)

ANSWERABLE = {
    "should_answer": True,
    "should_clarify": False,
    "accepted_source_files": ["pro-plan.md"],
    "required_terms": ["8 horas úteis"],
    "expected_doc_types": ["plan"],
    "expected_plan": "pro",
    "tags": ["rag"],
}


def output(**overrides) -> dict:
    base = {
        "answer": "8 horas úteis.",
        "has_answer": True,
        "needs_clarification": False,
        "policy_allowed": True,
        "sources": ["pro-plan.md"],
        "total_ms": 1000.0,
        "total_tokens": 100,
        "estimated_cost_usd": 0.001,
    }
    return {**base, **overrides}


def case(case_id: str, **scores: float) -> dict:
    return {
        "case_id": case_id,
        "question": "q?",
        **output(),
        "scores": scores,
        "reasons": {name: "why" for name in scores},
    }


def report(*cases: dict, requested: int | None = None) -> dict:
    return build_report("baseline", list(cases), requested or len(cases), None)


def test_summary_counts_only_the_cases_a_score_applies_to() -> None:
    built = report(
        case("answerable", experiment_answer_shape=1.0, experiment_required_terms_match=0.0),
        # A refusal produces no required-terms score at all.
        case("refusal", experiment_answer_shape=1.0),
    )
    summary = built["summary"]

    assert summary["experiment_answer_shape_passed"] == 2
    assert summary["experiment_answer_shape_applicable"] == 2
    assert summary["experiment_required_terms_match_passed"] == 0
    assert summary["experiment_required_terms_match_applicable"] == 1


def test_report_flags_cases_that_never_came_back(capsys) -> None:
    built = report(case("ran", experiment_answer_shape=1.0), requested=3)

    print_report(built)
    output_text = capsys.readouterr().out

    assert "Cases: 1" in output_text
    assert "Cases that failed to run: 2" in output_text


def test_report_flags_cases_blocked_by_the_policy(capsys) -> None:
    blocked = case("blocked", experiment_answer_shape=1.0)
    blocked["policy_allowed"] = False

    built = report(blocked)
    print_report(built)

    assert built["blocked_by_policy"] == ["blocked"]
    assert "Cases blocked by policy: 1" in capsys.readouterr().out


def test_comparison_refuses_runs_that_cover_different_cases() -> None:
    full = report(*(case(f"c{index}", experiment_answer_shape=1.0) for index in range(3)))
    smoke = report(case("c0", experiment_answer_shape=1.0))

    with pytest.raises(ExperimentError, match="different cases"):
        print_comparison(full, smoke)


def test_comparison_refuses_different_datasets() -> None:
    left = report(case("c0", experiment_answer_shape=1.0))
    right = report(case("c0", experiment_answer_shape=1.0))
    right["dataset_name"] = "another-dataset"

    with pytest.raises(ExperimentError, match="different datasets"):
        print_comparison(left, right)


def test_comparison_prints_the_diff_with_units(capsys) -> None:
    left = report(case("c0", experiment_answer_shape=1.0))
    right = report(case("c0", experiment_answer_shape=0.0))
    right["summary"]["average_total_ms"] = 500.0

    print_comparison(left, right)
    lines = capsys.readouterr().out

    assert "Answer shape" in lines and "-1" in lines
    assert "-500 ms" in lines


def test_answer_shape_catches_what_pydantic_cannot() -> None:
    assert experiment_answer_shape(output=output()).value == 1.0
    # Types are guaranteed upstream; the agreement between fields is not.
    assert experiment_answer_shape(output=output(answer="   ")).value == 0.0
    assert experiment_answer_shape(
        output=output(has_answer=True, needs_clarification=True)
    ).value == 0.0
    assert experiment_answer_shape(output=output(has_answer=False)).value == 0.0
    assert experiment_answer_shape(output=output(sources=[])).value == 0.0


def test_expected_behavior_names_the_three_families() -> None:
    assert behavior(True, False) == "answer"
    assert behavior(False, True) == "clarification"
    assert behavior(False, False) == "refusal"

    refusal = {**ANSWERABLE, "should_answer": False}
    assert experiment_expected_behavior(output=output(), expected_output=ANSWERABLE).value == 1.0
    assert experiment_expected_behavior(output=output(), expected_output=refusal).value == 0.0


def test_required_terms_ignore_case_and_skip_unanswerable_cases() -> None:
    assert experiment_required_terms_match(
        output=output(answer="São 8 HORAS ÚTEIS."), expected_output=ANSWERABLE
    ).value == 1.0
    assert experiment_required_terms_match(
        output=output(answer="Depende."), expected_output=ANSWERABLE
    ).value == 0.0
    assert experiment_required_terms_match(
        output=output(), expected_output={**ANSWERABLE, "should_answer": False}
    ) is None


def test_load_report_reads_what_build_report_wrote(tmp_path) -> None:
    built = report(case("c0", experiment_answer_shape=1.0))
    path = tmp_path / "report.json"
    path.write_text(json.dumps(built, ensure_ascii=False), encoding="utf-8")

    assert load_report(str(path))["summary"] == built["summary"]
    with pytest.raises(ExperimentError, match="no report found"):
        load_report(str(tmp_path / "missing.json"))


def test_a_variant_name_resolves_to_its_newest_report(tmp_path, monkeypatch) -> None:
    import app.eval_experiment as module

    monkeypatch.setattr(module, "REPORTS_DIR", tmp_path)
    for stamp in ("20260101T000000Z", "20260202T000000Z"):
        (tmp_path / f"experiment_baseline_{stamp}.json").write_text("{}", encoding="utf-8")

    # Typing two timestamped paths by hand is how the wrong pair gets compared.
    assert resolve_report("baseline").name == "experiment_baseline_20260202T000000Z.json"
    with pytest.raises(ExperimentError, match="no report found"):
        resolve_report("no-rerank")


def test_score_names_and_labels_stay_in_sync() -> None:
    # The names go to Langfuse and must not drift; the labels are what the table prints.
    assert SCORE_NAMES == [
        "experiment_answer_shape",
        "experiment_expected_behavior",
        "experiment_accepted_source_match",
        "experiment_required_terms_match",
    ]
