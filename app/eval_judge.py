"""LLM-as-a-judge: a model grading the answer against a written rubric.

Ragas measures a few named properties with metrics somebody else defined. A judge is
the other move: you write down what a good answer means for *this* product, and a model
applies that definition. It reaches the things a metric cannot name — whether the answer
covers what the question needed, whether it reads clearly — at the price of being a
model's opinion, which is why the rubric is spelled out instead of implied.

Two rules make the opinion checkable. The verdict is structured output validated by
Pydantic, so a judge that rambles fails loudly instead of producing an unusable score.
And the judge grades against the retrieved context only: it is forbidden from using what
it happens to know about SLAs, which is the difference between grading this pipeline and
grading the world.

Only answerable cases are judged. The questions and the reasons stay in Portuguese.
"""

import argparse
import sys
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from langchain.chat_models import init_chat_model
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field, ValidationError, model_validator

from app.config import PROJECT_ROOT, settings
from app.eval_dataset import DatasetError, load_cases
from app.eval_runner import (
    CaseRun,
    EvalRunError,
    answerable,
    average,
    ensure_within_policy,
    run_pipeline,
    save_report,
    select_cases,
)

DEFAULT_MIN_SCORE = 0.75

# Bumped whenever the rubric below changes. Two reports compared by hand cannot tell
# "the pipeline improved" from "I loosened the rubric" unless the rubric is stamped.
RUBRIC_VERSION = "2"

# Answers written by hand to be wrong in one specific way each, with the band the judge
# is expected to put them in. Versioned like the dataset, and for the same reason: a
# criterion you can edit after seeing the result is not a criterion.
CALIBRATION_PATH = PROJECT_ROOT / "evals" / "judge_calibration.jsonl"

# Stable on purpose. A later step will push these to Langfuse as scores, and a score
# that changes name between runs cannot be compared with itself.
SCORE_NAMES = {
    "groundedness_score": "judge_groundedness",
    "relevance_score": "judge_relevance",
    "completeness_score": "judge_completeness",
    "source_support_score": "judge_source_support",
    "clarity_score": "judge_clarity",
    "overall_score": "judge_overall",
}
PASS_SCORE_NAME = "judge_pass"

MainIssue = Literal[
    "unsupported_claim",
    "incomplete_answer",
    "irrelevant_answer",
    "weak_source_support",
    "unclear_answer",
    "none",
]


class JudgeVerdict(BaseModel):
    groundedness_score: float = Field(
        ge=0.0, le=1.0, description="How much of the answer the context actually supports."
    )
    relevance_score: float = Field(
        ge=0.0, le=1.0, description="How directly the answer addresses the question asked."
    )
    completeness_score: float = Field(
        ge=0.0, le=1.0, description="Whether the answer covers what the question needed."
    )
    source_support_score: float = Field(
        ge=0.0, le=1.0, description="Whether the cited sources hold up the answer."
    )
    clarity_score: float = Field(
        ge=0.0, le=1.0, description="How clear and objective the answer reads."
    )
    overall_score: float = Field(
        ge=0.0, le=1.0, description="Overall judgement, weighted, not a blind average."
    )
    passes: bool = Field(description="Whether the answer is good enough to ship.")
    reason: str = Field(description="Short justification in Portuguese.")
    main_issue: MainIssue | None = Field(
        default=None, description="The single biggest problem, or 'none'."
    )


# The rubric is the contract. Written down here, it can be reviewed, argued with and
# versioned — which is what separates a judge from asking a model if it liked the answer.
JUDGE_SYSTEM_PROMPT = (
    "You grade answers produced by a support assistant for FCAI. You never answer the "
    "question yourself.\n\n"
    "Grade ONLY against the context below. You may not use anything you know about "
    "SLAs, pricing or support outside it: if the context does not say it, it is not "
    "supported, no matter how true it sounds.\n\n"
    "Do not reward an answer for being long, and do not punish it for being short. A "
    "short answer that resolves the question is a good answer.\n\n"
    "Rubric — score each criterion between 0 and 1:\n\n"
    "groundedness_score:\n"
    "- 1.0: every important claim is supported by the context.\n"
    "- 0.5: partly supported, with gaps or claims that go beyond the context.\n"
    "- 0.0: an important claim is not supported at all.\n\n"
    "relevance_score:\n"
    "- 1.0: answers the question directly.\n"
    "- 0.5: answers partly, or talks around the topic without resolving it.\n"
    "- 0.0: irrelevant to the question.\n\n"
    "completeness_score:\n"
    "- 1.0: covers what the question needed.\n"
    "- 0.5: leaves out important information that WAS in the context.\n"
    "- 0.0: not enough to be useful.\n\n"
    "source_support_score:\n"
    "- 1.0: the listed sources clearly hold up the answer.\n"
    "- 0.5: the sources are related but do not hold up all of it.\n"
    "- 0.0: the sources do not hold up the answer.\n\n"
    "clarity_score:\n"
    "- 1.0: gets to the answer immediately, with no preamble and nothing to skip.\n"
    "- 0.5: understandable, but padded — it rambles, restates the question, or buries "
    "the fact under context nobody asked for. Being correct does not make it clear.\n"
    "- 0.0: hard to understand.\n\n"
    "overall_score: your overall judgement, NOT a blind average. Groundedness and "
    "relevance weigh more than style: a beautifully written answer that is not "
    "supported by the context is a bad answer.\n\n"
    "passes: true when the answer is good enough to ship as it is.\n\n"
    "reason: one or two sentences in Portuguese, naming the concrete problem or saying "
    "why the answer holds up.\n\n"
    "main_issue: the single biggest problem, or 'none' when there is none.\n\n"
    "Sources listed for the answer:\n{sources}\n\n"
    "Context given to the assistant:\n{context}"
)

