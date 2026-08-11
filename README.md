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
docker compose up -d          # Postgres com pgvector (o Langfuse fica no profile
                              # observability, veja "Langfuse via OTLP")

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
OTEL_EXPORTER_OTLP_HEADERS=x-api-key=...
```

### O backend mais leve possível: Jaeger

Antes de subir o Langfuse inteiro, vale ver os spans num backend de **um container só**
(117 MB). O compose tem o Jaeger no profile `jaeger`:

```bash
docker compose --profile jaeger up -d       # UI em http://localhost:16686
```

No `.env`, aponte para ele e deixe as chaves do Langfuse vazias:

```bash
OTEL_TRACES_EXPORTER=otlp
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

Faça uma pergunta e abra <http://localhost:16686>, serviço `fcai-rag-api`: lá está o mesmo
trace, com os cinco spans, as durações e os atributos `app.*`. **Nenhuma linha de código
mudou** — só o endereço. É a demonstração mais direta de que a aplicação não conhece o
backend. O que o Jaeger não faz é entender `gen_ai.*` como uma geração com modelo, tokens
e custo; para isso, o Langfuse.

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

### Langfuse via OTLP

O console serve para desenvolver; para **ver** os traces, eles vão para o Langfuse pelo
protocolo OTLP. A aplicação continua falando só OpenTelemetry: não existe SDK do Langfuse
no projeto (`grep -rn "langfuse" app/` só encontra a montagem do endpoint e do header). O
Langfuse aqui é o backend de visualização, e trocá-lo por Phoenix, Datadog, New Relic,
AWS, Google ou Azure é trocar variável de ambiente — o pipeline não muda.

O `docker-compose.yml` já traz um Langfuse completo, atrás do profile `observability` —
quem só quer o chat não baixa nada disso:

```bash
docker compose --profile observability up -d   # sobe o pgvector e o Langfuse
```

São **seis containers**, e isso não é escolha deste projeto: é a arquitetura do Langfuse
v3/v4. A ingestão é `evento → S3 → fila → worker → ClickHouse`, então cada peça é
obrigatória — Postgres (usuários, projetos, chaves), ClickHouse (os traces), Redis (a
fila), MinIO (os blobs dos eventos), mais `web` e `worker`. É o preço de um backend que
guarda milhões de traces e calcula custo por modelo; para só *ver* os spans, o Jaeger
acima resolve com um container.

Na primeira subida ele cria organização, projeto, usuário e **as chaves de API** (é o que
fazem as variáveis `LANGFUSE_INIT_*` no compose). Abra <http://localhost:3000> e entre com
`admin@fcai.local` / `langfuse123`. Tudo ali é credencial de desenvolvimento local — em
produção, todas mudam (e o `ENCRYPTION_KEY` sai de `openssl rand -hex 32`).

No `.env` da aplicação:

```bash
OBSERVABILITY_ENABLED=true
OTEL_TRACES_EXPORTER=otlp

LANGFUSE_PUBLIC_KEY=pk-lf-fcai-rag-local
LANGFUSE_SECRET_KEY=sk-lf-fcai-rag-local
LANGFUSE_HOST=http://localhost:3000
```

Para o Langfuse Cloud em vez do local, pegue as chaves em **Settings → API Keys** do seu
projeto e troque o `LANGFUSE_HOST` por `https://cloud.langfuse.com`.

Com as duas chaves preenchidas, `app/observability.py` monta sozinho o que o Langfuse
espera, sem nada hardcoded no código:

| | |
| --- | --- |
| Endpoint | `LANGFUSE_HOST` + `/api/public/otel` (o exporter acrescenta `/v1/traces`) |
| `Authorization` | `Basic base64(public_key:secret_key)` |
| `x-langfuse-ingestion-version` | `4` |

Suba a API e faça uma pergunta:

```bash
uvicorn app.api:app --reload

curl -X POST http://127.0.0.1:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"question": "Qual é o SLA para incidentes P1 no plano Enterprise?"}'
```

No Langfuse, abra **Tracing → Traces**: cada requisição é um trace `ai.rag.pipeline` com
os spans das etapas dentro (query planning, retrieval, reranking, answer generation),
cada um com seus tempos e atributos. Filtre por `app.request_id` para chegar em uma
execução específica — é o mesmo id que aparece no log JSON e no `--debug`.

