# fc-rag-pgvector

Base inicial de um Knowledge Chat com RAG usando Python, LangChain e Postgres com pgvector.

Esta etapa prepara a fundação do projeto: ambiente, banco de dados com a extensão
`pgvector` e validação de configuração/conexão. O pipeline completo de RAG (chunking,
embeddings, retrieval e chamada ao LLM) será implementado nas próximas etapas.

## Requisitos

- Python 3.11+
- Docker e Docker Compose

## Como rodar

1. Crie o `.env` a partir do exemplo e preencha `OPENAI_API_KEY`:

   ```bash
   cp .env.example .env
   ```

2. Suba o Postgres com pgvector:

   ```bash
   docker compose up -d
   ```

3. Crie e ative o ambiente virtual:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

4. Instale as dependências:

   ```bash
   pip install -r requirements.txt
   ```

5. Valide o ambiente:

   ```bash
   python -m app.main
   ```

   Saída esperada:

   ```
   Environment loaded.
   Database connection ok.
   pgvector extension enabled.
   Project setup completed.
   ```

## Knowledge base

Os documentos de exemplo da base de conhecimento ficam em `knowledge_base/`. São
seis arquivos Markdown em português sobre uma empresa SaaS fictícia (FCAI),
cobrindo informações da empresa, visão geral do produto, política comercial, SLA de
suporte, plano Enterprise e faturamento.

Cada arquivo possui metadados no front matter YAML (`title`, `tenant`, `product`,
`plan`, `doc_type`, `version`, `status`, `visibility`) que servirão de base para
filtros de RAG.

Esses documentos serão usados nas próximas etapas para ingestão, chunking, indexação
e retrieval. Nesta etapa ainda não há RAG implementado.

## No-RAG chat

Um chat simples de terminal que chama o modelo diretamente:

```bash
python -m app.chat
```

Este chat envia a pergunta direto ao modelo (instanciado com `init_chat_model`, no
padrão da LangChain v1) e **ainda não usa a base interna** em `knowledge_base/`. A
resposta é exibida em streaming. Ele serve como linha de base para comparar, nas
próximas aulas, com as respostas obtidas quando o RAG estiver implementado. Digite
`exit` ou `quit` para encerrar.

## Full-document context chat

Uma segunda versão do chat que lê **um documento inteiro** da `knowledge_base/` e o
envia como contexto no prompt:

```bash
python -m app.context_chat
```

Por padrão usa `knowledge_base/product-support-sla.md`. É possível trocar o documento
passando o nome do arquivo como argumento:

```bash
python -m app.context_chat product-overview.md
```

O modelo é instruído a responder **somente com base no documento** e a dizer quando o
documento não tem informação suficiente. Isso **ainda não é RAG**: não há embeddings,
chunking, retrieval nem pgvector — o documento inteiro simplesmente vai no prompt.

Essa abordagem funciona bem para **documentos pequenos**, mas começa a ficar limitada
quando há muitos documentos, controle de permissões e versões, custo de tokens (o
documento inteiro é enviado a cada pergunta) e necessidade de busca seletiva do
trecho relevante — problemas que o RAG resolve nas próximas etapas.

## Initial ingestion pipeline

Pipeline inicial que lê e valida os documentos da `knowledge_base/`:

```bash
python -m app.ingest
```

O script localiza os arquivos `.md`, extrai o front matter (via `python-frontmatter`),
valida os metadados obrigatórios (`title`, `tenant`, `product`, `plan`, `doc_type`,
`version`, `status`, `visibility`) e falha com erro claro se a pasta não existir, se
nenhum documento for encontrado ou se algum documento tiver front matter/metadados
inválidos.

## Chunk generation

O mesmo comando agora também **gera chunks** dos documentos, preservando os metadados:

```bash
python -m app.ingest
```

O chunking usa os splitters do LangChain: primeiro o `MarkdownHeaderTextSplitter`
preserva a estrutura por seções (`#`, `##`, `###`) e depois o
`RecursiveCharacterTextSplitter` (`chunk_size=900`, `chunk_overlap=150`) divide seções
grandes. Cada chunk mantém os metadados do documento e adiciona `chunk_id`,
`source_file`, `chunk_index`, `section` e `content_length`.

