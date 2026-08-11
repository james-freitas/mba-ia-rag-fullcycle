"""The quality gate: evaluation results turned into a pass/fail decision.

Six lessons produced numbers. This one turns them into an answer to a yes-or-no
question: does this build ship? Thresholds live in evals/quality_gates.json, next to the
datasets, because a bar you can lower after seeing the result is not a bar.

It reads reports that already exist and does nothing else. No model, no Langfuse, no
Postgres — it deliberately imports nothing from the rest of the app, so it runs in CI
with no credentials, no database and no .env. A gate that needs the whole stack to say
"no" is a gate nobody puts in front of a merge.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError, model_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = PROJECT_ROOT / "data" / "eval_runs"
DEFAULT_THRESHOLDS = PROJECT_ROOT / "evals" / "quality_gates.json"

PASSED, FAILED, SKIPPED = "passed", "failed", "skipped"
PASS, FAIL, MISSING = "PASS", "FAIL", "MISSING"


class GateError(Exception):
    pass


class GateSuite(BaseModel):
    required: bool
    report_prefix: str
    metrics: dict[str, float] = Field(min_length=1)

    @model_validator(mode="after")
    def check_suite(self) -> "GateSuite":
        if not self.report_prefix.strip():
            raise ValueError("report_prefix cannot be empty")
        outside = {name for name, value in self.metrics.items() if not 0.0 <= value <= 1.0}
        if outside:
            raise ValueError(f"thresholds must be between 0 and 1: {', '.join(sorted(outside))}")
        return self


class GateConfig(BaseModel):
    suites: dict[str, GateSuite] = Field(min_length=1)


def load_config(path: Path) -> GateConfig:
    if not path.exists():
        raise GateError(f"thresholds file not found: {path}")
    try:
        return GateConfig(suites=json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise GateError(f"{path}: {exc}")


def numeric_entries(values: dict) -> dict[str, float]:
    # None survives in a summary when a metric had no applicable case. Dropping it here
    # makes it MISSING rather than a threshold silently compared against nothing.
    return {
        name: float(value)
        for name, value in values.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def read_component_metrics(report: dict) -> dict[str, float]:
    return numeric_entries(report.get("summary") or report.get("metrics_summary") or {})


def read_ragas_metrics(report: dict) -> dict[str, float]:
    return numeric_entries(report.get("metrics_summary", {}))


def read_judge_metrics(report: dict) -> dict[str, float]:
    return numeric_entries(report.get("summary", {}))


def read_agent_metrics(report: dict) -> dict[str, float]:
    # The agent report prefixes every score with agent_; the thresholds name the metric.
    # Adapting here is what keeps old reports readable without rewriting them.
    return {
        name.removeprefix("agent_"): value
        for name, value in numeric_entries(report.get("summary", {})).items()
    }


READERS = {
    "component_eval": read_component_metrics,
    "ragas_eval": read_ragas_metrics,
    "judge_eval": read_judge_metrics,
    "agent_eval": read_agent_metrics,
}


def find_reports(prefix: str) -> list[Path]:
    # Sorted by name: the timestamp is in the filename by construction, so this is
    # chronological without trusting the filesystem's mtime.
    return sorted(REPORTS_DIR.glob(f"{prefix}*.json"))


def load_report(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError(f"{path}: {exc}")


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def check_suite(name: str, suite: GateSuite, path: Path | None) -> dict:
    if path is None:
        return {
            "status": FAILED if suite.required else SKIPPED,
            "report": None,
            "detail": "required report not found" if suite.required else "no report found",
            "metrics": {},
        }

    actual = READERS[name](load_report(path))
    metrics = {}
    for metric, threshold in suite.metrics.items():
        value = actual.get(metric)
        if value is None:
            metrics[metric] = {"actual": None, "threshold": threshold, "status": MISSING}
        else:
            metrics[metric] = {
                "actual": round(value, 4),
                "threshold": threshold,
                "status": PASS if value >= threshold else FAIL,
            }

    failed = any(entry["status"] != PASS for entry in metrics.values())
    return {
        "status": FAILED if failed else PASSED,
        "report": display_path(path),
        "detail": None,
        "metrics": metrics,
    }


def resolve_report(suite: GateSuite, override: str | None) -> Path | None:
    if override:
        path = Path(override)
        if not path.exists():
            raise GateError(f"report not found: {override}")
        return path

    found = find_reports(suite.report_prefix)
    return found[-1] if found else None


def build_report(config: GateConfig, results: dict, thresholds_file: Path) -> dict:
    failed_metrics = [
        f"{suite_name}.{metric}"
        for suite_name, result in results.items()
        for metric, entry in result["metrics"].items()
        if entry["status"] != PASS
    ]
    failed_metrics += [
        suite_name
        for suite_name, result in results.items()
        if result["status"] == FAILED and not result["metrics"]
    ]
    final = FAILED if any(r["status"] == FAILED for r in results.values()) else PASSED
    return {
        "run_id": str(uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "thresholds_file": display_path(thresholds_file),
        "final_status": final,
        "reports_used": {name: result["report"] for name, result in results.items()},
        "suites": results,
        "failed_metrics": failed_metrics,
    }


def save_report(report: dict) -> str:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS_DIR / f"gate_{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return display_path(path)


def print_report(report: dict) -> None:
    print()
    print("Quality Gate Summary")

    for name, result in report["suites"].items():
        print()
        header = f"{name}: {result['status'].upper()}"
        print(f"{header}  ({result['detail']})" if result["detail"] else header)
        if result["report"]:
            print(f"  report: {result['report']}")

        if not result["metrics"]:
            continue

        width = max(len(metric) for metric in result["metrics"]) + 2
        for metric, entry in result["metrics"].items():
            actual = "absent" if entry["actual"] is None else f"{entry['actual']:.2f}"
            comparison = f"{actual} >= {entry['threshold']:.2f}"
            print(f"  {metric:<{width}} {comparison:<16} {entry['status']}")

    print()
    print(f"Final result: {report['final_status'].upper()}")


def print_available_reports(config: GateConfig) -> None:
    print("Available evaluation reports")
    for name, suite in config.suites.items():
        print()
        print(f"{name}:")
        found = find_reports(suite.report_prefix)
        if not found:
            print(f"- none matching {suite.report_prefix}*.json")
        for path in found:
            print(f"- {display_path(path)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Turn existing evaluation reports into a pass/fail decision for CI."
    )
    parser.add_argument(
        "--thresholds",
        default=str(DEFAULT_THRESHOLDS),
        help=f"thresholds file (default {display_path(DEFAULT_THRESHOLDS)})",
    )
    for suite in ("component", "ragas", "judge", "agent"):
        parser.add_argument(
            f"--{suite}-report", help=f"use this report for the {suite} suite"
        )
    parser.add_argument(
        "--list-reports", action="store_true", help="list the reports found, and stop"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    try:
        config = load_config(Path(args.thresholds))

        if args.list_reports:
            print_available_reports(config)
            return

        overrides = {
            "component_eval": args.component_report,
            "ragas_eval": args.ragas_report,
            "judge_eval": args.judge_report,
            "agent_eval": args.agent_report,
        }
        unknown = set(config.suites) - set(READERS)
        if unknown:
            raise GateError(f"no reader for suites: {', '.join(sorted(unknown))}")

        results = {
            name: check_suite(name, suite, resolve_report(suite, overrides.get(name)))
            for name, suite in config.suites.items()
        }

        report = build_report(config, results, Path(args.thresholds))
        path = save_report(report)

        print_report(report)
        print(f"Report: {path}")
        raise SystemExit(0 if report["final_status"] == PASSED else 1)
    except GateError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