Repare em um detalhe: o `ai.rag.answer_generation` chega classificado como **generation**,
com o modelo e os tokens preenchidos, enquanto os outros são spans comuns. Ninguém disse
isso ao Langfuse — ele reconheceu os atributos `gen_ai.*` das semantic conventions. É o
que se ganha usando o vocabulário do padrão em vez de nomes próprios.

O que **não** vai junto: pergunta, prompts, contexto montado, conteúdo dos chunks,
documentos, `OPENAI_API_KEY` e `DATABASE_URL`. Os spans levam contagens, ids, flags,
filtros, tempos e tokens. Na subida, a aplicação imprime para onde os traces vão e os
**nomes** dos headers — nunca os valores, que carregam a credencial.

Sem `LANGFUSE_PUBLIC_KEY` e `LANGFUSE_SECRET_KEY`, o exporter usa direto
`OTEL_EXPORTER_OTLP_ENDPOINT` e `OTEL_EXPORTER_OTLP_HEADERS`, que é o caminho padrão para
qualquer backend OTLP (ou para um OpenTelemetry Collector no meio). E se você já tiver
definido um header à mão, ele **não** é sobrescrito pelo que o helper geraria.

### Environment-based telemetry

O que é aceitável observar muda conforme onde a aplicação roda. `APP_ENV` (`development`,
`staging` ou `production`) governa duas decisões: **quanto** se coleta e **o que** se
permite coletar.

| | development | staging | production |
| --- | --- | --- | --- |
| Exporter típico | `console` | `otlp` | `otlp` |
| Sample rate | sempre 1.0 | `OBSERVABILITY_SAMPLE_RATE` | `OBSERVABILITY_SAMPLE_RATE` |
| `OBSERVABILITY_CAPTURE_CONTENT` | pode ser `true` | **precisa ser false** | **precisa ser false** |

```bash
# Development: ver tudo, no terminal — com pergunta e resposta
APP_ENV=development
OBSERVABILITY_ENABLED=true
OBSERVABILITY_CAPTURE_CONTENT=true
OTEL_TRACES_EXPORTER=console
OBSERVABILITY_SAMPLE_RATE=1.0

# Staging: destino real, metade dos traces
APP_ENV=staging
OBSERVABILITY_ENABLED=true
OTEL_TRACES_EXPORTER=otlp
OBSERVABILITY_SAMPLE_RATE=0.5

# Production: amostragem com critério, conteúdo bruto proibido
APP_ENV=production
OBSERVABILITY_ENABLED=true
OTEL_TRACES_EXPORTER=otlp
OBSERVABILITY_SAMPLE_RATE=0.1
OBSERVABILITY_CAPTURE_CONTENT=false
```

Em development o sample rate é **sempre 1.0**, mesmo que a variável diga outra coisa:
localmente você quer ver todas as execuções. Nos outros ambientes vale o que estiver em
`OBSERVABILITY_SAMPLE_RATE`, aplicado por um sampler `ParentBased(TraceIdRatioBased)` —
`ParentBased` porque a decisão precisa valer para o trace inteiro: ou o pipeline é
gravado com suas quatro etapas, ou não é gravado. Um trace pela metade seria pior que
nenhum.

A política é conferida na subida por `validate_observability_policy()`, e a aplicação
**recusa iniciar** com mensagem clara se `APP_ENV` for inválido, se o sample rate estiver
fora de 0.0–1.0 ou se `OBSERVABILITY_CAPTURE_CONTENT=true` fora de development:

```
Invalid observability policy: OBSERVABILITY_CAPTURE_CONTENT can only be true in development.
```

Ligada, a aplicação imprime a configuração — e só o que é seguro imprimir:

```
Observability enabled.
Environment: development
Exporter: otlp
Sample rate: 1.0
Capture content: false
```

Em **production nada disso reduz a telemetria operacional**: continuam indo `request_id`,
tenant, product, feature, contagens, filtros, flags, tempos por etapa, modelo e tokens —
os mesmos atributos de sempre. O que muda entre os ambientes é o volume (sampling) e a
permissão para conteúdo.

