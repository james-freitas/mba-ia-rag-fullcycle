# RAG Knowledge Chat

Assistente de perguntas e respostas sobre uma base de documentos, com RAG usando Python,
LangChain (v1) e Postgres com pgvector.

A base de exemplo é a documentação interna de uma empresa SaaS fictícia (a FCAI): oito
documentos Markdown sobre a empresa, o produto, as políticas, o SLA de suporte e os três
planos comerciais. A estrutura serve para qualquer base de Markdown com metadados.

O sistema responde **somente** com base nos documentos indexados. Quando o contexto
recuperado não sustenta a resposta, ele diz que não encontrou — não improvisa. E cita as
fontes de cada resposta.

Há dois modos de uso, ambos sobre o mesmo pipeline: um chat no terminal e uma API HTTP.

## Como funciona o pipeline

```
pergunta do usuário
   │
   ▼
1. query planner ──── pergunta ambígua? → devolve pergunta de esclarecimento e para
   │
   ▼
2. busca vetorial no pgvector (top 8) ──── nenhum chunk? → "não encontrei" e para
   │
   ▼
3. reranking (top 4) ──── nenhum chunk relevante? → "não encontrei" e para
   │
   ▼
4. resposta final
   │
   ▼
answer + sources
```

| Etapa | O que faz |
| --- | --- |
| **Query planner** (`app/query_planner.py`) | O modelo lê a pergunta e devolve um plano estruturado: a pergunta reescrita para buscar melhor, os termos literais a preservar (P1, SSO, 99,9%), os tipos de documento que podem conter a resposta e o plano comercial em questão. Se a pergunta tem mais de uma leitura possível, ele pede esclarecimento em vez de chutar. |
| **Retrieval** (`app/query_planner.py`) | Busca semântica no pgvector com a pergunta normalizada, filtrada pelos metadados que o plano indicou. Traz até 8 candidatos — generoso de propósito, para o reranking ter de onde escolher. |
| **Reranking** (`app/rerank.py`) | O modelo lê os 8 candidatos e seleciona no máximo 4 que realmente ajudam a responder. Retrieval otimiza recall; reranking otimiza precisão. Se nenhum candidato serve, o fluxo para aqui, **sem chamar o modelo de resposta**. |
| **Resposta** (`app/rag_pipeline.py`) | O modelo responde usando apenas os chunks selecionados e devolve `answer`, `has_answer` e os `used_chunk_ids` em que se baseou. |

O modelo de chat é chamado em exatamente três pontos — planner, reranker e resposta. A
ingestão e a indexação não chamam o modelo de chat, só o de embeddings.

O pipeline inteiro vive em `app/rag_pipeline.py` e devolve um objeto de dados; quem
apresenta são os dois clientes: `app/rag_chat.py` (terminal) e `app/api.py` (HTTP).

### Duas decisões que sustentam o resto

**A aplicação é dona da segurança e das fontes, o modelo não.** Tenant, produto e status
são filtros fixos no código (`SAFE_FILTERS`), fora do alcance do modelo — ele só sugere
filtros de conteúdo (`doc_type` e `plan`). E as fontes não são escritas pelo modelo: a
aplicação cruza os `used_chunk_ids` que ele citou com os metadados dos chunks que foram
mesmo recuperados, descartando qualquer id inventado.

**O filtro de metadados é eliminatório.** Ele roda antes da busca semântica, como um
`WHERE`: o que não passa não pode ser recuperado, nem que contenha exatamente a resposta.
Por isso o planner lista **todos** os tipos de documento que podem servir, não um só (o
preço de um plano mora tanto no `policy` quanto no `plan`), e deixa a lista vazia quando
não tem certeza — sem filtro de tipo é melhor do que com o filtro errado.

## Requisitos

- Python 3.11+
- Docker e Docker Compose
- Uma chave de API da OpenAI

## Setup

```bash
cp .env.example .env          # preencha OPENAI_API_KEY
docker compose up -d          # Postgres com pgvector

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python -m app.main            # valida ambiente, conexão e extensão pgvector
```

## Preparando a base

```bash
python -m app.ingest   # lê e valida os documentos, gera os chunks (data/chunks.jsonl)
python -m app.index    # gera os embeddings e indexa no pgvector
```

O `ingest` exige front matter YAML completo em cada documento (`title`, `tenant`,
`product`, `plan`, `doc_type`, `version`, `status`, `visibility`) e falha com erro claro
se faltar algo. O chunking respeita a estrutura do Markdown: primeiro divide por seções
(`MarkdownHeaderTextSplitter`), depois corta o que ficou grande demais
(`RecursiveCharacterTextSplitter`, 900 chars com 150 de overlap).