JUDGE_PROMPT = ChatPromptTemplate.from_messages(
    [("system", JUDGE_SYSTEM_PROMPT), ("human", "Pergunta: {question}\n\nResposta: {answer}")]
)


class CalibrationProbe(BaseModel):
    """An answer broken in one specific way, and the criterion that must notice it.

    Deliberately keyed by criterion rather than by overall_score: the rubric says style
    weighs less than grounding, so a padded but correct answer *should* keep a decent
    overall. Asserting on overall there would be the probe contradicting the rubric.
    """

    id: str
    question: str
    context: list[str] = Field(min_length=1)
    sources: list[str] = Field(default_factory=list)
    answer: str
    expected_issue: MainIssue | None = None
    at_least: dict[str, float] = Field(default_factory=dict)
    at_most: dict[str, float] = Field(default_factory=dict)
    known_gap: str = ""
    note: str = ""

    @model_validator(mode="after")
    def check_probe(self) -> "CalibrationProbe":
        if not self.at_least and not self.at_most and not self.expected_issue:
            raise ValueError("a probe needs at_least, at_most or expected_issue")
        unknown = (set(self.at_least) | set(self.at_most)) - set(SCORE_NAMES)
        if unknown:
            raise ValueError(f"unknown criteria: {', '.join(sorted(unknown))}")
        return self

    def failures(self, verdict: JudgeVerdict) -> list[str]:
        problems = []
        for criterion, floor in self.at_least.items():
            value = getattr(verdict, criterion)
            if value < floor:
                problems.append(f"{criterion} {value:.2f} < {floor}")
        for criterion, ceiling in self.at_most.items():
            value = getattr(verdict, criterion)
            if value > ceiling:
                problems.append(f"{criterion} {value:.2f} > {ceiling}")
        if self.expected_issue and verdict.main_issue != self.expected_issue:
            problems.append(f"issue {verdict.main_issue} != {self.expected_issue}")
        return problems


def load_probes() -> list[CalibrationProbe]:
    if not CALIBRATION_PATH.exists():
        raise EvalRunError(f"calibration file not found: {CALIBRATION_PATH}")

    probes = []
    with CALIBRATION_PATH.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                probes.append(CalibrationProbe.model_validate_json(line))
            except ValidationError as exc:
                raise EvalRunError(f"line {number}: {exc}")

    if not probes:
        raise EvalRunError(f"no probes found in {CALIBRATION_PATH}")

    return probes


def format_judge_context(contexts: list[str]) -> str:
    return "\n\n".join(contexts)


def build_judge():
    # temperature=0 for the same reason the planner uses it: a grader that changes its
    # mind between runs turns every comparison into noise. The output cap is the same
    # governance setting the pipeline runs under.
    model = init_chat_model(
        settings.judge_model,
        model_provider="openai",
        api_key=settings.openai_api_key,
        temperature=0,
        max_tokens=settings.ai_max_output_tokens,
    )
    return model.with_structured_output(JudgeVerdict)


def passed(verdict: JudgeVerdict, min_score: float) -> bool:
    # Our bar, not the model's. The judge reports whether it would ship the answer; the
    # threshold that decides is a product decision, and --min-score is where it lives.
    return verdict.overall_score >= min_score