#### Conteúdo: pergunta e resposta, só em development

Com `APP_ENV=development` **e** `OBSERVABILITY_CAPTURE_CONTENT=true`, o span raiz passa a
carregar a pergunta e a resposta final como **eventos**:

```
"events": [
  { "name": "app.debug.question", "attributes": { "app.debug.question": "Qual é o SLA para incidentes P1 no plano Enterprise?" } },
  { "name": "app.debug.answer",   "attributes": { "app.debug.answer": "O SLA para incidentes P1 no plano Enterprise é..." } }
]
```

Eventos e não atributos, de propósito: atributo descreve a operação e viaja em todo
ambiente — é por onde se filtra e se agrega. Conteúdo bruto é outra natureza de dado,
fica separado, é opcional e morre em development. Resposta de esclarecimento e recusa
também entram como `answer`.

Em staging ou production, `OBSERVABILITY_CAPTURE_CONTENT=true` **não roda**: a aplicação
recusa iniciar. Não é uma flag que "não faz nada lá" — é uma porta que só abre em
development.

E o que **nunca** sai, em ambiente nenhum, nem com a captura ligada: os prompts (planner,
reranker, resposta final), o contexto montado, o conteúdo dos chunks, os documentos, a
`OPENAI_API_KEY`, a `DATABASE_URL` e os valores dos headers OTLP.

E o `--show-prompt`: é recurso **local de terminal**, para o desenvolvedor ler o prompt na
própria tela. Ele não existe na API, não vai para span nenhum e não serve como
observabilidade de produção.

## Operational governance

Observabilidade mostra **o que aconteceu**. Governança decide **o que ainda pode
acontecer**. É a diferença entre um painel e um freio.

Antes de qualquer coisa — antes do query planner, do retrieval e de qualquer embedding —
o pipeline carrega uma política e decide se a execução pode seguir:

```bash
AI_ALLOWED_MODELS=gpt-4.1-mini              # lista separada por vírgula
AI_MONTHLY_BUDGET_USD=10.0                  # budget do par tenant/feature no mês
AI_MAX_OUTPUT_TOKENS=1200                   # teto de saída, aplicado no modelo
AI_USAGE_LEDGER_PATH=data/ai_usage.jsonl    # o histórico de uso estimado
```

| Regra | O que faz |
| --- | --- |
| `AI_ALLOWED_MODELS` | Se o `OPENAI_CHAT_MODEL` não estiver na lista, a execução é bloqueada. Serve para impedir que alguém suba um modelo caro em produção sem passar por revisão |
| `AI_MONTHLY_BUDGET_USD` | Se o gasto estimado do mês já alcançou o teto, a execução é bloqueada |
| `AI_MAX_OUTPUT_TOKENS` | Vai direto para o `init_chat_model(..., max_tokens=...)`: limita o tamanho da resposta, e portanto o custo por chamada |

Bloqueado, o usuário recebe uma resposta controlada — **não** um erro:

```
Esta solicitação não pode ser processada porque ultrapassa uma política operacional da aplicação.
```

`POST /chat` devolve `200` com essa resposta, `has_answer: false` e `sources: []`. Bloqueio
de política não é falha de sistema; é o sistema funcionando.

### O ledger

Toda execução que **realmente chamou o modelo** grava uma linha em `data/ai_usage.jsonl`:

```json
{"request_id": "5ef56f73-...", "timestamp": "2026-08-08T16:07:47+00:00", "tenant": "fcai",
 "feature": "rag_chat", "model": "gpt-4.1-mini", "input_tokens": 3522, "output_tokens": 198,
 "total_tokens": 3720, "estimated_cost_usd": 0.000647}
```

É esse arquivo que a próxima requisição lê para saber o gasto do mês — o ciclo se fecha:
usar consome budget, budget consumido bloqueia o uso. Contagens e custo, nunca pergunta,
prompt, contexto, chunk ou resposta.

