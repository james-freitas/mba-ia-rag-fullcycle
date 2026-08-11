"""Running the real pipeline over the dataset, for whoever is going to judge it.

Shared by the evaluation scripts that grade the final answer — Ragas today, the
custom judge next to it. Both need the same three things: the answerable cases, the
answer the pipeline really produced, and the chunk texts it was written from. What
each of them does with that is where they stop being the same.

The contexts are the reason this lives apart from the pipeline: they are chunk
contents, and the pipeline hands them out only through a callback (see
RagPipeline.run_for_evaluation). They never reach the API, the debug payload, the
spans or the run log.
"""

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.config import PROJECT_ROOT, settings
from app.eval_dataset import EvalCase
from app.governance import check_policy, load_policy
from app.query_planner import SAFE_FILTERS
from app.rag_pipeline import FEATURE_NAME, RagPipeline, RagPipelineResult

REPORTS_DIR = PROJECT_ROOT / "data" / "eval_runs"


class EvalRunError(Exception):
    pass


@dataclass
class CaseRun:
    case: EvalCase
    result: RagPipelineResult | None = None
    contexts: list[str] = field(default_factory=list)
    error: str | None = None
    # Whatever the evaluator produced for this case, under the names it will keep:
    # the Ragas metrics here, the judge criteria there. Same shape either way, so a
    # future step can push them to Langfuse as scores without reshaping anything.
    scores: dict[str, float | None] = field(default_factory=dict)

    @property
    def gradable(self) -> bool:
        """An answerable case that really produced an answer — the rest cannot be graded."""
        return self.result is not None and self.result.has_answer

    @property
    def sources(self) -> list[str]:
        if not self.result:
            return []
        return [source.source_file for source in self.result.sources if source.source_file]

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
            "sources": self.sources,
            "accepted_source_files": self.case.accepted_source_files,
            "tags": self.case.tags,
            # The count, never the contexts: chunk contents do not leave this process.
            "contexts_count": len(self.contexts),
            "scores": self.scores,
        }


def answerable(cases: list[EvalCase]) -> list[EvalCase]:
    # A refusal has no answer to grade and a clarification has no context to grade it
    # against. Both are measured by the deterministic evaluators instead.
    return [case for case in cases if case.should_answer]


def select_cases(cases: list[EvalCase], case_id: str | None, limit: int | None) -> list[EvalCase]:
    if case_id:
        cases = [case for case in cases if case.id == case_id]
        if not cases:
            raise EvalRunError(f"case not found among the answerable cases: {case_id}")

    return cases[:limit] if limit else cases


def ensure_within_policy(*models: str) -> None:
    """Fail on the budget and the allowlist before spending a single case on them.

    The pipeline checks this per run and answers with a refusal when it fails, so a
    blocked budget would otherwise show up as every case coming back empty — which
    reads exactly like a retrieval regression. Evaluation scripts that reach for a
    second model (a judge, say) pass it here too: an allowlist the judge can walk
    around is not an allowlist.
    """
    policy = load_policy(SAFE_FILTERS["tenant"], FEATURE_NAME)
    for model in dict.fromkeys(models):
        decision = check_policy(policy, model)
        if not decision.allowed:
            raise EvalRunError(
                f"the pipeline would refuse every case with {model}: {decision.reason} "
                f"(budget {decision.monthly_budget_usd} USD, "
                f"spent {decision.current_month_spend_usd} USD)"
            )


def average(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def run_pipeline(cases: list[EvalCase], use_rerank: bool) -> list[CaseRun]:
    # Sequential on purpose, and not only for readable progress: RagPipeline reads the
    # usage ledger to check the budget and appends to it afterwards. Concurrent runs
    # would all read the same pre-append spend, and the budget would stop holding.
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


def save_report(name: str, report: dict) -> str:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS_DIR / f"{name}_{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path.relative_to(PROJECT_ROOT))


def label(name: str) -> str:
    return name.replace("_", " ").capitalize()
