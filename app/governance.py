"""Operational governance for the model calls: what may run, and how much it may cost.

Observability tells what happened; governance decides what is still allowed to
happen. The policy is checked before the pipeline touches the model, and every
run that did call the model leaves a line in a local usage ledger.

The prices here are made up for teaching. This is not billing: nobody is charged,
and the numbers do not track any provider's price list.
"""

import json
from datetime import datetime, timezone

from pydantic import BaseModel

from app.config import settings

INPUT_USD_PER_MILLION = 0.15
OUTPUT_USD_PER_MILLION = 0.60

ALLOWED_REASON = "within policy"
MODEL_NOT_ALLOWED_REASON = "model not allowed"
BUDGET_REACHED_REASON = "monthly budget reached"


class AiPolicy(BaseModel):
    tenant: str
    feature: str
    allowed_models: list[str]
    monthly_budget_usd: float
    max_output_tokens: int


class PolicyDecision(BaseModel):
    allowed: bool
    reason: str
    tenant: str
    feature: str
    model: str
    monthly_budget_usd: float
    current_month_spend_usd: float


class UsageRecord(BaseModel):
    request_id: str
    timestamp: str
    tenant: str
    feature: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_usd: float


def load_policy(tenant: str, feature: str) -> AiPolicy:
    return AiPolicy(
        tenant=tenant,
        feature=feature,
        allowed_models=settings.allowed_models,
        monthly_budget_usd=settings.ai_monthly_budget_usd,
        max_output_tokens=settings.ai_max_output_tokens,
    )


def estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    """Made-up price table, for teaching only. Not a real bill."""
    cost = (
        input_tokens * INPUT_USD_PER_MILLION + output_tokens * OUTPUT_USD_PER_MILLION
    ) / 1_000_000
    return round(cost, 6)


def current_month_spend(tenant: str, feature: str) -> float:
    month = _now().strftime("%Y-%m")
    total = 0.0

    for record in _read_ledger():
        if (
            record.get("tenant") == tenant
            and record.get("feature") == feature
            and str(record.get("timestamp", "")).startswith(month)
        ):
            total += float(record.get("estimated_cost_usd", 0.0))

    return round(total, 6)


def check_policy(policy: AiPolicy, model: str) -> PolicyDecision:
    spend = current_month_spend(policy.tenant, policy.feature)

    if model not in policy.allowed_models:
        reason, allowed = MODEL_NOT_ALLOWED_REASON, False
    elif spend >= policy.monthly_budget_usd:
        reason, allowed = BUDGET_REACHED_REASON, False
    else:
        reason, allowed = ALLOWED_REASON, True

    return PolicyDecision(
        allowed=allowed,
        reason=reason,
        tenant=policy.tenant,
        feature=policy.feature,
        model=model,
        monthly_budget_usd=policy.monthly_budget_usd,
        current_month_spend_usd=spend,
    )


def record_usage(record: UsageRecord) -> None:
    path = settings.usage_ledger_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as ledger:
        ledger.write(json.dumps(record.model_dump(), ensure_ascii=False) + "\n")


def build_usage_record(
    request_id: str,
    decision: PolicyDecision,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    estimated_cost_usd: float,
) -> UsageRecord:
    return UsageRecord(
        request_id=request_id,
        timestamp=_now().isoformat(),
        tenant=decision.tenant,
        feature=decision.feature,
        model=decision.model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        estimated_cost_usd=estimated_cost_usd,
    )


def _read_ledger() -> list[dict]:
    path = settings.usage_ledger_path
    if not path.exists():
        return []

    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def _now() -> datetime:
    return datetime.now(timezone.utc)