**Isto não é billing.** A tabela de preços em `app/governance.py` é fictícia (`0.15 USD` por
milhão de tokens de entrada, `0.60` de saída), o arquivo é local, o ledger é lido inteiro a
cada requisição (não escala) e não há coordenação entre processos. É uma simulação
arquitetural: o objetivo é mostrar **onde** a decisão mora e **como** ela se conecta ao que
já era medido. Em produção isso seria um contador em banco ou Redis, com transação.

A política vale para o pipeline de RAG. As ferramentas de comparação (`app.chat`,
`app.context_chat`, `app.retrieve`) chamam modelo e embeddings direto, sem passar por ela —
são de laboratório, de propósito.

### Como isso aparece

- **Spans**: `app.policy.allowed`, `app.policy.reason`, `app.budget.monthly_usd`,
  `app.budget.current_spend_usd`, `app.estimated_cost_usd`
- **Log estruturado**: `policy_allowed`, `policy_reason`, `monthly_budget_usd`,
  `current_month_spend_usd`, `estimated_cost_usd`
- **`--debug` e `"debug": true`**: um bloco `policy` com a decisão, o budget e o gasto

Repare que o custo estimado atravessa as três camadas. Foi possível porque os tokens já
eram medidos desde a etapa de observabilidade — governança não precisou de instrumentação
nova, só deu **consequência** ao que já se media.

```bash
python -m app.rag_chat --debug                          # veja o bloco Policy
AI_ALLOWED_MODELS=gpt-4o python -m app.rag_chat         # modelo fora da lista: bloqueia
AI_MONTHLY_BUDGET_USD=0 python -m app.rag_chat          # budget esgotado: bloqueia
cat data/ai_usage.jsonl                                 # o histórico
```

## Evaluation dataset

Observabilidade mostra o que aconteceu e governança decide o que ainda pode acontecer.
Nenhuma das duas responde à pergunta que mais importa: **a resposta estava certa?**

Para responder isso é preciso ter contra o que comparar. Esta etapa cria essa
referência — um conjunto de casos representativos, versionado junto com o código, em
`evals/fcai_knowledge_chat.jsonl`. Uma linha por caso:

```json
{"id": "sla_p1_enterprise",
 "question": "Qual é o tempo de resposta para incidentes P1 no plano Enterprise?",
 "should_answer": true, "should_clarify": false,
 "accepted_source_files": ["product-support-sla.md", "enterprise-plan.md"],
 "required_terms": ["1 hora", "24x7"],
 "expected_doc_types": ["sla", "plan"], "expected_plan": "enterprise",
 "tags": ["rag", "sla", "enterprise", "p1"]}
```

| Campo | O que expressa |
| --- | --- |
| `should_answer` | A base sustenta uma resposta |
| `should_clarify` | A pergunta é ambígua: o certo é perguntar de volta, não adivinhar |
| `accepted_source_files` | Os documentos que podem sustentar a resposta |
| `required_terms` | O que precisa aparecer na resposta para ela ser correta |
| `expected_doc_types`, `expected_plan` | O filtro que o planner deveria ter escolhido |
| `tags` | Recortes para fatiar o resultado depois (por tema, por plano) |

É `accepted_source_files`, e não `expected_source_files`, por um motivo: o tempo de
resposta do P1 Enterprise está tanto no documento de SLA quanto no do plano. Citar
qualquer um dos dois é estar certo, e um campo que exigisse um único arquivo
transformaria uma resposta correta em falha.

Os termos são literais e verbatim da base — `1 hora`, `R$ 499`, `80%`, `próximo ciclo`,
`suporte@fcai.example.com` — justamente para não punir variação de estilo. O que se
cobra é o fato, não a redação.

### Três tipos de caso

Um dataset só de perguntas fáceis mede a coisa errada: ele premia um sistema que
responde sempre, inclusive quando deveria se recusar. Por isso os 37 casos se dividem
em três comportamentos esperados:

| Tipo | Casos | O que verifica |
| --- | --- | --- |
| Respondível | 29 | Achou o documento certo e disse o fato certo |
| Recusa | 4 | Reconheceu o que **não** está na base, em vez de inventar |
| Clarificação | 4 | Percebeu a ambiguidade e perguntou de volta |

