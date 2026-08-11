"""Component evaluation: the retrieval half of the pipeline, measured on its own.

The contract tests check the shape the API promises; the dataset holds what each
question should produce. This step runs the real components against those cases —
query planning, filters, retrieval and reranking — and scores each one separately.

The answer model is never called here. What is measured is whether the pipeline
reached the right documents, which is where most RAG failures actually start: an
answer graded as wrong is useless feedback when the chunk it needed never arrived.
"""

import argparse
import os
import sys
from dataclasses import dataclass

from langchain.chat_models import init_chat_model
from langchain_core.documents import Document
from langfuse import Evaluation

from app.config import settings
from app.eval_dataset import (
    DATASET_NAME,
    DatasetError,
    build_item,
    connect_langfuse,
    item_field,
    load_cases,
    select_items,
)
from app.query_planner import (
    QueryPlan,
    build_filters,
    build_planner_prompt,
    build_search_query,
    ensure_planner_covers_index,
    search_chunks,
)
from app.rerank import RerankResult, build_rerank_prompt, select_reranked_documents
from app.retrieve import connect_store, ensure_collection_ready

RUN_FLAG = "RUN_COMPONENT_EVALS"
EXPERIMENT_NAME = "component-evaluation"
EXPERIMENT_DESCRIPTION = (
    "Query planning, retrieval and reranking scored per component. "
    "The answer model is not called."
)


class ComponentEvalError(Exception):
    pass


@dataclass
class CaseResult:
    case_id: str
    evaluations: list[Evaluation]


def source_files(documents: list[Document]) -> list[str]:
    files = []
    for document in documents:
        name = document.metadata.get("source_file")
        if name and name not in files:
            files.append(name)
    return files


def chunk_ids(documents: list[Document]) -> list[str]:
    return [document.metadata.get("chunk_id") for document in documents]


def stage_output(documents: list[Document], prefix: str, ran: bool = True) -> dict:
    return {
        "ran": ran,
        f"{prefix}_count": len(documents),
        f"{prefix}_source_files": source_files(documents),
        f"{prefix}_chunk_ids": chunk_ids(documents),
    }


class ComponentRunner:
    """The pipeline up to the answer, and not one step further."""

    def __init__(self, use_rerank: bool) -> None:
        ensure_collection_ready()
        ensure_planner_covers_index()
        self.store = connect_store()
        self.use_rerank = use_rerank

        # Same model, same temperature and same output cap as the pipeline: a planner
        # answering differently here would make the scores describe another system.
        model = init_chat_model(
            settings.openai_chat_model,
            model_provider="openai",
            api_key=settings.openai_api_key,
            temperature=0,
            max_tokens=settings.ai_max_output_tokens,
        )
        self.planner = model.with_structured_output(QueryPlan)
        self.reranker = model.with_structured_output(RerankResult)

    def run(self, question: str) -> dict:
        query_plan = self.planner.invoke(build_planner_prompt(question))
        filters = build_filters(query_plan)

        output = {
            "query_plan": {
                **query_plan.model_dump(),
                "search_query": build_search_query(query_plan),
                "filters": filters,
            },
            "retrieval": stage_output([], "retrieved", ran=False),
            "reranking": stage_output([], "selected", ran=False),
        }

        # An ambiguous question stops before retrieval, exactly like in production.
        if query_plan.needs_clarification:
            return output

        results = search_chunks(self.store, query_plan, filters)
        output["retrieval"] = stage_output(
            [document for document, _ in results], "retrieved"
        )

        # With --no-rerank there is no selection to report. Production would still
        # trim the top N here, but that is answer preparation, not a component the
        # dataset has an expectation for.
        if not self.use_rerank or not results:
            return output

        rerank_result = self.reranker.invoke(
            build_rerank_prompt(results, question, query_plan)
        )
        output["reranking"] = stage_output(
            select_reranked_documents(results, rerank_result.selected_chunk_ids),
            "selected",
        )
        return output

    def component_target(self, *, item, **kwargs) -> dict:
        print(f"  {item_field(item, 'id')}", flush=True)
        return self.run(item_field(item, "input")["question"])


