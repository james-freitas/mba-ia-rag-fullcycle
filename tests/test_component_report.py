"""The report is where a "not applicable" could quietly turn into a failure.

No model, no database, no Langfuse: the evaluation objects are built by hand and what
matters is what print_report writes. The guards in the evaluators are checked the same
way, with plain dicts standing in for a dataset item.
"""

from langfuse import Evaluation

from app.eval_components import (
    CaseResult,
    clarification_skips_retrieval,
    planner_clarification_match,
    print_report,
    rerank_source_kept,
    retrieval_source_hit,
    select_evaluators,
    stage_output,
)

ANSWERABLE = {
    "should_answer": True,
    "should_clarify": False,
    "accepted_source_files": ["company-info.md"],
    "required_terms": ["suporte@fcai.example.com"],
    "expected_doc_types": ["company"],
    "expected_plan": None,
    "tags": ["rag"],
}
CLARIFICATION = {**ANSWERABLE, "should_answer": False, "should_clarify": True,
                 "accepted_source_files": [], "required_terms": []}
REFUSAL = {**CLARIFICATION, "should_clarify": False}

SKIPPED = {
    "retrieval": stage_output([], "retrieved", ran=False),
    "reranking": stage_output([], "selected", ran=False),
}


def case(case_id: str, **scores: float) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        evaluations=[
            Evaluation(name=name, value=value, comment=f"why {name}")
            for name, value in scores.items()
        ],
    )


def line_for(output: str, label: str) -> str:
    for line in output.splitlines():
        if line.startswith(label):
            return " ".join(line.split())
    raise AssertionError(f"line not found: {label}\n{output}")


def test_not_applicable_stays_out_of_the_denominator(capsys) -> None:
    results = [
        case("answerable", planner_clarification_match=1.0, retrieval_source_hit=1.0),
        case("clarification", planner_clarification_match=1.0),
    ]

    print_report([planner_clarification_match, retrieval_source_hit], results, len(results))
    output = capsys.readouterr().out

    assert line_for(output, "Planner clarification match:") == (
        "Planner clarification match: 2/2 (0 n/a)"
    )
    # The clarification case produced no retrieval score: 1 applicable, 1 n/a — never 1/2.
    assert line_for(output, "Retrieval source hit:") == "Retrieval source hit: 1/1 (1 n/a)"


def test_not_applicable_is_never_reported_as_a_failure(capsys) -> None:
    results = [
        case("answerable", retrieval_source_hit=1.0),
        case("clarification", planner_clarification_match=1.0),
    ]

    print_report([retrieval_source_hit], results, len(results))
    output = capsys.readouterr().out

    assert "Failures" in output
    assert "None." in output
    assert "clarification" not in output.split("Failures")[1]


def test_failures_are_grouped_by_metric(capsys) -> None:
    results = [
        case("company_email", retrieval_source_hit=0.0, rerank_source_kept=0.0),
        case("sla_p1", retrieval_source_hit=1.0, rerank_source_kept=1.0),
    ]

    print_report([retrieval_source_hit, rerank_source_kept], results, len(results))
    failures = capsys.readouterr().out.split("Failures")[1]

    assert "retrieval_source_hit:\n- company_email: why retrieval_source_hit" in failures
    assert "rerank_source_kept:\n- company_email: why rerank_source_kept" in failures
    assert "sla_p1" not in failures


def test_report_flags_cases_that_never_ran(capsys) -> None:
    results = [case("answerable", retrieval_source_hit=1.0)]

    print_report([retrieval_source_hit], results, requested=3)
    output = capsys.readouterr().out

    assert "Cases: 1" in output
    assert "Cases that failed to run: 2" in output


def test_retrieval_and_rerank_skip_clarification_and_refusal_cases() -> None:
    for expected in (CLARIFICATION, REFUSAL):
        assert retrieval_source_hit(input={}, output=SKIPPED, expected_output=expected) is None
        assert rerank_source_kept(input={}, output=SKIPPED, expected_output=expected) is None


def test_retrieval_accepts_any_of_the_accepted_sources() -> None:
    output = {
        "retrieval": stage_output([], "retrieved"),
        "reranking": stage_output([], "selected"),
    }
    output["retrieval"]["retrieved_source_files"] = ["billing-policy.md", "company-info.md"]

    evaluation = retrieval_source_hit(input={}, output=output, expected_output=ANSWERABLE)

    assert evaluation.value == 1.0


def test_clarification_skips_retrieval_only_applies_to_ambiguous_cases() -> None:
    assert clarification_skips_retrieval(
        input={}, output=SKIPPED, expected_output=ANSWERABLE
    ) is None

    evaluation = clarification_skips_retrieval(
        input={}, output=SKIPPED, expected_output=CLARIFICATION
    )

    assert evaluation.value == 1.0


def test_no_rerank_drops_the_rerank_metric_instead_of_failing_it() -> None:
    assert rerank_source_kept in select_evaluators(use_rerank=True)
    assert rerank_source_kept not in select_evaluators(use_rerank=False)