Cada chunk é prefixado com um **cabeçalho de contexto** (título, tipo, produto, plano,
versão, seção). Metadado não entra no embedding — só no filtro — então é esse cabeçalho
que torna um chunk tirado do meio de um documento autoexplicativo para a busca.
`python -m app.ingest --no-context-header` gera sem o cabeçalho, para comparar.

O `index` reindexa **apenas o que mudou**. Cada chunk carrega o hash SHA256 do documento
de origem, e o `chunk_id` é determinístico (`arquivo` + hash + posição). Comparando os
chunks atuais com o manifest da última execução (`data/index_manifest.json`), cada
documento cai em um estado:

| Estado | Ação |
| --- | --- |
| novo | indexa os chunks |
| alterado | deleta os chunks antigos e indexa os novos |
| removido | deleta os chunks do índice |
| inalterado | nada — nenhuma chamada de embedding |

Rodar `index` duas vezes seguidas não duplica nada nem gasta embeddings.
`python -m app.index --force` recria o índice do zero.

O runtime **nunca** indexa: o chat e a API só consultam o índice existente.

## Usando o chat no terminal

```bash
python -m app.rag_chat                # o chat
python -m app.rag_chat --debug        # query plan, chunks recuperados, selecionados, tempos e tokens
python -m app.rag_chat --show-prompt  # os três prompts enviados ao modelo
python -m app.rag_chat --no-rerank    # pula o reranking (usa os 4 primeiros do retrieval)
```

Perguntas que mostram cada comportamento do pipeline (rode com `--debug`):

| Pergunta | O que acontece |
| --- | --- |
| Qual é o SLA para incidentes P1 no plano Enterprise? | responde e cita as fontes |
| Como funciona o suporte no plano da empresa? | ambígua ("o plano da empresa" tem duas leituras) → pede esclarecimento |
| O que acontece se eu ultrapassar a cota de ingestão? | não nomeia plano → responde cobrindo os três |
| Qual é a capital da França? | fora da base → "não encontrei informação suficiente" |

## Usando a API

```bash
uvicorn app.api:app --reload
```

- `GET /health` → `{"status": "ok"}`
- `POST /chat` → roda o pipeline completo

```bash
curl -X POST http://127.0.0.1:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"question": "Qual é o SLA para incidentes P1 no plano Enterprise?"}'
```

O request aceita `debug` (default `false`) para incluir o diagnóstico na resposta e
`use_rerank` (default `true`) para pular o reranking. Pergunta vazia retorna `400`;
falha do pipeline retorna `500` com mensagem genérica, sem stack trace.

O arquivo `test.http` na raiz tem todos esses requests prontos para a extensão **REST
Client** do VS Code.

## Observabilidade

Toda execução do pipeline recebe um `request_id` (UUID), mede o tempo de cada etapa e
soma os tokens consumidos por modelo (via `UsageMetadataCallbackHandler` do LangChain —
tokens, não custo: o LangChain não fornece preços).

- **Log estruturado**: cada execução emite uma linha JSON no stdout com `request_id`,
  se houve resposta, se pediu esclarecimento, contagens de chunks, os arquivos usados
  como fonte, os tempos e os tokens. Só isso: nada de prompt, conteúdo de chunk,
  resposta ou credencial. Em caso de erro a linha traz o tipo e a mensagem da exceção.
- **`--debug` no terminal** e **`"debug": true` na API** devolvem o mesmo diagnóstico
  com detalhe: query plan, chunks recuperados com score, chunks selecionados, tempos por
  etapa e tokens por modelo.

### OpenTelemetry instrumentation

O pipeline também é instrumentado com **spans** de OpenTelemetry. A aplicação depende
só do OpenTelemetry — nunca do SDK de um backend específico. Para onde os traces vão é
decisão de configuração, não de código: console durante o desenvolvimento e, depois,
qualquer endpoint OTLP (Langfuse, Phoenix, Datadog, New Relic, AWS, Google, Azure).

```bash
OBSERVABILITY_ENABLED=true    # o interruptor: com false, nada de OpenTelemetry é configurado
OTEL_SERVICE_NAME=fcai-rag-api
OTEL_TRACES_EXPORTER=console  # console | otlp
```

Com `OTEL_TRACES_EXPORTER=console` cada span é impresso no terminal assim que termina —
é a forma de ver a instrumentação funcionando sem subir nada:

```bash
OBSERVABILITY_ENABLED=true OTEL_TRACES_EXPORTER=console python -m app.rag_chat
```

Para mandar os traces para uma ferramenta, troque o exporter e aponte o endpoint OTLP
(o `/v1/traces` é acrescentado pelo exporter):

