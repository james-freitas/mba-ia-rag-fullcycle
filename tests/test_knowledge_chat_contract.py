"""Contract tests for the HTTP API, against a fake pipeline.

They check the shape of what /chat returns and that the request flags reach the
pipeline. No model, no database, no retrieval: the pipeline is replaced by a fake,
so the three possible shapes — an answer, a refusal, a question back — are produced
on demand instead of being hoped for.

Whether the real pipeline picks the right shape for a real question is evaluation,
and it is measured elsewhere.
"""

import pytest
from fastapi.testclient import TestClient

from app.api import app
from app.rag_pipeline import (
    PipelineTimings,
    QueryPlanDebug,
    RagDebug,
    RagPipelineResult,
    Source,
)

QUESTION = "Qual é o SLA para P1 no Enterprise?"
ANSWER = "O P1 do plano Enterprise tem resposta em até 1 hora, com atendimento 24x7."
REFUSAL = "Não encontrei informação suficiente na base de conhecimento."
CLARIFICATION = "Você poderia informar qual é o seu plano?"


class FakeRagPipeline:
    def __init__(self, result: RagPipelineResult) -> None:
        self.result = result
        self.debug: RagDebug | None = None
        self.calls: list[dict] = []

    def run(
        self,
        question: str,
        use_rerank: bool = True,
        include_debug: bool = False,
        on_prompt=None,
    ) -> RagPipelineResult:
        self.calls.append(
            {
                "question": question,
                "use_rerank": use_rerank,
                "include_debug": include_debug,
            }
        )
        # Like the real pipeline: the debug block only exists when it was asked for.
        return self.result.model_copy(
            update={"debug": self.debug if include_debug else None}
        )


def make_debug() -> RagDebug:
    return RagDebug(
        request_id="6d3f5f1a-0000-4000-8000-000000000001",
        query_plan=QueryPlanDebug(
            normalized_question="Tempo de resposta para P1 no plano Enterprise",
            doc_types=["sla", "plan"],
            plan="enterprise",
            exact_terms=["P1"],
            needs_clarification=False,
            search_query="Tempo de resposta para P1 no plano Enterprise P1",
            filters={"tenant": "fcai", "doc_type": {"$in": ["sla", "plan"]}},
        ),
        selected_chunk_ids=["product-support-sla.md#3"],
        timings=PipelineTimings(query_planning_ms=4.2, total_ms=11.7),
    )


def answer_result() -> RagPipelineResult:
    return RagPipelineResult(
        answer=ANSWER,
        has_answer=True,
        needs_clarification=False,
        sources=[
            Source(
                source_file="product-support-sla.md",
                title="SLA de Suporte ao Produto",
                section="Tempo de Resposta por Plano",
                version="2026-01",
            )
        ],
    )


def refusal_result() -> RagPipelineResult:
    return RagPipelineResult(
        answer=REFUSAL, has_answer=False, needs_clarification=False, sources=[]
    )


def clarification_result() -> RagPipelineResult:
    return RagPipelineResult(
        answer=CLARIFICATION, has_answer=False, needs_clarification=True, sources=[]
    )


@pytest.fixture
def pipeline() -> FakeRagPipeline:
    return FakeRagPipeline(answer_result())


@pytest.fixture
def client(pipeline: FakeRagPipeline):
    app.state.pipeline = pipeline
    with TestClient(app) as test_client:
        yield test_client


def ask(client, question: str = QUESTION, **flags) -> dict:
    response = client.post("/chat", json={"question": question, **flags})
    assert response.status_code == 200, response.text
    return response.json()


def assert_base_contract(payload: dict) -> None:
    assert isinstance(payload["answer"], str)
    assert payload["answer"].strip()
    assert isinstance(payload["has_answer"], bool)
    assert isinstance(payload["needs_clarification"], bool)
    assert isinstance(payload["sources"], list)


def test_health_returns_ok(client) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_answer_carries_sources(client, pipeline: FakeRagPipeline) -> None:
    payload = ask(client, debug=False, use_rerank=True)

    assert_base_contract(payload)
    assert payload["has_answer"] is True
    assert payload["needs_clarification"] is False
    assert payload["sources"]
    for source in payload["sources"]:
        for field in ("source_file", "title"):
            assert source.get(field), f"source is missing {field}: {source}"
    assert payload.get("debug") is None

    assert pipeline.calls == [
        {"question": QUESTION, "use_rerank": True, "include_debug": False}
    ]


def test_refusal_has_no_sources(client, pipeline: FakeRagPipeline) -> None:
    pipeline.result = refusal_result()

    payload = ask(client)

    assert_base_contract(payload)
    assert payload["has_answer"] is False
    assert payload["needs_clarification"] is False
    assert payload["sources"] == []


def test_clarification_has_no_sources(client, pipeline: FakeRagPipeline) -> None:
    pipeline.result = clarification_result()

    payload = ask(client)

    assert_base_contract(payload)
    assert payload["has_answer"] is False
    assert payload["needs_clarification"] is True
    assert payload["sources"] == []


def test_debug_is_returned_when_requested(client, pipeline: FakeRagPipeline) -> None:
    pipeline.debug = make_debug()

    payload = ask(client, debug=True)

    assert pipeline.calls[0]["include_debug"] is True

    debug = payload["debug"]
    assert isinstance(debug["request_id"], str)
    assert debug["request_id"]
    assert isinstance(debug["query_plan"], dict)
    assert isinstance(debug["timings"], dict)


def test_debug_is_omitted_when_not_requested(client, pipeline: FakeRagPipeline) -> None:
    pipeline.debug = make_debug()

    payload = ask(client, debug=False)

    assert pipeline.calls[0]["include_debug"] is False
    assert payload.get("debug") is None


def test_use_rerank_false_reaches_the_pipeline(
    client, pipeline: FakeRagPipeline
) -> None:
    ask(client, use_rerank=False)

    assert pipeline.calls[0]["use_rerank"] is False


@pytest.mark.parametrize("question", ["", "   "])
def test_empty_question_is_rejected(
    client, pipeline: FakeRagPipeline, question: str
) -> None:
    response = client.post("/chat", json={"question": question})

    assert response.status_code == 400
    assert response.json()["detail"]
    assert pipeline.calls == []