def scored(name: str, passed: bool, reason: str) -> Evaluation:
    return Evaluation(name=name, value=1.0 if passed else 0.0, comment=reason)


def listed(values: list[str]) -> str:
    return ", ".join(values) or "none"


def path_name(needs_clarification: bool) -> str:
    return "clarification" if needs_clarification else "answered path"


# Each evaluator returns None when the case cannot express the expectation — no
# expected plan, no expected doc types, no answer to retrieve. Langfuse turns a None
# into no score at all, so a case that was never applicable does not inflate the
# metric the way a free 1.0 would.


def planner_clarification_match(*, output, expected_output, **kwargs) -> Evaluation:
    expected = expected_output["should_clarify"]
    actual = output["query_plan"]["needs_clarification"]
    return scored(
        "planner_clarification_match",
        actual == expected,
        f"expected {path_name(expected)}, got {path_name(actual)}",
    )


def planner_plan_match(*, output, expected_output, **kwargs) -> Evaluation | None:
    expected = expected_output["expected_plan"]
    if expected is None:
        return None

    actual = output["query_plan"]["plan"]
    return scored(
        "planner_plan_match",
        actual == expected,
        f"expected plan {expected}, got {actual or 'none'}",
    )


def planner_doc_type_coverage(*, output, expected_output, **kwargs) -> Evaluation | None:
    expected = expected_output["expected_doc_types"]
    if not expected:
        return None

    actual = output["query_plan"]["doc_types"]
    missing = [doc_type for doc_type in expected if doc_type not in actual]
    reason = (
        f"missing {listed(missing)}, planned {listed(actual)}"
        if missing
        else f"covered {listed(expected)}"
    )
    return scored("planner_doc_type_coverage", not missing, reason)


def retrieval_source_hit(*, output, expected_output, **kwargs) -> Evaluation | None:
    if not expected_output["should_answer"]:
        return None

    accepted = expected_output["accepted_source_files"]
    retrieved = output["retrieval"]["retrieved_source_files"]
    return scored(
        "retrieval_source_hit",
        any(name in retrieved for name in accepted),
        f"accepted {listed(accepted)}, retrieved {listed(retrieved)}",
    )


def rerank_source_kept(*, output, expected_output, **kwargs) -> Evaluation | None:
    if not expected_output["should_answer"]:
        return None

    accepted = expected_output["accepted_source_files"]
    selected = output["reranking"]["selected_source_files"]
    return scored(
        "rerank_source_kept",
        any(name in selected for name in accepted),
        f"accepted {listed(accepted)}, selected {listed(selected)}",
    )


def clarification_skips_retrieval(*, output, expected_output, **kwargs) -> Evaluation | None:
    if not expected_output["should_clarify"]:
        return None

    retrieval_ran = output["retrieval"]["ran"]
    reranking_ran = output["reranking"]["ran"]
    skipped = not retrieval_ran and not reranking_ran
    reason = (
        "no retrieval and no reranking"
        if skipped
        else f"retrieval ran={retrieval_ran}, reranking ran={reranking_ran}"
    )
    return scored("clarification_skips_retrieval", skipped, reason)


EVALUATORS = [
    planner_clarification_match,
    planner_plan_match,
    planner_doc_type_coverage,
    retrieval_source_hit,
    rerank_source_kept,
    clarification_skips_retrieval,
]


def select_evaluators(use_rerank: bool) -> list:
    # Without reranking there is no selection to judge, so the score is not produced
    # at all instead of being recorded as a failure.
    return [
        evaluator
        for evaluator in EVALUATORS
        if use_rerank or evaluator is not rerank_source_kept
    ]


def run_locally(items: list[dict], target, evaluators: list) -> list[CaseResult]:
    results = []
    for item in items:
        output = target(item=item)
        evaluations = []
        for evaluator in evaluators:
            evaluation = evaluator(
                input=item["input"],
                output=output,
                expected_output=item["expected_output"],
                metadata=item["metadata"],
            )
            if evaluation is not None:
                evaluations.append(evaluation)
        results.append(CaseResult(case_id=item["id"], evaluations=evaluations))
    return results