Os casos de recusa cobrem o que a base não tem: contrato de um cliente específico, um
recurso que não existe no produto, dado operacional de uso real e número de
funcionários. Os de clarificação são perguntas cuja resposta muda conforme o plano —
"quanto eu pago no meu plano?" não tem uma resposta só.

Repare no que **não** virou recusa: "qual é o preço do plano Enterprise?". A base
responde — `sob consulta` — e essa é a resposta certa. "Não existe preço fixo" é
fundamentado; "não encontrei informação" seria falso. Marcar esse caso como recusa
cobraria do sistema exatamente o comportamento errado, e é o tipo de engano que só
aparece quando se escreve a expectativa antes de medir.

### Sincronizando com o Langfuse

O arquivo é a fonte da verdade; o Langfuse é onde ele vira dataset, e depois experiments
e scores. A sincronização valida antes de enviar:

```bash
python -m app.eval_dataset --validate-only   # só valida o JSONL, não chama o Langfuse
python -m app.eval_dataset                   # valida e sincroniza
```

```
Loading evaluation cases...
Loaded cases: 37
Validating dataset...
Dataset: fcai-knowledge-chat-v1
Synced items: 37
Dataset sync completed.
```

A validação é do próprio dataset, não do RAG: `id` único e não vazio, pergunta não
vazia, `should_answer` e `should_clarify` nunca verdadeiros ao mesmo tempo, caso de
clarificação sem arquivos de origem, caso respondível com origem e termos, `tags`
preenchidas. Os `expected_doc_types` e o `expected_plan` são validados contra os mesmos
`Literal`s que o planner usa (`app/query_planner.py`) — uma expectativa que o filtro não
consegue expressar seria impossível de medir. E cada `accepted_source_files` precisa
existir de fato em `knowledge_base/`: um nome de arquivo errado reprovaria o caso pelo
motivo errado, meses depois.

O `id` do caso é o id do item no Langfuse, então sincronizar de novo **atualiza** os
itens em vez de criar cópias. O dataset se chama `fcai-knowledge-chat-v1` — o sufixo é
proposital: mudar as expectativas depois de medir invalida a comparação, então um
critério novo vira uma `v2`, e não uma edição silenciosa da `v1`.

Requer `LANGFUSE_PUBLIC_KEY` e `LANGFUSE_SECRET_KEY` no `.env`, as mesmas chaves já
usadas pelo Langfuse na etapa de observabilidade. Sem elas, o comando avisa e para —
`--validate-only` continua funcionando offline.

### O que esta etapa ainda não é

Nada aqui executa o pipeline de RAG. Nenhuma pergunta foi respondida, nenhum modelo foi
chamado, nenhum experiment foi criado e nenhum score existe ainda. O objetivo é só ter
a base de casos — porque medir sem um critério escrito antes vira justificar o resultado
depois.

## Contract tests

O dataset diz o que se espera de cada pergunta. Os testes de contrato verificam algo
anterior a isso: se a API cumpre a **forma** que promete, independente do que o modelo
decida responder.

Eles não chamam LLM, não chamam OpenAI, não abrem o Postgres e não fazem retrieval. No
lugar do pipeline entra um `FakeRagPipeline`, injetado em `app.state.pipeline`, que
devolve sob demanda as três formas possíveis de resposta — resposta com fontes, recusa e
pergunta de volta. Assim cada cenário é exercitado de propósito, em vez de esperado.

```bash
python -m pytest        # roda tudo, em menos de 1 segundo
```

Sem flag, sem variável de ambiente, sem custo. É a suíte que roda em cada commit.

### O que é verificado

| Cenário | Contrato |
| --- | --- |
| `GET /health` | 200 e `{"status": "ok"}` |
| Resposta | `has_answer=true`, `needs_clarification=false`, ao menos uma source com `source_file` e `title` |
| Recusa | `has_answer=false`, `needs_clarification=false`, `sources` vazio |
| Clarificação | `needs_clarification=true`, `has_answer=false`, `sources` vazio |
| `debug=false` | a chave `debug` **não** aparece na resposta |
| `debug=true` | `debug` com `request_id`, `query_plan` e `timings` |
| Pergunta vazia ou só espaços | `400`, e o pipeline nunca é chamado |
| Flags | `use_rerank` e `debug` chegam ao `pipeline.run(...)` como foram pedidos |

