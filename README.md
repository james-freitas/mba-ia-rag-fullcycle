# RAG Knowledge Chat

Assistente de perguntas e respostas sobre uma base de documentos, com RAG usando Python,
LangChain (v1) e Postgres com pgvector.

A base de exemplo é a documentação interna de uma empresa SaaS fictícia (a FCAI): oito
documentos Markdown sobre a empresa, o produto, as políticas, o SLA de suporte e os três
planos comerciais. A estrutura serve para qualquer base de Markdown com metadados.

O sistema responde **somente** com base nos documentos indexados. Quando o contexto
recuperado não sustenta a resposta, ele diz que não encontrou — não improvisa. E cita as
fontes de cada resposta.

## Setup

```bash
cp .env.example .env          # e preencha OPENAI_API_KEY
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
docker compose --profile observability up -d
python -m app.ingest && python -m app.index
```

```bash
python -m app.rag_chat                    # o chat no terminal
uvicorn app.api:app --reload              # a API HTTP
```

## O que este repositório é

Um pipeline de RAG que funciona, e **cinco camadas de avaliação em cima dele**. O código do
pipeline é a parte pequena; medir se ele está certo é a parte grande, e é o assunto.

| Camada | Pergunta | Custa | Comando |
| --- | --- | --- | --- |
| Contract tests | a API cumpre a forma que promete? | nada | `pytest` |
| Component evaluation | em qual etapa a informação se perdeu? | LLM | `python -m app.eval_components` |
| Ragas | a resposta é fiel ao contexto? | LLM | `python -m app.eval_ragas` |
| LLM-as-a-judge | ela atende à rubrica **deste** produto? | LLM | `python -m app.eval_judge` |
| Experiments | está melhor ou pior que a versão anterior? | LLM | `python -m app.eval_experiment` |

Cada uma responde uma pergunta diferente, e nenhuma substitui a anterior.

→ **[docs/evaluation.md](docs/evaluation.md)**

## Duas ideias que atravessam tudo

**Produzir ≠ pontuar.** Toda camada que custa roda o pipeline real, e o pipeline real chama
modelo — é o Knowledge Chat funcionando. O que varia é quem dá a nota: Ragas e judge
pontuam com modelo; componente e experiments pontuam por comparação de campo.

**Não aplicável ≠ passou.** Um caso de recusa não tem fonte aceita para recuperar. Dar
nota máxima a ele infla a métrica com casos que nunca foram testados, então o evaluator
devolve `None` e nenhum score é gravado. Por isso os denominadores variam entre métricas.

## Rodando as evaluations

```bash
python -m app.eval_dataset                              # sincroniza o dataset com o Langfuse

RUN_COMPONENT_EVALS=true python -m app.eval_components  # ~5 min
python -m app.eval_ragas                                # ~6 min
python -m app.eval_judge                                # ~5 min
python -m app.eval_experiment --variant baseline        # ~3 min
```

Todas aceitam `--limit` e `--case-id` para reduzir custo. O Ragas, o judge e os
experiments salvam relatório em `data/eval_runs/`.

## Documentação

| | |
| --- | --- |
| [docs/pipeline.md](docs/pipeline.md) | como o RAG funciona: ingestão, planner, retrieval, rerank, observabilidade, governança |
| [docs/evaluation.md](docs/evaluation.md) | as cinco camadas de avaliação, em detalhe |

## Estrutura

```
app/
  ingest.py / index.py       # lê a knowledge_base e indexa no pgvector
  query_planner.py           # planeja a busca e executa o retrieval filtrado
  rerank.py                  # escolhe quais chunks entram no prompt final
  rag_pipeline.py            # o pipeline completo
  api.py / rag_chat.py       # os dois modos de uso
  observability.py           # os spans de OpenTelemetry
  governance.py              # allowlist de modelo, budget e ledger de uso

  eval_dataset.py            # valida o dataset e sincroniza com o Langfuse
  eval_components.py         # planner, retrieval e rerank, sem gerar resposta
  eval_ragas.py              # a resposta contra o contexto usado
  eval_judge.py              # a resposta contra uma rubrica escrita
  eval_experiment.py         # duas variantes do pipeline, comparadas
  eval_runner.py             # roda o pipeline sobre o dataset, compartilhado

evals/                       # o dataset e as sondas de calibração do judge
knowledge_base/              # os documentos Markdown com front matter
tests/                       # contrato da API, dataset e a matemática dos relatórios
data/eval_runs/              # relatórios das execuções (fora do git)
```
