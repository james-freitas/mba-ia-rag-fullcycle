# RAG Knowledge Chat

Knowledge Chat com RAG usando Python, LangChain (v1) e Postgres com pgvector.

O projeto implementa um pipeline completo de RAG em módulos pequenos e independentes:
ingestão e chunking de documentos Markdown, indexação vetorial no Postgres,
**query planner** (o modelo interpreta a pergunta e planeja a busca antes do retrieval),
busca vetorial filtrada por metadados e resposta com fontes. A base de exemplo é a
documentação interna de uma empresa SaaS fictícia (FCAI), mas a estrutura serve para
qualquer base de documentos Markdown com metadados.

## Como funciona

Fluxo de uma pergunta no chat:

```
pergunta do usuário
   │
   ▼
query planner (modelo, structured output)
   │        └── pergunta ambígua? → devolve uma pergunta de esclarecimento e para
   ▼
busca vetorial no pgvector
   (pergunta normalizada + termos exatos, com filtros de metadados)
   │
   ▼
modelo de resposta (structured output)
   │
   ▼
Answer + Sources (as fontes são montadas pela aplicação, não pelo modelo)
```

O modelo de chat é chamado em exatamente dois pontos: no query planner e na resposta
final. Ingestão e indexação não chamam o modelo de chat — apenas o modelo de
embeddings.

## Requisitos

- Python 3.11+
- Docker e Docker Compose
- Uma chave de API da OpenAI

## Setup

1. Crie o `.env` a partir do exemplo e preencha `OPENAI_API_KEY` (o exemplo já traz
   modelo de chat, modelo de embeddings e URL do banco):

   ```bash
   cp .env.example .env
   ```

2. Suba o Postgres com pgvector:

   ```bash
   docker compose up -d
   ```

3. Crie o ambiente virtual e instale as dependências:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

4. Valide ambiente, conexão e extensão pgvector:

   ```bash
   python -m app.main
   ```

## Preparando a base

```bash
python -m app.ingest   # lê, valida e gera os chunks (data/chunks.jsonl)
python -m app.index    # gera embeddings e indexa no pgvector
```

O `ingest` valida o front matter de cada documento (metadados obrigatórios: `title`,
`tenant`, `product`, `plan`, `doc_type`, `version`, `status`, `visibility`) e falha com
erro claro quando algo está faltando. O chunking preserva a estrutura do Markdown
(`MarkdownHeaderTextSplitter` por seções, depois `RecursiveCharacterTextSplitter` com
`chunk_size=900` e `chunk_overlap=150`), e cada chunk carrega os metadados do documento
mais os seus próprios (`chunk_id`, `source_file`, `chunk_index`, `section`,
`content_length`).

Cada chunk é prefixado com um **cabeçalho de contexto** (título, tipo, produto, plano,
versão e seção) antes de ser indexado. Metadados não entram no embedding — só no filtro
— então é esse cabeçalho que torna um chunk tirado do meio de um documento
autoexplicativo para a busca e para o prompt final. A flag `--no-context-header` gera
os chunks sem o cabeçalho, útil para comparar a qualidade do retrieval com e sem ele.

O `index` recria a tabela `fcai_knowledge_base` a cada execução usando `chunk_id` como
id (`PGEngine` + `PGVectorStore`, API atual do `langchain-postgres`), então reindexar é
idempotente: rodar de novo não duplica nada.

## Usando o chat

```bash
python -m app.rag_chat                # o chat com RAG
python -m app.rag_chat --debug        # imprime o QueryPlan, os filtros e os chunks recuperados
python -m app.rag_chat --show-prompt  # imprime o prompt do planner e o prompt final
```

Digite `exit` ou `quit` para sair.

O modelo de resposta retorna saída estruturada (`answer`, `has_answer`,
`used_chunk_ids`) e é instruído a responder **somente com base no contexto
recuperado**. As **fontes não são escritas pelo modelo**: a aplicação as monta cruzando
os `used_chunk_ids` com os metadados dos chunks realmente recuperados (`source_file`,
`title`, `section`, `version`) — IDs inventados são descartados. Quando o contexto não
sustenta a resposta, o chat diz que não há informação suficiente e imprime
`Sources: No sources.`.

Perguntas para ver o planner trabalhando (com `--debug`):