Os chunks são salvos em `data/chunks.jsonl` (uma linha JSON por chunk; a pasta `data/`
é criada automaticamente e não é versionada).

## Indexing chunks

Gera embeddings dos chunks e os indexa no Postgres com pgvector:

```bash
python -m app.index
```

O script lê `data/chunks.jsonl`, cria um `Document` do LangChain por chunk
(preservando os metadados), gera embeddings com `OpenAIEmbeddings`
(`OPENAI_EMBEDDING_MODEL`) e salva tudo no Postgres usando a API atual do
`langchain-postgres` — `PGEngine` + `PGVectorStore` — na tabela
`fcai_knowledge_base`, usando `chunk_id` como id. A tabela é recriada a cada execução
(`overwrite_existing=True`), então rodar o script mais de uma vez **não gera
duplicidade**.

Ainda **não há retrieval nem resposta com RAG** nesta etapa — apenas a indexação.

## Isolated retrieval

Testa o retrieval isoladamente: faz a pergunta, busca os chunks mais relevantes no
Postgres (pgvector) e imprime os resultados — **sem chamar o modelo de chat**.

```bash
python -m app.retrieve "Qual é o SLA para incidentes P1 no plano Enterprise?"
```

Se nenhuma pergunta for passada como argumento, ela é pedida interativamente. Use
`--top-k` para mudar quantos chunks retornar (padrão 5):

```bash
python -m app.retrieve "Qual é o SLA para P1?" --top-k 5
```

Filtros simples por metadados (`--tenant`, `--product`, `--plan`, `--doc-type`,
`--status`) podem ser combinados:

```bash
python -m app.retrieve "Qual é o SLA para P1?" --product fcai-cloud --status published
```

Para cada resultado são exibidos score, `chunk_id`, arquivo de origem, título, seção,
plano, tipo, versão, status e um trecho do conteúdo. Esta etapa serve para inspecionar
se o retrieval encontrou o contexto certo antes de conectar ao LLM — **ainda não há
resposta com RAG**. É preciso ter rodado `python -m app.ingest` e `python -m app.index`
antes.

## RAG chat

Primeira versão do chat com RAG no terminal:

```bash
python -m app.rag_chat
```

Para cada pergunta, o chat **busca os chunks mais relevantes no pgvector** (top-k 5,
com filtros `tenant=fcai`, `product=fcai-cloud`, `status=published`), **monta um
contexto simples** com os trechos recuperados (preservando `source_file`, `title`,
`section` e `version`) e **chama o modelo** com esse contexto, instruído a responder
apenas com base nele — dizendo que não há informação suficiente quando o contexto não
sustentar a resposta.

É preciso ter rodado `python -m app.ingest` e `python -m app.index` antes. Digite `exit`
ou `quit` para sair.

## RAG answer with sources

O chat responde com **Answer** e **Sources**:

```bash
python -m app.rag_chat
```

O modelo retorna uma **saída estruturada** (`answer`, `has_answer`, `used_chunk_ids`)
via `with_structured_output`. As **fontes não são escritas pelo modelo**: a aplicação as
monta a partir dos `used_chunk_ids` cruzados com os metadados dos chunks realmente
recuperados (`source_file`, `title`, `section`, `version`). IDs inventados são
descartados. Quando o contexto não sustenta a resposta (ou nada é recuperado), o chat
responde que não há informação suficiente e imprime `Sources: No sources.` — sem chamar
o modelo no caso de retrieval vazio.

Para inspecionar o prompt final enviado ao modelo (system com o contexto + human com a
pergunta):

```bash
python -m app.rag_chat --show-prompt
```

Para inspecionar os chunks recuperados antes da resposta (rank, score, `chunk_id`,
arquivo, título, seção, versão e preview):

```bash
python -m app.rag_chat --debug
```

Ainda **não há API, frontend, streaming nem histórico de conversa**.