Em todos, o contrato base: `200`, `answer` string não vazia, `has_answer` e
`needs_clarification` booleanos, `sources` lista.

### O ponto de injeção

`app/api.py` construía o `RagPipeline` no import, o que obrigava qualquer `TestClient` a
abrir banco antes do primeiro teste. Agora existe um `get_pipeline()` que constrói na
primeira chamada:

```python
def get_pipeline() -> RagPipeline:
    if not hasattr(app.state, "pipeline"):
        app.state.pipeline = RagPipeline()
    return app.state.pipeline
```

Os testes põem o fake em `app.state.pipeline` antes de subir o cliente, e o real nunca
chega a existir. Em produção nada muda, exceto que o pipeline nasce na primeira
requisição em vez de na subida do processo.

O `FakeRagPipeline` usa os modelos Pydantic de verdade (`RagPipelineResult`, `Source`,
`RagDebug`, `QueryPlanDebug`, `PipelineTimings`) — se um campo do contrato mudar, o teste
quebra na hora, que é o objetivo. E ele honra o `include_debug` como o pipeline real: o
bloco de debug só existe quando foi pedido.

### O que eles não fazem

Nada aqui julga conteúdo. Nenhum teste pergunta se a resposta cita `1 hora`, se veio do
documento certo, ou se aquela pergunta específica *deveria* ter pedido esclarecimento.
Isso é qualidade do modelo, não contrato da API — e é medido por evaluation, com o
dataset e os experiments, nas próximas etapas. Sem judge, sem Ragas, sem métrica de RAG.

## Dataset validation

O dataset é validado por conta própria, em `tests/test_eval_dataset_validation.py`. Ele
roda o mesmo `validate_dataset` do `app/eval_dataset.py` e confere que os três
comportamentos continuam cobertos: respondível, recusa e clarificação.

Essa validação **não chama o Knowledge Chat**: não sobe API, não instancia pipeline, não
chama modelo e não sincroniza nada com o Langfuse. Ela só garante que o dataset está
consistente para as aulas de evaluation que vêm a seguir — um caso quebrado apareceria lá
como experiment falhando, com a culpa caindo no pipeline; aqui aparece como teste vermelho.

## Component evaluation

Até aqui nada tinha medido o pipeline de verdade: os testes de contrato usam um fake e o
dataset só descreve o que se espera. Esta etapa é a primeira que **executa o pipeline
real contra os casos** — e a primeira que custa dinheiro, porque chama o modelo.

Mas ela para antes da resposta. Roda **query planner, filtros, retrieval e reranking**, e
não chama o `ANSWER_PROMPT` nem o `RagAnswer`. O motivo é diagnóstico: quando a resposta
final sai errada, a pergunta que importa é *onde* quebrou. Se o chunk certo nunca chegou
ao prompt, avaliar a redação da resposta é medir a consequência em vez da causa.

```bash
RUN_COMPONENT_EVALS=true python -m app.eval_components
```

Sem a variável, o script se recusa a rodar:

```
RUN_COMPONENT_EVALS=true is required because this evaluation calls LLMs.
```

É controle de custo, e é proposital que seja chato: `python -m pytest` continua grátis e
rodando em cada commit; isto aqui é uma decisão consciente de gastar tokens.

### O que o target produz

`component_target` recebe um item do dataset e devolve o estado dos três componentes,
sem opinião sobre ele:

```json
{"query_plan": {"normalized_question": "...", "doc_types": ["sla", "plan"],
                "plan": "enterprise", "exact_terms": ["P1"],
                "needs_clarification": false, "search_query": "...", "filters": {}},
 "retrieval": {"ran": true, "retrieved_count": 8,
               "retrieved_source_files": ["product-support-sla.md"],
               "retrieved_chunk_ids": ["..."]},
 "reranking": {"ran": true, "selected_count": 4,
               "selected_source_files": ["product-support-sla.md"],
               "selected_chunk_ids": ["..."]}}
```

