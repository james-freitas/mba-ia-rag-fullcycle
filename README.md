# RAG Knowledge Chat

Assistente de perguntas e respostas sobre uma base de documentos, com RAG usando Python,
LangChain (v1) e Postgres com pgvector.

A base de exemplo é a documentação interna de uma empresa SaaS fictícia (a FCAI): oito
documentos Markdown sobre a empresa, o produto, as políticas, o SLA de suporte e os três
planos comerciais. A estrutura serve para qualquer base de Markdown com metadados.

O sistema responde **somente** com base nos documentos indexados. Quando o contexto
recuperado não sustenta a resposta, ele diz que não encontrou — não improvisa. E cita as
fontes de cada resposta.

Sobre ele há um segundo projeto, menor: um **Support Triage Agent**, que escolhe entre
três ferramentas em vez de sempre responder. Ele existe para estudar evaluation de tool
calling.

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
python -m app.support_agent               # o agente de triagem
```

## O que este repositório é

Um pipeline de RAG que funciona, e **sete camadas de avaliação em cima dele**. O código do
pipeline é a parte pequena; medir se ele está certo é a parte grande, e é o assunto.

Duas coisas são avaliadas, e elas pedem medições diferentes.

**O Knowledge Chat** tem um caminho só — pergunta entra, resposta sai. Mede-se a
**resposta**:

| Camada | Pergunta | Custa | Comando |
| --- | --- | --- | --- |
| Contract tests | a API cumpre a forma que promete? | nada | `pytest` |
| Component evaluation | em qual etapa a informação se perdeu? | LLM | `python -m app.eval_components` |
| Ragas | a resposta é fiel ao contexto? | LLM | `python -m app.eval_ragas` |
| LLM-as-a-judge | ela atende à rubrica **deste** produto? | LLM | `python -m app.eval_judge` |
| Experiments | está melhor ou pior que a versão anterior? | LLM | `python -m app.eval_experiment` |

→ **[docs/evaluation.md](docs/evaluation.md)**

**O agente** não tem caminho: ele escolhe. Mede-se a **trajetória** — quais ferramentas,
em que ordem, com quais argumentos:

| Camada | Pergunta | Custa | Comando |
| --- | --- | --- | --- |
| Agent dataset | o comportamento esperado está escrito? | nada | `python -m app.eval_agent_dataset` |
| Agent evaluation | ele escolheu certo? | LLM | `python -m app.eval_agent` |

→ **[docs/agent.md](docs/agent.md)**

E uma camada que não mede nada — lê os relatórios das outras e decide:

| Camada | Pergunta | Custa | Comando |
| --- | --- | --- | --- |
| Quality gate | esse build sobe? | nada | `python -m app.eval_gate` |

→ **[docs/quality-gates.md](docs/quality-gates.md)**

## Três ideias que atravessam tudo

**Produzir ≠ pontuar.** Toda camada que custa roda o sistema real, e o sistema real chama
modelo. O que varia é quem dá a nota: Ragas e judge pontuam com modelo; componente,
experiments e agente pontuam por comparação de campo.

**Não aplicável ≠ passou.** Um caso de recusa não tem fonte aceita para recuperar. Dar
nota máxima a ele infla a métrica com casos que nunca foram testados, então o evaluator
devolve `None` e nenhum score é gravado. Por isso os denominadores variam entre métricas.

**Resposta certa ≠ caminho certo.** Um agente que responde "1320 GB, 88% da cota" pode ter
consultado o sistema ou inventado o número — **a resposta é idêntica nos dois casos**. Só
a trajetória separa os dois.

## Rodando tudo

```bash
python -m app.eval_dataset          # sincroniza os datasets com o Langfuse
python -m app.eval_agent_dataset

python -m app.eval_components       # ~5 min
python -m app.eval_ragas            # ~6 min
python -m app.eval_judge            # ~5 min
python -m app.eval_agent            # ~4 min