def run_calibration(judge) -> bool:
    """Feed the judge answers whose grade is already known, and check it agrees.

    A judge that scores everything 1.00 has two possible explanations — the pipeline is
    good, or the rubric is toothless — and from the outside they look identical. The
    only way to tell them apart is to hand it answers you know are bad. Nothing here
    touches the pipeline or the database: it is the rubric being measured, not the RAG.
    """
    probes = load_probes()
    print(f"Judge Calibration (rubric v{RUBRIC_VERSION}, model {settings.judge_model})")
    print()

    width = max(len(probe.id) for probe in probes) + 2
    clean = True
    for probe in probes:
        verdict = judge.invoke(
            JUDGE_PROMPT.invoke(
                {
                    "sources": ", ".join(probe.sources) or "none",
                    "context": format_judge_context(probe.context),
                    "question": probe.question,
                    "answer": probe.answer,
                }
            )
        )
        problems = probe.failures(verdict)
        # A probe carrying known_gap documents something the judge provably cannot see.
        # It stays in the file so the gap is visible, and it does not cry wolf.
        if problems and probe.known_gap:
            status = "KNOWN GAP: " + probe.known_gap
        elif problems:
            status = "UNEXPECTED: " + "; ".join(problems)
            clean = False
        else:
            status = "ok"

        print(
            f"{probe.id:<{width}} overall={verdict.overall_score:.2f}  "
            f"grounded={verdict.groundedness_score:.2f}  "
            f"clarity={verdict.clarity_score:.2f}  "
            f"issue={verdict.main_issue or 'none':<20} {status}"
        )

    gaps = sum(1 for probe in probes if probe.known_gap)
    print()
    if clean:
        print(f"{len(probes) - gaps}/{len(probes) - gaps} probes behaved as expected.")
        if gaps:
            print(f"{gaps} known gap(s) above: documented, not fixed.")
    else:
        print("The judge did not behave as expected. Its scores cannot be trusted yet.")
    return clean


def judge_runs(runs: list[CaseRun], judge, min_score: float) -> tuple[dict, list[dict]]:
    verdicts, errors = {}, []
    for run in runs:
        print(f"  {run.case.id}", flush=True)
        prompt = JUDGE_PROMPT.invoke(
            {
                "sources": ", ".join(run.sources) or "none",
                "context": format_judge_context(run.contexts),
                "question": run.case.question,
                "answer": run.result.answer,
            }
        )
        try:
            verdict = judge.invoke(prompt)
        except Exception as exc:
            # Structured output failing is the judge refusing to answer in the shape
            # the rubric requires. That is a judging failure, not a bad answer.
            print(f"    judge failed: {exc}", file=sys.stderr)
            errors.append({"case_id": run.case.id, "error": str(exc)})
            continue

        for field_name, score_name in SCORE_NAMES.items():
            run.scores[score_name] = getattr(verdict, field_name)
        run.scores[PASS_SCORE_NAME] = float(passed(verdict, min_score))
        verdicts[run.case.id] = verdict

    return verdicts, errors


def summarize(verdicts: dict, min_score: float) -> dict:
    values = list(verdicts.values())
    summary = {
        f"average_{field_name}": average([getattr(v, field_name) for v in values])
        for field_name in SCORE_NAMES
    }
    approved = sum(1 for verdict in values if passed(verdict, min_score))
    summary["pass_rate"] = round(approved / len(values), 4) if values else None
    summary["passed"] = approved
    return summary


def case_report(run: CaseRun, verdict: JudgeVerdict | None, min_score: float) -> dict:
    report = run.as_report()
    if verdict:
        report |= {
            "passes": passed(verdict, min_score),
            "reason": verdict.reason,
            # Kept as the model gave it: a verdict that calls an answer shippable while
            # scoring it below the bar is miscalibration, and it is worth seeing.
            "judge_would_ship": verdict.passes,
            "main_issue": verdict.main_issue,
        }
    return report