## Improving retrieval

Um chunk tirado do meio de um documento pode conter a resposta mas perder o contexto
(de qual documento veio, qual seção, plano, tipo e versão). Para resolver isso, o
`ingest.py` agora prefixa cada chunk com um **cabeçalho de contexto** antes de indexar:

```
Document title: SLA de Suporte ao Produto
Document type: sla
Product: fcai-cloud
Plan: all
Version: 2026-01
Section: Créditos de SLA

<conteúdo original do chunk>
```

Esse cabeçalho é o que o **embedding enxerga**, então o chunk fica autoexplicativo tanto
para a busca quanto para o prompt final. Os metadados continuam sendo salvos
separadamente (`chunk_id`, `source_file`, `title`, `section`, `tenant`, `product`,
`plan`, `doc_type`, `version`, `status`, `visibility`).

Para aplicar, regere os chunks e reindexe (a reindexação continua idempotente):

```bash
python -m app.ingest
python -m app.index
```

### Demonstração antes/depois

A flag `--no-context-header` no `ingest` regera os chunks **sem** o cabeçalho, do jeito
antigo. Isso permite um A/B usando o próprio pipeline:

```bash
# ANTES: chunks sem cabeçalho de contexto
python -m app.ingest --no-context-header
python -m app.index
python -m app.retrieve "Quais são os limites do plano Pro?" --top-k 3

# DEPOIS: chunks com cabeçalho de contexto
python -m app.ingest
python -m app.index
python -m app.retrieve "Quais são os limites do plano Pro?" --top-k 3
```

Exemplo real de diferença nessa pergunta:

| | Top 3 recuperado |
| --- | --- |
| **Antes** | `billing-policy` (Cobrança por Consumo Excedente, Exemplo de Cálculo, Cartão de Crédito) — nenhum chunk sobre planos |
| **Depois** | `enterprise-plan` (Diferenças entre Starter/Pro/Enterprise, FAQ, Benefícios) — a tabela comparativa de planos |

O motivo: o `MarkdownHeaderTextSplitter` remove a linha do cabeçalho do conteúdo, então
o chunk da seção "Pro" ficava sem a palavra "Pro" no texto — e **metadados não entram no
embedding**, só no filtro e no prompt. Com o cabeçalho, o embedding passa a enxergar
título, seção, plano, tipo e versão.

### Casos de teste manuais

Rode com `--debug` para comparar antes/depois e observar se os chunks recuperados agora
carregam título, seção, plano, tipo de documento e versão no preview:

```bash
python -m app.rag_chat --debug
```

Perguntas sugeridas:

- Qual é o tempo de resposta para P1 no plano Enterprise?
- Qual crédito de SLA é aplicado quando a disponibilidade fica entre 98,0% e 99,0%?
- O Enterprise tem SSO e SCIM?
- O que acontece se eu ultrapassar a cota de ingestão?
- Como funciona o pagamento corporativo no Enterprise?

Ainda **não há busca híbrida (BM25), reranking nem query rewriting** — o foco desta
etapa é só resolver o chunk sem contexto com uma melhoria simples na ingestão.

## Estrutura

```
app/
  config.py   # carrega e valida variáveis de ambiente
  db.py       # conexão com Postgres e extensão pgvector
  main.py     # script de validação do ambiente
  chat.py     # chat de terminal sem RAG (chama o modelo diretamente)
  context_chat.py  # chat com um documento inteiro no prompt (sem RAG)
  ingest.py   # lê, valida e gera chunks da knowledge_base (sem embeddings)
  index.py    # gera embeddings dos chunks e indexa no pgvector (sem retrieval)
  retrieve.py # busca chunks relevantes no pgvector e imprime (sem resposta RAG)
  rag_chat.py # chat com RAG: retrieval + contexto + chamada ao modelo
knowledge_base/
  company-info.md
  product-overview.md
  commercial-policy.md
  product-support-sla.md
  enterprise-plan.md
  billing-policy.md
docker-compose.yml
requirements.txt
.env.example
```