Ele reaproveita as funções do próprio pipeline — `build_planner_prompt`, `build_filters`,
`build_search_query`, `search_chunks`, `build_rerank_prompt`, `select_reranked_documents`
— com o mesmo `OPENAI_CHAT_MODEL` e `temperature=0`. Nenhum prompt é duplicado: se o
prompt do planner mudar, a evaluation passa a medir o prompt novo, que é o ponto.

E quando o planner pede esclarecimento, o target para ali, como a produção faz:
`retrieval.ran=false`, `reranking.ran=false`, listas vazias.

### Os seis scores

Todos determinísticos — comparação de campo, sem judge e sem Ragas:

| Score | Passa quando | Não se aplica quando |
| --- | --- | --- |
| `planner_clarification_match` | `needs_clarification` == `should_clarify` | — |
| `planner_plan_match` | `plan` == `expected_plan` | o caso não espera plano |
| `planner_doc_type_coverage` | todo `expected_doc_types` está em `doc_types` | o caso não espera doc types |
| `retrieval_source_hit` | algum `accepted_source_files` foi recuperado | `should_answer=false` |
| `rerank_source_kept` | algum `accepted_source_files` sobreviveu ao rerank | `should_answer=false` |
| `clarification_skips_retrieval` | não houve retrieval nem reranking | `should_clarify=false` |

"Não se aplica" é diferente de "passou". Um caso de recusa não tem fonte aceita para
recuperar, e dar 1.0 a ele inflaria a métrica com casos que nunca foram testados. Por
isso o avaliador devolve `None` nesses casos e **nenhum score é gravado** — o denominador
do relatório é só o que era mensurável (`28/29`, e não `36/37`).

Repare que os casos de clarificação e os de recusa caem os dois no mesmo guard: o
`EvalCase` proíbe `should_answer` e `should_clarify` verdadeiros ao mesmo tempo, então
`should_answer=false` cobre as duas famílias. É a razão de `retrieval_source_hit` ter
denominador 29 e não 37 — os 4 ambíguos e os 4 negativos ficam de fora, e o relatório
mostra isso como `(8 n/a)`.

### O relatório

```
Component Evaluation Summary

Dataset: fcai-knowledge-chat-v1
Cases: 37

Planner clarification match:     35/37  (0 n/a)
Planner plan match:              14/15  (22 n/a)
Planner doc type coverage:       19/29  (8 n/a)
Retrieval source hit:            28/29  (8 n/a)
Rerank source kept:              28/29  (8 n/a)
Clarification skips retrieval:     3/4  (33 n/a)

Failures

planner_clarification_match:
- ambiguous_chamado_urgente: expected clarification, got answered path
- negative_consumo_atual: expected answered path, got clarification

retrieval_source_hit:
- company_email_financeiro: accepted company-info.md, retrieved billing-policy.md

rerank_source_kept:
- company_email_financeiro: accepted company-info.md, selected none

clarification_skips_retrieval:
- ambiguous_chamado_urgente: retrieval ran=True, reranking ran=True

Langfuse run: http://localhost:3000/project/.../runs/9661cbae31d3f54e
```

A coluna `n/a` é o que impede a leitura errada do denominador. `Clarification skips
retrieval: 3/4 (33 n/a)` diz de uma vez que a métrica só fazia sentido em 4 casos e que
os outros 33 não foram nem julgados — sem ela, alguém pode ler `3/4` como se 33 casos
tivessem sumido. E `Planner clarification match` com `0 n/a` mostra que essa é a única
métrica que se aplica ao dataset inteiro.

O relatório é a única peça onde um "não se aplica" poderia virar uma falha sem ninguém
perceber, então ele tem teste próprio em `tests/test_component_report.py` — sem modelo,
sem banco e sem Langfuse, montando as avaliações na mão. Um deles existe só para travar
o caso perigoso: se um evaluator passar a devolver `0.0` onde devolvia `None`, a suíte
quebra em vez de a métrica encolher em silêncio.

As falhas vêm agrupadas **por métrica**, não por caso, porque a pergunta que o relatório
responde é "qual componente está sangrando". Um caso que aparece em duas listas — como o
`ambiguous_chamado_urgente` acima — errou a bifurcação e levou junto tudo que dependia
dela.

