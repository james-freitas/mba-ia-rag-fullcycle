"""Ragas evaluation: the final answer, judged against the context it was written from.

The component evaluation stops before the answer and asks whether the pipeline reached
the right documents. This one starts where that one stops: the answer exists, and the
question is whether it says what the context supports.

Only answerable cases run here. A refusal has no answer to be faithful to and an
ambiguous question has no context to ground, so those two families are measured by the
deterministic evaluators instead. Nothing in this file reinvents a RAG metric: the
scoring is Ragas, and the questions stay in Portuguese.
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
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

from app.config import PROJECT_ROOT, settings
from app.eval_dataset import DatasetError, EvalCase, load_cases
from app.governance import check_policy, load_policy
from app.query_planner import SAFE_FILTERS
from app.rag_pipeline import FEATURE_NAME, RagPipeline, RagPipelineResult

# Ragas posts a usage event to its own endpoint after every model call — synchronously,
# with no try/except, from inside the async metric code. It stalls the event loop the
# batch scoring runs on, and a hiccup there would fail the whole batch.
os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")

REPORTS_DIR = PROJECT_ROOT / "data" / "eval_runs"

FAITHFULNESS = "faithfulness"
RESPONSE_RELEVANCY = "response_relevancy"
CONTEXT_PRECISION = "context_precision"

# Every metric here works from question, answer and contexts alone. Context recall is
# left out on purpose: it needs a reference answer, and the dataset has required_terms
# — facts the answer must contain, not a reference answer. Stretching them into one
# would be inventing the ground truth this whole step is supposed to check against.
METRIC_NAMES = [FAITHFULNESS, RESPONSE_RELEVANCY, CONTEXT_PRECISION]


class RagasEvalError(Exception):
    pass


@dataclass
class CaseRun:
    case: EvalCase
    result: RagPipelineResult | None = None
    contexts: list[str] = field(default_factory=list)
    error: str | None = None
    scores: dict[str, float | None] = field(default_factory=dict)

    @property
    def scorable(self) -> bool:
        return self.result is not None and self.result.has_answer

    def as_report(self) -> dict:
        result = self.result
        return {
            "case_id": self.case.id,
            "question": self.case.question,
            "answer": result.answer if result else None,
            "has_answer": result.has_answer if result else False,
            # Kept apart from has_answer: an answerable case that asked a question back
            # is a planner failure, and a refusal is a grounding one.
            "needs_clarification": result.needs_clarification if result else False,
            "error": self.error,
            "sources": [
                source.source_file for source in result.sources if source.source_file
            ] if result else [],
            "accepted_source_files": self.case.accepted_source_files,
            "tags": self.case.tags,
            # The count, never the contexts: chunk contents do not leave this process.
            "contexts_count": len(self.contexts),
            "scores": self.scores,
        }


def select_cases(cases: list[EvalCase], case_id: str | None, limit: int | None) -> list[EvalCase]:
    if case_id:
        cases = [case for case in cases if case.id == case_id]
        if not cases:
            raise RagasEvalError(f"case not found among the answerable cases: {case_id}")

    return cases[:limit] if limit else cases


def ensure_within_policy() -> None:
    # The pipeline checks this per run and answers with a refusal when it fails. Read
    # once up front so a blocked budget reads as a budget problem instead of 29 cases
    # mysteriously coming back empty.
    decision = check_policy(
        load_policy(SAFE_FILTERS["tenant"], FEATURE_NAME), settings.openai_chat_model
    )
    if not decision.allowed:
        raise RagasEvalError(
            f"the pipeline would refuse every case: {decision.reason} "
            f"(budget {decision.monthly_budget_usd} USD, "
            f"spent {decision.current_month_spend_usd} USD)"
        )


def build_metrics() -> dict:
    # The same models the pipeline uses. A judge on a different model would be
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


def run_pipeline(cases: list[EvalCase], use_rerank: bool) -> list[CaseRun]:
    pipeline = RagPipeline()
    runs = []
    for case in cases:
        print(f"  {case.id}", flush=True)
        try:
            evaluation = pipeline.run_for_evaluation(case.question, use_rerank=use_rerank)
        except Exception as exc:
            # A timeout on the twenty-fifth case must not throw away the twenty-four
            # runs already paid for.
            print(f"    pipeline failed: {exc}", file=sys.stderr)
            runs.append(CaseRun(case=case, error=str(exc)))
            continue

        runs.append(
            CaseRun(
                case=case,
                result=evaluation.result,
                contexts=evaluation.selected_contexts,
            )
        )
    return runs


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
        scored = [run.scores[name] for run in runs if run.scores.get(name) is not None]
        summary[name] = round(sum(scored) / len(scored), 4) if scored else None
    return summary


def build_report(runs: list[CaseRun], metric_errors: dict[str, str], total_cases: int,
                 answerable_count: int, use_rerank: bool) -> dict:
    scored = [run for run in runs if run.scorable]
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
            run.case.id for run in runs if not run.scorable and not run.error
        ],
        "pipeline_errors": [
            {"case_id": run.case.id, "error": run.error} for run in runs if run.error
        ],
        "metric_errors": metric_errors,
        "metrics_summary": summarize(scored),
        "per_case_results": [run.as_report() for run in runs],
    }


def save_report(report: dict) -> str:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS_DIR / f"ragas_{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path.relative_to(PROJECT_ROOT))


def label(name: str) -> str:
    return name.replace("_", " ").capitalize()


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
        else:
            score = f"failed: {report['metric_errors'][name]}" if name in report["metric_errors"] else "no cases scored"
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
        answerable = [case for case in cases if case.should_answer]
        selected = select_cases(answerable, args.case_id, args.limit)
        if not selected:
            raise RagasEvalError("no answerable case selected")

        ensure_within_policy()
        # Built before the expensive loop: a bad key here should not cost 29 runs.
        metrics = build_metrics()

        print(f"Cases to run: {len(selected)} of {len(answerable)} answerable")
        print(f"Reranking: {'on' if use_rerank else 'off'}")

        print("Running the pipeline...")
        runs = run_pipeline(selected, use_rerank)

        # An answerable case that came back without an answer has nothing for Ragas to
        # score: there is no answer to be faithful to. It is a failure, and it belongs
        # in the report as one instead of dragging an average down as a zero.
        scorable = [run for run in runs if run.scorable]
        metric_errors = {}
        if scorable:
            print("Scoring with Ragas...")
            metric_errors = score_runs(scorable, metrics)

        report = build_report(runs, metric_errors, len(cases), len(answerable), use_rerank)
        path = save_report(report)

        print_report(report)
        print()
        print(f"Report: {path}")
    except (RagasEvalError, DatasetError) as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