```bash
OTEL_TRACES_EXPORTER=otlp
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic%20...
```

Cada execução abre um span raiz `ai.rag.pipeline` e, dentro dele, um span por etapa:

| Span | Atributos |
| --- | --- |
| `ai.rag.pipeline` | `app.request_id`, `app.feature`, `app.tenant`, `app.product`, `app.use_rerank`, `app.has_answer`, `app.needs_clarification`, `app.sources_count`, `app.total_ms` |
| `ai.rag.query_planning` | `app.normalized_question_length`, `app.doc_types`, `app.plan`, `app.exact_terms_count`, `app.needs_clarification`, `app.query_planning_ms` |
| `ai.rag.retrieval` | `app.retrieved_chunks_count`, `app.filters`, `app.retrieval_ms` |
| `ai.rag.reranking` (só com reranking ligado) | `app.selected_chunks_count`, `app.reranking_ms` |
| `ai.rag.answer_generation` | `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `app.total_tokens`, `app.answer_generation_ms`, `app.has_answer` |

A etapa de resposta usa os nomes das **semantic conventions de GenAI** (`gen_ai.*`), que o
ecossistema já sabe ler; o resto do que é específico daqui fica no prefixo `app.`.

São **contagens, ids, flags, filtros e tempos**. A pergunta, os prompts, o contexto, o
conteúdo dos chunks, a chave da API e a `DATABASE_URL` nunca viram atributo. Quando uma
etapa falha, o span é marcado com status de erro contendo só o tipo e a mensagem da
exceção — sem stack trace. Valor que não existe não é enviado.

Todo o código de OpenTelemetry vive em `app/observability.py`, com os nomes dos spans e
dos atributos em constantes: o pipeline só chama `observability.span(...)` e
`observability.set_attributes(...)`. Com `OBSERVABILITY_ENABLED=false` nenhum tracer
provider é configurado, e a própria API do OpenTelemetry devolve spans que não gravam
nada — o pipeline roda igual, no terminal e na API.

## Ferramentas de inspeção e comparação

- `python -m app.retrieve "sua pergunta"` — só a busca vetorial, sem chamar o modelo de
  chat. Aceita `--top-k` e filtros (`--plan`, `--doc-type`, …). Útil para ver o efeito
  eliminatório do filtro na prática: a mesma pergunta com o `--doc-type` errado esconde
  a resposta.
- `python -m app.chat` — chat **sem RAG**, direto no modelo. Linha de base de comparação.
- `python -m app.context_chat [arquivo.md]` — chat com **um documento inteiro** no
  prompt, sem embeddings nem busca. Funciona para um documento pequeno e mostra o
  problema que o RAG resolve quando a base cresce.

## Adicionando documentos à base

1. Crie o `.md` em `knowledge_base/` com o front matter completo.
2. Se ele introduz um `doc_type` ou `plan` novo, atualize os `Literal`s `DocType`/`Plan`
   **e** o catálogo de tipos no prompt do planner (`app/query_planner.py`). O planner só
   enxerga a base por esse catálogo, e um tipo que ele não conhece nunca seria escolhido
   — os documentos sumiriam de toda busca filtrada, em silêncio. Por isso o sistema
   valida esse contrato ao subir e falha na largada se você esquecer.
3. `python -m app.ingest && python -m app.index` — só o documento novo entra.

`plan: all` marca documentos válidos para qualquer plano; o filtro usa
`plan IN [plano, "all"]` para que eles nunca sejam descartados.

## Estrutura

```
app/
  config.py        # variáveis de ambiente
  db.py            # conexão com Postgres e extensão pgvector
  main.py          # validação do ambiente
  ingest.py        # lê, valida e gera os chunks da knowledge_base
  index.py         # indexa no pgvector só o que mudou
  manifest.py      # hash dos documentos e o manifest da indexação
  query_planner.py # planeja a busca e executa o retrieval filtrado
  rerank.py        # escolhe quais chunks recuperados entram no prompt final
  rag_pipeline.py  # o pipeline completo, compartilhado pelo terminal e pela API
  observability.py # os spans de OpenTelemetry, isolados em um só lugar
  rag_chat.py      # o chat no terminal
  api.py           # a API HTTP (FastAPI)
  retrieve.py      # busca isolada, sem modelo de chat
  chat.py          # chat sem RAG (comparação)
  context_chat.py  # chat com um documento inteiro no prompt (comparação)
knowledge_base/    # os documentos Markdown com front matter
test.http          # requests prontos para a extensão REST Client
docker-compose.yml
requirements.txt
.env.example
```
