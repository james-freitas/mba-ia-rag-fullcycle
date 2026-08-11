"""Ragas evaluation: the final answer, judged against the context it was written from.

The component evaluation stops before the answer and asks whether the pipeline reached
the right documents. This one starts where that one stops: the answer exists, and the
question is whether it says what the context supports.

Only answerable cases run here — see app/eval_runner.py, which does the running. What
this file adds is the scoring, and it adds nothing of its own: faithfulness and response
relevancy need a model reading the answer, so the metrics are Ragas and none of them is
reimplemented here. The questions stay in Portuguese.
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from uuid import uuid4

from openai import AsyncOpenAI
from ragas.embeddings.base import embedding_factory
from ragas.llms import llm_factory
from ragas.metrics.collections import (
    AnswerRelevancy,
    ContextPrecisionWithoutReference,
    Faithfulness,
)

from app.config import settings
from app.eval_dataset import DatasetError, load_cases
from app.eval_runner import (
    CaseRun,
    EvalRunError,
    answerable,
    average,
    ensure_within_policy,
    label,
    run_pipeline,
    save_report,
    select_cases,
)

# Ragas posts a usage event to its own endpoint after every model call — synchronously,
# with no try/except, from inside the async metric code. It stalls the event loop the
# batch scoring runs on, and a hiccup there would fail the whole batch.
os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")

FAITHFULNESS = "faithfulness"
RESPONSE_RELEVANCY = "response_relevancy"
CONTEXT_PRECISION = "context_precision"

# Every metric here works from question, answer and contexts alone. Context recall is
# left out on purpose: it needs a reference answer, and the dataset has required_terms
# — facts the answer must contain, not a reference answer. Stretching them into one
# would be inventing the ground truth this whole step is supposed to check against.
METRIC_NAMES = [FAITHFULNESS, RESPONSE_RELEVANCY, CONTEXT_PRECISION]


def build_metrics() -> dict:
    # The same models the pipeline uses. Scoring on a different model would be
    # measuring two changes at once.
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    llm = llm_factory(settings.openai_chat_model, client=client)
    embeddings = embedding_factory(
        provider="openai", model=settings.openai_embedding_model, client=client
    )
    return {
        FAITHFULNESS: Faithfulness(llm=llm),
        RESPONSE_RELEVANCY: AnswerRelevancy(llm=llm, embeddings=embeddings),
        CONTEXT_PRECISION: ContextPrecisionWithoutReference(llm=llm),
    }


def metric_inputs(name: str, runs: list[CaseRun]) -> list[dict]:
    # Response relevancy asks whether the answer addresses the question, so it never
    # looks at the context.
    if name == RESPONSE_RELEVANCY:
        return [{"user_input": run.case.question, "response": run.result.answer} for run in runs]

    return [
        {
            "user_input": run.case.question,
            "response": run.result.answer,
            "retrieved_contexts": run.contexts,
        }
        for run in runs
    ]


def score_runs(runs: list[CaseRun], metrics: dict) -> dict[str, str]:
    errors = {}
    for name, metric in metrics.items():
        print(f"  {name}", flush=True)
        try:
            values = [result.value for result in metric.batch_score(metric_inputs(name, runs))]
        except Exception as exc:
            # One failed case aborts the whole batch, and the pipeline runs behind it
            # were the expensive part. Losing a metric beats losing the run.
            print(f"    metric failed: {exc}", file=sys.stderr)
            errors[name] = str(exc)
            values = [None] * len(runs)

        for run, value in zip(runs, values):
            run.scores[name] = value

    return errors


def summarize(runs: list[CaseRun]) -> dict[str, float | None]:
    summary = {}
    for name in METRIC_NAMES:
        summary[name] = average(
            [run.scores[name] for run in runs if run.scores.get(name) is not None]
        )
    return summary


def build_report(runs: list[CaseRun], metric_errors: dict[str, str], total_cases: int,
                 answerable_count: int, use_rerank: bool) -> dict:
    scored = [run for run in runs if run.gradable]
    return {
        "run_id": str(uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": settings.openai_chat_model,
        "use_rerank": use_rerank,
        "total_cases": total_cases,
        "answerable_cases": answerable_count,
        "ragas_cases": len(scored),
        # Only the cases the dataset never meant for Ragas: refusals and clarifications.
        "skipped_cases": total_cases - answerable_count,
        "failed_before_ragas": [
            run.case.id for run in runs if not run.gradable and not run.error
        ],
        "pipeline_errors": [
            {"case_id": run.case.id, "error": run.error} for run in runs if run.error
        ],
        "metric_errors": metric_errors,
        "metrics_summary": summarize(scored),
        "per_case_results": [run.as_report() for run in runs],
    }


def lowest_scoring(report: dict, limit: int = 5) -> list[tuple[str, str, float]]:
    # Filtered on the rounded value, the same one printed above: Ragas returns things
    # like 0.9999996, which is below 1.0 and still shows up as a perfect 1.00.
    entries = [
        (entry["case_id"], name, value)
        for entry in report["per_case_results"]
        for name, value in entry["scores"].items()
        if value is not None and round(value, 2) < 1.0
    ]
    return sorted(entries, key=lambda entry: entry[2])[:limit]


def print_report(report: dict) -> None:
    print()
    print("Ragas Evaluation Summary")
    print()
    print(f"Dataset cases: {report['total_cases']}")
    print(f"Answerable cases: {report['answerable_cases']}")
    print(f"Evaluated by Ragas: {report['ragas_cases']}")
    print(f"Failed before Ragas: {len(report['failed_before_ragas'])}")
    print(f"Skipped: {report['skipped_cases']}")
    print()

    summary = report["metrics_summary"]
    width = max(len(label(name)) for name in summary) + 1
    for name, value in summary.items():
        if value is not None:
            score = f"{value:.2f}"
        elif name in report["metric_errors"]:
            score = f"failed: {report['metric_errors'][name]}"
        else:
            score = "no cases scored"
        print(f"{label(name) + ':':<{width}} {score}")

    for title, entries in [
        ("Failed before Ragas:", report["failed_before_ragas"]),
        ("Pipeline errors:", [f"{e['case_id']}: {e['error']}" for e in report["pipeline_errors"]]),
    ]:
        if entries:
            print()
            print(title)
            for entry in entries:
                print(f"- {entry}")

    # Zooming in on one case is the whole point of --case-id: a score is not actionable
    # until you read the answer that earned it.
    scored = [entry for entry in report["per_case_results"] if entry["scores"]]
    if len(scored) == 1:
        entry = scored[0]
        print()
        print(f"Question: {entry['question']}")
        print(f"Answer:   {entry['answer']}")
        print(f"Sources:  {', '.join(entry['sources']) or 'none'}")
        return

    lowest = lowest_scoring(report)
    if lowest:
        print()
        print("Lowest scoring cases:")
        for case_id, name, value in lowest:
            print(f"- {case_id}: {name}={value:.2f}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ragas evaluation of the final answer, on the answerable cases."
    )
    parser.add_argument("--limit", type=int, help="run only the first N answerable cases")
    parser.add_argument("--case-id", help="run only this case")
    parser.add_argument(
        "--no-rerank", action="store_true", help="run the pipeline without reranking"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    use_rerank = not args.no_rerank

    try:
        cases = load_cases()
        candidates = answerable(cases)
        selected = select_cases(candidates, args.case_id, args.limit)
        if not selected:
            raise EvalRunError("no answerable case selected")

        ensure_within_policy(settings.openai_chat_model)
        # Built before the expensive loop: a bad key here should not cost 29 runs.
        metrics = build_metrics()

        print(f"Cases to run: {len(selected)} of {len(candidates)} answerable")
        print(f"Reranking: {'on' if use_rerank else 'off'}")

        print("Running the pipeline...")
        runs = run_pipeline(selected, use_rerank)

        # An answerable case that came back without an answer has nothing for Ragas to
        # score: there is no answer to be faithful to. It is a failure, and it belongs
        # in the report as one instead of dragging an average down as a zero.
        scorable = [run for run in runs if run.gradable]
        metric_errors = {}
        if scorable:
            print("Scoring with Ragas...")
            metric_errors = score_runs(scorable, metrics)

        report = build_report(runs, metric_errors, len(cases), len(candidates), use_rerank)
        path = save_report("ragas", report)

        print_report(report)
        print()
        print(f"Report: {path}")
    except (EvalRunError, DatasetError) as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
