"""HTTP API over the shared RAG pipeline.

Run with: uvicorn app.api:app --reload
The API only queries the existing index: ingestion and indexing never run here.
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.rag_pipeline import RagPipeline, RagPipelineResult

app = FastAPI(title="FCAI Knowledge Chat API")


def get_pipeline() -> RagPipeline:
    # Built on first use, never at import: this is what lets the contract tests put a
    # fake in app.state and exercise the API without a database or an OpenAI key.
    if not hasattr(app.state, "pipeline"):
        app.state.pipeline = RagPipeline()
    return app.state.pipeline


class ChatRequest(BaseModel):
    question: str
    debug: bool = False
    use_rerank: bool = True


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/chat", response_model_exclude_none=True)
def chat(request: ChatRequest) -> RagPipelineResult:
    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question must not be empty")

    try:
        return get_pipeline().run(
            question,
            use_rerank=request.use_rerank,
            include_debug=request.debug,
        )
    except Exception:
        # The pipeline already logged request_id, error type and message.
        raise HTTPException(status_code=500, detail="RAG pipeline failed.")
