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
   cd app
   python main.py
   ```

   Saída esperada:

   ```
   Environment loaded.
   Database connection ok.
   pgvector extension enabled.
   Project setup completed.
   ```

## Estrutura

```
app/
  config.py   # carrega e valida variáveis de ambiente
  db.py       # conexão com Postgres e extensão pgvector
  main.py     # script de validação do ambiente
docker-compose.yml
requirements.txt
.env.example
```