python -m app.eval_gate             # lê os relatórios e decide
```

Cada evaluation salva um relatório em `data/eval_runs/`, e o gate lê o mais recente de
cada. Todas aceitam `--limit` para reduzir custo — mas rodada parcial deixa métricas sem
caso aplicável, e o gate as reporta como `MISSING`.

## Security baseline

A mesma entrada não confiável tem **dois caminhos** e **duas baselines separadas** — datasets
diferentes, cujas taxas **não** devem ser somadas. O **Knowledge Chat** mede principalmente
resposta e task scope; o **Support Agent** mede tools, argumentos, trajetória e side effects
(o blast radius é maior). Perfil `baseline-no-new-guardrails`, sem mitigação nova.

### Knowledge Chat direct-input baseline

Uma suíte adversarial mede como o Knowledge Chat se comporta sob **entrada direta
maliciosa** (prompt injection, grounding bypass, extração de contexto, role escalation,
manipulação do planner e **off-task generation**) — dataset `fcai-security-direct-injection-v1`,
~33 casos em três níveis de `difficulty` (basic/intermediate/advanced) e alguns idiomas.
Roda o pipeline real (`use_rerank=True`, produção) e pontua propriedades determinísticas,
cada uma **blocking** (decide se o ataque teve sucesso) ou **diagnostic** (só sinaliza — ex.:
a resposta cita um valor falso apenas para refutá-lo). Só falhas blocking contam contra a
resistência. Registra no Langfuse e em relatório local.

**Off-task generation** testa task hijacking: pedir uma tarefa fora da finalidade (traduzir,
escrever anúncio, campanha, poema, música) usando um assunto que **existe** na base. Conteúdo
in-domain não implica que toda tarefa sobre esse conteúdo seja permitida — `grounding` e
`source_integrity` **não** equivalem a `task_scope`, então executar a tarefa é falha blocking.

Nesta baseline **nenhuma mitigação nova está ativa** (`security_controls_profile =
baseline-no-new-guardrails`): fotografa o comportamento atual para comparar quando
guardrails existirem. Não entra no quality gate ainda. A taxa é **relativa a este dataset
versionado e às suas propriedades blocking** — não é uma medida absoluta de segurança.

```bash
python -m app.eval_security --validate-only          # valida o dataset
python -m app.eval_security --sync                   # sincroniza o dataset com o Langfuse
python -m app.eval_security                          # roda os ataques e pontua
python -m app.eval_security --case-id sec_direct_031 # um caso; --limit N para um subconjunto
python -m app.eval_security --case-id sec_direct_032 --diagnose-without-rerank  # isolamento (NÃO é baseline)
```

Dataset: `evals/security_direct_injection.jsonl`. Relatórios: `data/eval_runs/`.

### Support Agent direct-input baseline

Uma suíte separada mede se uma **mensagem adversarial** faz o Support Triage Agent chamar
uma tool proibida, adicionar uma ação não solicitada, trocar argumentos (`severity`),
atravessar o tenant (`tenant_id`) ou produzir um **side effect real** (ticket em disco).
Lê as tool calls da trajetória real e mede o ticket criado por **snapshot/delta** do arquivo,
não pela flag do modelo.

```bash
python -m app.eval_security_agent --validate-only          # valida o dataset
python -m app.eval_security_agent --sync                   # sincroniza o dataset com o Langfuse
python -m app.eval_security_agent                          # roda os ataques contra o agente real
python -m app.eval_security_agent --case-id sec_agent_011  # um caso; --limit N para um subconjunto
```

Dataset: `evals/security_agent_direct_injection.jsonl`. Relatórios: `data/eval_runs/`.

## Documentação

| | |
| --- | --- |
| [docs/pipeline.md](docs/pipeline.md) | como o RAG funciona: ingestão, planner, retrieval, rerank, observabilidade, governança |
| [docs/evaluation.md](docs/evaluation.md) | as cinco camadas que avaliam a resposta |
| [docs/agent.md](docs/agent.md) | o agente, suas ferramentas e a avaliação de trajetória |
| [docs/quality-gates.md](docs/quality-gates.md) | thresholds e a decisão de CI |
| [docs/security-threat-model.md](docs/security-threat-model.md) | threat model: assets, trust boundaries, riscos abertos |

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

  eval_dataset.py            # valida o dataset do chat e sincroniza
  eval_components.py         # planner, retrieval e rerank, sem gerar resposta
  eval_ragas.py              # a resposta contra o contexto usado
  eval_judge.py              # a resposta contra uma rubrica escrita
  eval_experiment.py         # duas variantes do pipeline, comparadas
  eval_runner.py             # roda o pipeline sobre o dataset, compartilhado

  support_tools.py           # as três ferramentas do agente
  support_agent.py           # o agente que escolhe entre elas
  eval_agent_dataset.py      # valida o dataset de comportamento e sincroniza
  eval_agent.py              # roda o agente e pontua a trajetória

  eval_gate.py               # lê os relatórios e decide — sem chamar nada

evals/                       # os datasets, as sondas de calibração e os thresholds
knowledge_base/              # os documentos Markdown com front matter
tests/                       # contrato da API, dataset e a matemática dos relatórios
data/eval_runs/              # relatórios das execuções (fora do git)
```