Os mesmos scores ficam no Langfuse, item a item, com o `comment` acima como razão. O
terminal serve para agir agora; o Langfuse serve para comparar esta rodada com a próxima,
depois que alguém mexer no prompt do planner.

### O que esses números dizem

Vale ler o resultado real acima, porque ele é mais interessante do que "está bom":

**`Planner doc type coverage: 19/29` é o pior score, e mesmo assim `Retrieval source hit`
é 28/29.** Dez casos falharam a cobertura de doc types sem prejudicar a busca. O padrão
dominante: na maioria faltou `product`, quase sempre porque o planner escolheu só `plan`.
O chunk certo estava no `plan` mesmo, então a busca achou assim.

Some os dois: **doze dos treze casos reprovados chegaram no documento certo.** O único
que de fato falhou em recuperar foi o `company_email_financeiro`.

Isso não é o planner errando — é a expectativa sendo mais rígida do que o necessário. O
critério exige que *todos* os tipos esperados apareçam, quando o que importa de fato é
que o filtro não elimine o documento que responde. É exatamente o tipo de coisa que só
aparece quando se mede: a métrica está certa como escrita e errada como definida, e a
correção é da `v2` do dataset, não do código.

**`company_email_financeiro` é a falha que importa.** Retrieval e rerank falharam juntos:
a pergunta pelo e-mail do financeiro trouxe `billing-policy.md`, que fala de cobrança mas
não tem o endereço, e o rerank descartou tudo. O documento certo, `company-info.md`, nunca
entrou — falha de verdade, e localizada antes de qualquer resposta ser gerada.

**`ambiguous_chamado_urgente` não pediu esclarecimento** e por isso quebrou dois scores
de uma vez, o que é o comportamento correto do medidor: seguir pelo caminho errado leva
junto tudo o que dependia da bifurcação.

### Flags

```bash
RUN_COMPONENT_EVALS=true python -m app.eval_components
RUN_COMPONENT_EVALS=true python -m app.eval_components --limit 5
RUN_COMPONENT_EVALS=true python -m app.eval_components --case-id company_email_financeiro
RUN_COMPONENT_EVALS=true python -m app.eval_components --no-rerank
RUN_COMPONENT_EVALS=true python -m app.eval_components --no-langfuse
```

| Flag | Efeito |
| --- | --- |
| `--limit N` | roda só os N primeiros casos |
| `--case-id ID` | roda um caso só — o ciclo curto para depurar uma falha |
| `--no-rerank` | avalia o retrieval cru; `rerank_source_kept` some do relatório em vez de virar falha |
| `--no-langfuse` | lê o JSONL local, roda e imprime, sem gravar score nenhum |

`--no-rerank` responde a uma pergunta específica: quando um caso falha, o chunk não foi
encontrado ou foi encontrado e descartado? Se `retrieval_source_hit` passa sem rerank e
`rerank_source_kept` falha com ele, a culpa é do reranker.

### O que esta etapa ainda não é

Nenhuma resposta foi gerada e nenhuma foi julgada. Não há LLM-as-a-judge, não há Ragas,
não há agente e nada disso bloqueia commit. O que existe é o mapa de onde o pipeline erra
antes de escrever a primeira palavra da resposta — que é o que torna a avaliação da
resposta, depois, interpretável.

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
  governance.py    # política de modelo e budget, e o ledger de uso estimado
  eval_dataset.py  # valida o dataset de evaluation e sincroniza com o Langfuse
  eval_components.py # avalia planner, retrieval e rerank, sem gerar resposta
  rag_chat.py      # o chat no terminal
  api.py           # a API HTTP (FastAPI)
  retrieve.py      # busca isolada, sem modelo de chat
  chat.py          # chat sem RAG (comparação)
  context_chat.py  # chat com um documento inteiro no prompt (comparação)
knowledge_base/    # os documentos Markdown com front matter
evals/             # o dataset de evaluation em JSONL, versionado com o código
tests/             # contrato da API (com fake), dataset e relatório da evaluation
pytest.ini         # testpaths e pythonpath dos testes
test.http          # requests prontos para a extensão REST Client
docker-compose.yml
requirements.txt
.env.example
```
