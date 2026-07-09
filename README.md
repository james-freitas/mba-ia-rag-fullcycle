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

## Estrutura

```
app/
  config.py   # carrega e valida variáveis de ambiente
  db.py       # conexão com Postgres e extensão pgvector
  main.py     # script de validação do ambiente
  chat.py     # chat de terminal sem RAG (chama o modelo diretamente)
  context_chat.py  # chat com um documento inteiro no prompt (sem RAG)
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