- E se der problema grave no Enterprise? → normaliza para P1 + SLA, `plan=enterprise`
- Como funciona o suporte no plano da empresa? → ambígua, pede esclarecimento
- Qual é o e-mail de suporte? → `doc_types` inclui `sla` (o e-mail mora no doc de SLA)
- O que acontece se eu ultrapassar a cota de ingestão? → `plan=null`, resposta cobre os três planos

## O query planner

A pergunta que o usuário digita quase nunca é a melhor pergunta para buscar no banco.
Antes do retrieval, o chat faz uma chamada ao modelo com structured output
(`QueryPlan`, em `app/query_planner.py`) que interpreta a pergunta e planeja a busca.
Cada campo do plano tem um efeito concreto:

| Campo | O que ele faz |
| --- | --- |
| `normalized_question` | reescrita autoexplicativa da pergunta; é o texto da busca semântica |
| `exact_terms` | literais (P1, SSO, 99,9%…) anexados ao texto da busca, para o embedding não perdê-los |
| `doc_types` | vira filtro `doc_type IN (...)` |
| `plan` | vira filtro `plan IN [plano, "all"]` |
| `needs_clarification` | interrompe o fluxo antes do retrieval e devolve uma pergunta de esclarecimento |

A pergunta original continua sendo a que o modelo final responde — a normalizada serve
só para buscar.

### Filtros: o que é do modelo e o que é da aplicação

O modelo **só sugere filtros de conteúdo** (`doc_types` e `plan`). Os filtros seguros
são fixos na aplicação e o modelo nunca decide sobre eles:

```python
SAFE_FILTERS = {"tenant": "fcai", "product": "fcai-cloud", "status": "published"}
```

Tenant, produto, status e autorização são responsabilidade da aplicação — deixar o
modelo escolher isso seria abrir mão do controle de acesso.

### O filtro é eliminatório: `doc_types` é uma lista generosa

O filtro de metadados roda **antes** da busca semântica, como um `WHERE`: o que não
passa nele não pode ser recuperado, nem que contenha exatamente a resposta. Como o
filtro vem de um palpite do modelo, ele precisa ser generoso — o planner lista **todos**
os tipos que podem conter a resposta, não um único:

| Pergunta | `doc_types` | Onde a resposta mora |
| --- | --- | --- |
| Quanto custa o plano Pro? | `plan`, `policy` | seção de preço do `pro-plan.md` |
| O Enterprise tem SSO e SCIM? | `plan`, `product` | `enterprise-plan.md` |
| Tempo de resposta P1 no Enterprise? | `plan`, `sla` | `enterprise-plan.md` + `product-support-sla.md` |

Quando não dá para saber, a lista fica vazia e **nenhum filtro de tipo é aplicado**.
Filtro aumenta a precisão e derruba o recall; na dúvida, é melhor buscar em tudo.

Pelo mesmo motivo, o catálogo de tipos no prompt do planner descreve o que cada tipo
**realmente contém**, não o que o nome sugere — `sla` também cobre canais de
atendimento e janelas de manutenção; `company` também cobre os e-mails de contato de
todos os departamentos. O planner só enxerga a base por esse catálogo.

### Quando o planner pede esclarecimento

Só quando a pergunta tem mais de uma leitura razoável, cada uma levando a uma resposta
diferente. Dois casos que parecem iguais são tratados de forma diferente:

- pergunta que **aponta para um plano específico sem nomear** ("o meu plano", "o plano
  da empresa") → ambígua: pede esclarecimento em vez de chutar;
- pergunta que **não menciona plano nenhum** → uma leitura só, cuja resposta apenas
  varia por plano: `plan=null` e a resposta cobre todos.

### O contrato entre o planner e o índice

`DocType` e `Plan` são `Literal`s: o structured output garante que o planner nunca
inventa um tipo. O outro lado dessa garantia: um `doc_type` novo indexado que o planner
não conhece nunca pode ser escolhido — os documentos dele sumiriam de toda busca
filtrada, silenciosamente. Por isso o chat valida o contrato na largada
(`ensure_planner_covers_index()`): os valores distintos de `doc_type` e `plan` do
índice são comparados com o que o planner conhece, e o chat encerra com instrução clara
quando há algo desconhecido:

```
The index has metadata the planner does not know: faq. Update DocType/Plan and the
catalog in PLANNER_SYSTEM_PROMPT (app/query_planner.py).
```

### Decisões de design

- **`temperature=0`** — o planner é um classificador: a mesma pergunta tem que produzir
  o mesmo plano, inclusive na decisão de pedir esclarecimento.
- **`TOP_K=8`** — sem reranking nem busca híbrida, o top-k é o único botão de recall; o
  modelo de resposta ignora bem chunks irrelevantes a mais, então o custo deles no
  contexto é menor que o custo de cortar a resposta fora.

## Retrieval isolado

Para inspecionar a busca sem chamar o modelo de chat:

```bash
python -m app.retrieve "Qual é o SLA para incidentes P1 no plano Enterprise?"
python -m app.retrieve "Qual é o SLA para P1?" --top-k 5 --doc-type sla --plan all
```

Aceita `--top-k` e filtros por metadados (`--tenant`, `--product`, `--plan`,
`--doc-type`, `--status`). Para cada resultado imprime score, `chunk_id`, arquivo,
título, seção, plano, tipo, versão, status e um trecho do conteúdo. Útil para verificar
se o contexto certo está sendo recuperado e para observar o efeito dos filtros — a
mesma pergunta com um `--doc-type` errado mostra, na prática, o filtro eliminatório
escondendo a resposta.

## Utilitários de comparação

- `python -m app.chat` — chat **sem RAG**: envia a pergunta direto ao modelo, com
  streaming. Linha de base para comparar com as respostas do RAG.
- `python -m app.context_chat [arquivo.md]` — chat com **um documento inteiro no
  prompt** (sem embeddings nem retrieval). Funciona para documentos pequenos e mostra o
  limite que o RAG resolve: muitos documentos, custo de tokens e busca seletiva.

## Knowledge base

Os documentos ficam em `knowledge_base/`: oito arquivos Markdown em português sobre a
FCAI, cobrindo informações da empresa (`company`), visão geral do produto (`product`),
políticas comercial e de faturamento (`policy`), SLA de suporte (`sla`) e um documento
por plano — Starter, Pro e Enterprise (`plan`).

Todo documento tem front matter YAML com os metadados obrigatórios. `plan: all` marca
documentos válidos para qualquer plano — o filtro de plano usa `plan IN [plano, "all"]`
para que eles nunca sejam descartados.

### Adicionando documentos

1. Crie o `.md` em `knowledge_base/` com o front matter completo.
2. Se o documento introduz um `doc_type` ou `plan` novo, atualize os `Literal`s
   `DocType`/`Plan` **e** o catálogo em `PLANNER_SYSTEM_PROMPT`
   (`app/query_planner.py`) — a validação de startup aponta exatamente isso se você
   esquecer.
3. Regere e reindexe (idempotente): `python -m app.ingest && python -m app.index`.

## Possibilidades de uso

- **Assistente de suporte interno** sobre a documentação de uma empresa ou produto —
  troque os documentos de `knowledge_base/`, ajuste os metadados e os `SAFE_FILTERS`.
- **Bancada de experimentação de RAG** — cada módulo roda isolado (retrieval sem LLM,
  chat sem RAG, documento inteiro no prompt, RAG completo), o que permite comparar
  abordagens e medir o efeito de cada peça (cabeçalho de contexto, filtros, query
  planner) com as flags de debug.
- **Base para evoluções** — busca híbrida (BM25), reranking, API e frontend, histórico
  de conversa, streaming da resposta e reindexação incremental são extensões naturais
  sobre esta estrutura; nenhuma está implementada.

## Estrutura

```
app/
  config.py        # carrega e valida variáveis de ambiente
  db.py            # conexão com Postgres e extensão pgvector
  main.py          # script de validação do ambiente
  chat.py          # chat sem RAG (linha de base)
  context_chat.py  # chat com um documento inteiro no prompt (sem RAG)
  ingest.py        # lê, valida e gera chunks da knowledge_base
  index.py         # gera embeddings e indexa no pgvector
  retrieve.py      # busca isolada no pgvector, com filtros (sem resposta RAG)
  query_planner.py # planeja a busca: entende a pergunta, monta os filtros e busca
  rag_chat.py      # o chat: query plan + retrieval + resposta com fontes
knowledge_base/    # documentos Markdown com front matter de metadados
docker-compose.yml
requirements.txt
.env.example
```