def label(name: str) -> str:
    return name.replace("_", " ").capitalize()


def scores_named(results: list[CaseResult], name: str) -> list[tuple[str, Evaluation]]:
    return [
        (result.case_id, evaluation)
        for result in results
        for evaluation in result.evaluations
        if evaluation.name == name
    ]


def print_report(evaluators: list, results: list[CaseResult], requested: int) -> None:
    print()
    print("Component Evaluation Summary")
    print()
    print(f"Dataset: {DATASET_NAME}")
    print(f"Cases: {len(results)}")
    if len(results) < requested:
        print(f"Cases that failed to run: {requested - len(results)}")
    print()

    width = max(len(label(evaluator.__name__)) for evaluator in evaluators) + 1
    for evaluator in evaluators:
        scores = scores_named(results, evaluator.__name__)
        passed = sum(1 for _, evaluation in scores if evaluation.value == 1.0)
        # A case the metric could not express an expectation for: the evaluator
        # returned None, no score was recorded, and it stays out of the denominator.
        not_applicable = len(results) - len(scores)
        counts = f"{passed}/{len(scores)}"
        print(f"{label(evaluator.__name__) + ':':<{width}} {counts:>7}  ({not_applicable} n/a)")

    print()
    print("Failures")
    print()

    clean = True
    for evaluator in evaluators:
        failed = [
            (case_id, evaluation)
            for case_id, evaluation in scores_named(results, evaluator.__name__)
            if evaluation.value == 0.0
        ]
        if not failed:
            continue

        clean = False
        print(f"{evaluator.__name__}:")
        for case_id, evaluation in failed:
            print(f"- {case_id}: {evaluation.comment}")
        print()

    if clean:
        print("None.")


def ensure_enabled() -> None:
    if os.getenv(RUN_FLAG, "").lower() != "true":
        raise ComponentEvalError(
            f"{RUN_FLAG}=true is required because this evaluation calls LLMs."
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Component evaluation for the RAG chat: planner, retrieval, rerank."
    )
    parser.add_argument("--limit", type=int, help="run only the first N cases")
    parser.add_argument("--case-id", help="run only this case")
    parser.add_argument(
        "--no-rerank", action="store_true", help="evaluate retrieval without reranking"
    )
    parser.add_argument(
        "--no-langfuse",
        action="store_true",
        help="run against the local dataset file and do not record scores",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    use_rerank = not args.no_rerank

    try:
        ensure_enabled()
        evaluators = select_evaluators(use_rerank)

        client = None
        if args.no_langfuse:
            print("Running without Langfuse: scores will not be recorded.")
            items = [build_item(case) for case in load_cases()]
        else:
            client = connect_langfuse()
            items = client.get_dataset(DATASET_NAME).items

        items = select_items(items, args.case_id, args.limit)
        print(f"Dataset: {DATASET_NAME}")
        print(f"Cases to run: {len(items)}")
        print(f"Reranking: {'on' if use_rerank else 'off'}")

        runner = ComponentRunner(use_rerank)

        print("Running cases...")
        run_url = None
        if client is None:
            results = run_locally(items, runner.component_target, evaluators)
        else:
            experiment = client.run_experiment(
                name=EXPERIMENT_NAME,
                description=EXPERIMENT_DESCRIPTION,
                data=items,
                task=runner.component_target,
                evaluators=evaluators,
                # The target is synchronous, so the SDK would run the items one at a
                # time anyway. Saying so keeps the progress lines in order.
                max_concurrency=1,
            )
            results = [
                CaseResult(case_id=result.item.id, evaluations=result.evaluations)
                for result in experiment.item_results
            ]
            run_url = experiment.dataset_run_url

        print_report(evaluators, results, len(items))
        if run_url:
            print(f"Langfuse run: {run_url}")
    except (ComponentEvalError, DatasetError) as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