def build_report(runs: list[CaseRun], verdicts: dict, judge_errors: list[dict],
                 total_cases: int, answerable_count: int, args) -> dict:
    return {
        "run_id": str(uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": settings.judge_model,
        "rubric_version": RUBRIC_VERSION,
        "min_score": args.min_score,
        "use_rerank": not args.no_rerank,
        "total_cases": total_cases,
        "answerable_cases": answerable_count,
        "judged_cases": len(verdicts),
        "failed_before_judge": [
            run.case.id for run in runs if not run.gradable and not run.error
        ],
        "pipeline_errors": [
            {"case_id": run.case.id, "error": run.error} for run in runs if run.error
        ],
        "judge_errors": judge_errors,
        # Refusals and clarifications: the dataset never meant them for a judge.
        "skipped_cases": total_cases - answerable_count,
        "score_names": {**SCORE_NAMES, "passes": PASS_SCORE_NAME},
        "summary": summarize(verdicts, args.min_score),
        "per_case_results": [
            case_report(run, verdicts.get(run.case.id), args.min_score) for run in runs
        ],
    }


def print_report(report: dict) -> None:
    summary = report["summary"]
    print()
    print("Judge Evaluation Summary")
    print()
    print(f"Dataset cases: {report['total_cases']}")
    print(f"Answerable cases: {report['answerable_cases']}")
    print(f"Judged cases: {report['judged_cases']}")
    print(f"Failed before judge: {len(report['failed_before_judge'])}")
    print(f"Skipped: {report['skipped_cases']}")
    print()

    rows = []
    for field_name in SCORE_NAMES:
        value = summary[f"average_{field_name}"]
        criterion = field_name.removesuffix("_score").replace("_", " ")
        score = f"{value:.2f}" if value is not None else "no cases judged"
        rows.append((f"Average {criterion}:", score))
    rows.append(("Pass rate:", f"{summary['passed']}/{report['judged_cases']}"))

    width = max(len(text) for text, _ in rows) + 1
    for text, score in rows:
        print(f"{text:<{width}} {score}")

    for title, entries in [
        ("Failed before judge:", report["failed_before_judge"]),
        ("Pipeline errors:", [f"{e['case_id']}: {e['error']}" for e in report["pipeline_errors"]]),
        ("Judge errors:", [f"{e['case_id']}: {e['error']}" for e in report["judge_errors"]]),
    ]:
        if entries:
            print()
            print(title)
            for entry in entries:
                print(f"- {entry}")

    judged = [entry for entry in report["per_case_results"] if "passes" in entry]
    if len(judged) == 1:
        entry = judged[0]
        print()
        print(f"Question: {entry['question']}")
        print(f"Answer:   {entry['answer']}")
        print(f"Issue:    {entry['main_issue']}")
        print(f"Reason:   {entry['reason']}")
        return

    failing = sorted(
        (entry for entry in judged if not entry["passes"]),
        key=lambda entry: entry["scores"]["judge_overall"],
    )[:5]
    if failing:
        print()
        print("Lowest scoring cases:")
        for entry in failing:
            print(
                f"- {entry['case_id']}: overall={entry['scores']['judge_overall']:.2f}, "
                f"issue={entry['main_issue']}"
            )


def score_fraction(value: str) -> float:
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return number


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="LLM-as-a-judge evaluation of the final answer, against a rubric."
    )
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="grade the known-bad answers in evals/judge_calibration.jsonl and stop",
    )
    parser.add_argument("--limit", type=int, help="run only the first N answerable cases")
    parser.add_argument("--case-id", help="run only this case")
    parser.add_argument(
        "--no-rerank", action="store_true", help="run the pipeline without reranking"
    )
    parser.add_argument(
        "--min-score",
        type=score_fraction,
        default=DEFAULT_MIN_SCORE,
        help=f"overall score needed to pass (default {DEFAULT_MIN_SCORE})",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    use_rerank = not args.no_rerank

    try:
        # Calibration grades hand-written answers, so it needs neither the dataset nor
        # the pipeline — and it is the one thing worth running before trusting a score.
        if args.calibrate:
            ensure_within_policy(settings.judge_model)
            raise SystemExit(0 if run_calibration(build_judge()) else 1)

        cases = load_cases()
        candidates = answerable(cases)
        selected = select_cases(candidates, args.case_id, args.limit)
        if not selected:
            raise EvalRunError("no answerable case selected")

        # The judge model goes through the allowlist too, and both are checked before
        # the expensive loop: a bad key should not cost 29 pipeline runs.
        ensure_within_policy(settings.openai_chat_model, settings.judge_model)
        judge = build_judge()

        print(f"Cases to run: {len(selected)} of {len(candidates)} answerable")
        print(f"Reranking: {'on' if use_rerank else 'off'}")
        print(f"Judge model: {settings.judge_model} (rubric v{RUBRIC_VERSION})")
        print(f"Pass threshold: {args.min_score}")

        print("Running the pipeline...")
        runs = run_pipeline(selected, use_rerank)

        # An answerable case that came back without an answer has nothing to judge.
        # Grading it as zero would mix "the answer was bad" with "there was no answer".
        gradable = [run for run in runs if run.gradable]
        verdicts, judge_errors = {}, []
        if gradable:
            print("Judging...")
            verdicts, judge_errors = judge_runs(gradable, judge, args.min_score)

        report = build_report(
            runs, verdicts, judge_errors, len(cases), len(candidates), args
        )
        path = save_report("judge", report)

        print_report(report)
        print()
        print(f"Report: {path}")
    except (EvalRunError, DatasetError) as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
