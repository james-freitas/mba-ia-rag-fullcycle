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

O pipeline em si — ingestão, planner, retrieval, reranking, observabilidade e governança
— está em **[docs/pipeline.md](docs/pipeline.md)**. Este arquivo trata do que vem
depois: **como se sabe se ele está funcionando.**

---

## As cinco camadas de avaliação

Cada uma responde uma pergunta diferente. Nenhuma substitui a anterior.

| Camada | Pergunta | Custa | Comando |
| --- | --- | --- | --- |
| Contract tests | a API cumpre a forma que promete? | nada | `pytest` |
| Component evaluation | em qual etapa a informação se perdeu? | LLM | `app.eval_components` |
| Ragas | a resposta é fiel ao contexto? | LLM | `app.eval_ragas` |
| LLM-as-a-judge | ela atende à rubrica **deste** produto? | LLM | `app.eval_judge` |
| Experiments | está melhor ou pior que a versão anterior? | LLM | `app.eval_experiment` |

Duas distinções que atravessam tudo:

**Produzir ≠ pontuar.** Toda camada acima da primeira roda o pipeline real, e o pipeline
sempre chama modelo — é o Knowledge Chat funcionando. O que varia é quem dá a nota: Ragas
e judge usam modelo para pontuar; componente e experiments usam comparação de campo.

**Não aplicável ≠ passou.** Um caso de recusa não tem fonte aceita para recuperar. Dar
nota máxima a ele infla a métrica com casos que nunca foram testados, então o evaluator
devolve `None` e nenhum score é gravado. Por isso os denominadores variam entre métricas.

---

## O dataset

`evals/fcai_knowledge_chat.jsonl` — 37 casos versionados junto do código, uma linha cada:

```json
{"id": "sla_p1_enterprise",
 "question": "Qual é o tempo de resposta para incidentes P1 no plano Enterprise?",
 "should_answer": true, "should_clarify": false,
 "accepted_source_files": ["product-support-sla.md", "enterprise-plan.md"],
 "required_terms": ["1 hora", "24x7"],
 "expected_doc_types": ["sla", "plan"], "expected_plan": "enterprise",
 "tags": ["rag", "sla", "enterprise", "p1"]}
```

Três famílias, porque um dataset só de perguntas fáceis premia um sistema que responde
sempre — inclusive quando deveria se recusar:

| Tipo | Casos | Verifica |
| --- | --- | --- |
| Respondível | 29 | achou o documento certo e disse o fato certo |
| Recusa | 4 | reconheceu o que **não** está na base, em vez de inventar |
| Clarificação | 4 | percebeu a ambiguidade e perguntou de volta |

**`accepted_source_files`, não `expected`:** o SLA do P1 Enterprise está tanto no
documento de SLA quanto no do plano. Citar qualquer um é estar certo, e exigir um único
arquivo transformaria resposta correta em falha.

**`required_terms` são literais** (`1 hora`, `R$ 499`, `80%`) para não punir variação de
estilo. Cobra-se o fato, não a redação. E eles **não são gabarito** — voltam nas métricas
que precisam de resposta de referência.

O arquivo é a fonte da verdade; o Langfuse é onde ele vira dataset, experiments e scores:

```bash
python -m app.eval_dataset --validate-only   # só valida o JSONL, offline
python -m app.eval_dataset                   # valida e sincroniza
```

A validação confere `id` único, as três famílias, e que cada `accepted_source_files`
existe em `knowledge_base/` — um nome errado reprovaria o caso pelo motivo errado, meses
depois. Os `expected_doc_types` e `expected_plan` são validados contra os mesmos
`Literal` que o planner usa: expectativa que o filtro não consegue expressar é
immensurável.

O sufixo `v1` é proposital. Mudar as expectativas depois de medir invalida a comparação,
então critério novo vira `v2`.

---

## Contract tests

```bash
python -m pytest        # 33 testes, menos de 1 segundo, custo zero
```

Verificam a **forma** que a API promete, independente do que o modelo decida responder.
Não chamam LLM, não abrem Postgres, não fazem retrieval: no lugar do pipeline entra um
`FakeRagPipeline` injetado em `app.state.pipeline`, que devolve sob demanda as três
formas possíveis de resposta.

| Cenário | Contrato |
| --- | --- |
| Resposta | `has_answer=true`, `needs_clarification=false`, source com `source_file` e `title` |
| Recusa | `has_answer=false`, `sources` vazio |
| Clarificação | `needs_clarification=true`, `has_answer=false`, `sources` vazio |
| `debug=false` / `debug=true` | a chave `debug` some / traz `request_id`, `query_plan`, `timings` |
| Pergunta vazia | `400`, e o pipeline nunca é chamado |

O `app/api.py` constrói o pipeline na primeira chamada, não no import — é isso que
permite pôr o fake antes de o real existir.

Além dos testes de API, a suíte cobre a validação do dataset e a matemática dos
relatórios de evaluation (`tests/test_component_report.py`,
`tests/test_experiment_report.py`). Nenhum deles julga conteúdo: se a resposta cita
`1 hora` é qualidade do modelo, medida pelas camadas abaixo.

---

## Component evaluation

```bash
RUN_COMPONENT_EVALS=true python -m app.eval_components
```

Roda **query planner, filtros, retrieval e reranking** contra o dataset, e para antes da
resposta. O motivo é diagnóstico: quando a resposta final sai errada, a pergunta é *onde*
quebrou — e se o chunk certo nunca chegou ao prompt, avaliar a redação mede a
consequência em vez da causa.

A variável de ambiente é controle de custo, e é proposital que seja chata: `pytest`
continua grátis; isto aqui é decisão consciente de gastar tokens. Não é a governança do
pipeline — o script não passa pela allowlist nem pelo ledger.

### Os seis scores

| Score | Passa quando | Não se aplica quando |
| --- | --- | --- |
| `planner_clarification_match` | `needs_clarification` == `should_clarify` | — |
| `planner_plan_match` | `plan` == `expected_plan` | o caso não espera plano |
| `planner_doc_type_coverage` | todo `expected_doc_types` está em `doc_types` | o caso não espera doc types |
| `retrieval_source_hit` | alguma fonte aceita foi recuperada | `should_answer=false` |
| `rerank_source_kept` | alguma fonte aceita sobreviveu ao rerank | `should_answer=false` |
| `clarification_skips_retrieval` | não houve retrieval nem reranking | `should_clarify=false` |

```
Planner clarification match:     35/37  (0 n/a)
Planner plan match:              14/15  (22 n/a)
Planner doc type coverage:       19/29  (8 n/a)
Retrieval source hit:            28/29  (8 n/a)
Rerank source kept:              28/29  (8 n/a)
Clarification skips retrieval:     3/4  (33 n/a)

Failures

retrieval_source_hit:
- company_email_financeiro: accepted company-info.md, retrieved billing-policy.md
```

A coluna `n/a` impede a leitura errada do denominador: `3/4 (33 n/a)` diz que a métrica
só fazia sentido em 4 casos. As falhas vêm agrupadas **por métrica**, porque a pergunta
que o relatório responde é qual componente está sangrando.

### O que os números dizem

**`doc type coverage` é o pior score (19/29) e mesmo assim `retrieval source hit` é
28/29.** Doze dos treze casos reprovados chegaram no documento certo. O critério exige
que *todos* os tipos esperados apareçam, quando o que decide é se o filtro elimina o
documento que responde. A métrica está certa como escrita e errada como definida — e a
correção é da `v2` do dataset, não do prompt.

**`company_email_financeiro` é a falha real**, e ela ilustra o limite da ferramenta. O
planner passou na métrica dele; o reranker recusou corretamente; quem perdeu a informação
foi a busca. Mas a causa foi uma decisão do planner: ele liberou também `policy`, e os
oito chunks de `billing-policy.md` ocuparam o top-K inteiro, empurrando para fora o único
chunk com o e-mail.

Ou seja: **o score diz onde a informação se perdeu, não de quem é a culpa.** Isso ainda é
infinitamente melhor que só saber que "o chat não respondeu".

### Flags

```bash
RUN_COMPONENT_EVALS=true python -m app.eval_components --limit 5
RUN_COMPONENT_EVALS=true python -m app.eval_components --case-id company_email_financeiro
RUN_COMPONENT_EVALS=true python -m app.eval_components --no-rerank    # a métrica de rerank some do relatório
RUN_COMPONENT_EVALS=true python -m app.eval_components --no-langfuse  # lê o JSONL local, não grava score
```

`--no-langfuse` **não** é offline: continua chamando OpenAI e pgvector, só não escreve no
servidor. Quem reduz custo é `--limit` e `--case-id`.

---

## Ragas evaluation

```bash
python -m app.eval_ragas --limit 3
```

Começa onde a anterior termina: a resposta existe, e a pergunta é se ela diz o que o
contexto sustenta. Aqui as métricas **não são escritas por nós** — faithfulness não é
comparação de campo, exige um modelo lendo a resposta.

Só os **29 casos respondíveis** entram. Medir fidelidade numa recusa correta daria zero e
puniria o sistema por ter acertado.

| Métrica | Pergunta | Precisa de gabarito? |
| --- | --- | --- |
| `faithfulness` | a resposta afirma só o que o contexto sustenta? | não |
| `response_relevancy` | ela responde à pergunta feita? | não |
| `context_precision` | o contexto recuperado era relevante? | não |

**`context_recall` ficou de fora.** Precision pergunta "do que eu trouxe, quanto
prestava?"; recall pergunta "do que prestava, quanto eu trouxe?" — e para responder a
segunda seria preciso saber de antemão a lista completa, ou seja, uma resposta de
referência. O dataset tem `required_terms`, que é um checklist, não um gabarito. Esticar
um no outro seria inventar a verdade que a evaluation deveria conferir.

**Use a métrica que os seus dados sustentam. Não fabrique dado para caber na métrica.**

### Os contextos, e por que não vazam

O Ragas precisa do **texto** dos chunks, e `docs/pipeline.md` documenta que conteúdo de
chunk nunca entra na telemetria. A saída é um canal que a API não tem como acionar:

```python
def run_for_evaluation(self, question, use_rerank=True) -> EvaluationRun:
    contexts: list[str] = []
    result = self.run(question, use_rerank=use_rerank, on_context=contexts.extend)
    return EvaluationRun(result=result, selected_contexts=contexts)
```

Os textos vão para um callback que o chamador fornece. Como `selected_contexts` vive no
`EvaluationRun` e não no `RagPipelineResult`, **a API não tem como serializar um campo
que não existe.** O relatório local guarda `contexts_count`, nunca os contextos.

### Falhas separadas

| Campo | Significa | Consertar onde |
| --- | --- | --- |
| `failed_before_ragas` | rodou e não produziu resposta | retrieval ou grounding |
| `pipeline_errors` | estourou naquele caso (timeout, 429) | infra; dá para re-rodar só ele |
| `metric_errors` | a medição falhou; score fica `null` | a métrica |

Nota zero misturaria "a resposta estava ruim" com "não houve resposta" — problemas
diferentes, consertos diferentes. E um erro no vigésimo quinto caso não descarta os
vinte e quatro anteriores.

### Duas armadilhas de dependência

**O Ragas 0.4.3 não importa** com `langchain-community` 0.4.2, que removeu o caminho do
`ChatVertexAI` ([issue #2745](https://github.com/vibrantlabsai/ragas/issues/2745)). O
workaround que circula — fixar `ragas<0.4` — não resolve, porque o problema é a
transitiva. Daí o único pin do `requirements.txt`.

**E ele liga para casa.** O Ragas envia um evento de uso para um endpoint próprio depois
de **cada** chamada de modelo, de forma síncrona e sem `try/except`, de dentro do código
assíncrono das métricas. Trava o event loop que o `batch_score` usa, e uma instabilidade
de rede ali derruba o lote inteiro. Por isso a primeira linha executável do módulo é
`os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")`.

Vale como hábito: quando você põe uma biblioteca nova no caminho crítico, abra e veja o
que ela faz.

### Custo e flags

```bash
python -m app.eval_ragas --limit 5
python -m app.eval_ragas --case-id sla_p1_enterprise   # mostra a resposta que gerou o score
python -m app.eval_ragas --no-rerank
```

Gasta duas vezes: o pipeline inteiro por caso, e depois cada métrica chamando o modelo de
novo. Mais de cem chamadas nos 29 casos. Comece por `--limit 3`. Relatórios em
`data/eval_runs/`.

---

## LLM-as-a-judge evaluation

```bash
python -m app.eval_judge --limit 5
```

O Ragas mede propriedades que outra pessoa definiu. Um judge é o movimento oposto: você
escreve o que "resposta boa" significa **neste produto**, e um modelo aplica a definição.

### A rubrica é o contrato

Cinco critérios, cada um com os três pontos de ancoragem escritos — sem eles, "dê uma
nota de 0 a 1" é convite para o modelo devolver 0.8 em tudo:

| Critério | 1.0 | 0.5 | 0.0 |
| --- | --- | --- | --- |
| `groundedness` | tudo sustentado pelo contexto | parcial, com extrapolações | afirmação sem apoio |
| `relevance` | responde diretamente | responde em parte | irrelevante |
| `completeness` | cobre o que a pergunta pedia | falta algo que **estava** no contexto | insuficiente |
| `source_support` | as fontes sustentam a resposta | relacionadas, mas não sustentam tudo | não sustentam |
| `clarity` | vai direto à resposta | compreensível, mas prolixo | difícil de entender |

O `overall_score` **não é a média**: groundedness e relevância pesam mais que estilo. E o
judge é proibido de usar conhecimento externo — se o contexto não diz, não está
sustentado, por mais verdadeiro que soe.

O veredito vem por `init_chat_model(...).with_structured_output(JudgeVerdict)`. Um judge
que resolve escrever um ensaio **quebra na validação do Pydantic**, em vez de produzir um
score inutilizável que ninguém percebe.

### O limiar é da aplicação

```python
def passed(verdict: JudgeVerdict, min_score: float) -> bool:
    return verdict.overall_score >= min_score
```

O prompt **não** informa o limiar ao judge. Se informasse, ele ancoraria as notas em
volta da linha de corte e duas rodadas com `--min-score` diferente deixariam de ser
comparáveis. O judge responde "você entregaria isso?" e o corte é aplicado depois. O
veredito dele fica no relatório como `judge_would_ship`: quando os dois discordam, isso é
descalibração e vale ver.

### Calibrar antes de confiar

Nos 29 casos o judge deu **1.00 em todos os critérios**. Um judge que nunca discrimina
não carrega informação — e esse número tem duas explicações opostas que, de fora, são
idênticas: ou o pipeline é muito bom, ou a rubrica é frouxa.

```bash
python -m app.eval_judge --calibrate
```

```
Judge Calibration (rubric v2, model gpt-4.1-mini)

grounded_answer     overall=1.00  grounded=1.00  clarity=1.00  issue=none               ok
invented_claim      overall=0.30  grounded=0.00  clarity=1.00  issue=unsupported_claim  ok
off_topic           overall=0.00  grounded=0.00  clarity=1.00  issue=irrelevant_answer  ok
incomplete_answer   overall=0.30  grounded=0.00  clarity=1.00  issue=incomplete_answer  ok
unclear_answer      overall=0.90  grounded=1.00  clarity=0.50  issue=unclear_answer     ok
weak_source_support overall=1.00  grounded=1.00  clarity=1.00  issue=none               KNOWN GAP: ...

5/5 probes behaved as expected.
1 known gap(s) above: documented, not fixed.
```

As sondas são respostas escritas à mão, erradas de **um** jeito cada, versionadas em
`evals/judge_calibration.jsonl` — critério que você pode editar depois de ver o resultado
não é critério. A cobrança é **por critério**, não pelo `overall`: a sonda prolixa cobra
`clarity_score <= 0.6` e deixa o overall em paz, porque a rubrica diz que estilo pesa
menos. Sonda que cobrasse overall ali contradiria a rubrica que deveria testar.

**A calibração achou dois problemas na primeira execução.** A rubrica de clareza dizia só
"clear and objective", fácil demais de satisfazer — a resposta prolixa passava com 1.00.
Reescrita, virou `RUBRIC_VERSION = "2"`, que o relatório carimba: sem isso, comparando
dois relatórios daqui a um mês não dá para separar "o pipeline melhorou" de "eu afrouxei
a rubrica".

E o `source_support_score` era **immensurável**: o judge recebe as fontes como nomes de
arquivo e o contexto sem atribuição, sem como ligar um ao outro. A sonda ficou marcada
com `known_gap` — reporta o buraco sem reprovar a rodada. Consertar exige o pipeline
entregar a origem de cada trecho.

Ou seja: a calibração não deu selo de qualidade. Achou um critério frouxo e um critério
impossível, ambos invisíveis enquanto tudo pontuava 1.00.

### Flags

```bash
python -m app.eval_judge --calibrate                   # não toca pipeline nem banco
python -m app.eval_judge --case-id sla_p1_enterprise   # mostra resposta, issue e reason
python -m app.eval_judge --no-rerank
python -m app.eval_judge --min-score 0.9
```

Por padrão usa o mesmo `OPENAI_CHAT_MODEL`. Dá para separar com `OPENAI_JUDGE_MODEL` —
útil no dia em que o modelo que responde mudar e você quiser que o critério não mude
junto. Sem a variável, nada quebra.

---

## Langfuse experiments

As camadas anteriores respondem "isso está bom?". Esta responde a pergunta que antecede
um deploy: **está melhor ou pior do que o que já está no ar?**

Um número sozinho não diz isso. `28/29` é ótimo ou alarmante dependendo do que a rodada
de ontem marcou.

São três comandos, nesta ordem:

```bash
python -m app.eval_experiment --variant baseline     # 37 casos COM rerank    (~3 min)
python -m app.eval_experiment --variant no-rerank    # 37 casos SEM rerank    (~3 min)
python -m app.eval_experiment --compare baseline no-rerank
```

As duas variantes são o mesmo pipeline — `use_rerank` já era uma flag que ele aceitava.
Uma comparação só vale se houver exatamente **uma** diferença entre os lados.

### Critérios sem modelo

Os quatro scores não usam IA para julgar. **O pipeline continua chamando modelo** — ele
tem que responder as 37 perguntas — mas quem dá a nota é comparação de campo. Se o
medidor tivesse variação própria, você não saberia se a diferença entre as rodadas veio
do pipeline ou do instrumento.

| Score | Passa quando |
| --- | --- |
| `experiment_answer_shape` | os campos não se contradizem entre si |
| `experiment_expected_behavior` | respondeu / esclareceu / recusou conforme o esperado |
| `experiment_accepted_source_match` | citou alguma das fontes aceitas |
| `experiment_required_terms_match` | a resposta contém todos os `required_terms` |

O `answer_shape` merece nota, porque a primeira versão estava errada: eu checava tipos
(`answer` é string, `has_answer` é booleano) e **isso não pode falhar** — o
`RagPipelineResult` é Pydantic, os tipos já estão garantidos. Era métrica travada em
37/37 ocupando uma coluna. Agora checa o que o Pydantic não garante: coerência entre os
campos, como afirmar uma resposta sem fonte nenhuma.

### O resultado

```
Metric                 baseline    candidate   diff
Answer shape           37/37       37/37       +0
Expected behavior      34/37       33/37       -1
Accepted source match  28/29       27/29       -1
Required terms match   26/29       24/29       -2
Average latency        4527 ms     3509 ms     -1018 ms
Total tokens           118222      68120       -50102
Estimated cost         0.0204 USD  0.0122 USD  -0.0082 USD
```

**Este é o formato de uma decisão de engenharia.** Tirar o reranking economiza 42% dos
tokens e um segundo por pergunta, e custa quatro regressões.

Nenhum lado é obviamente certo — chat interno de baixo volume é uma conversa, milhões de
chamadas por dia é outra. O ponto não é que a ferramenta decide: é que agora existe o que
discutir, em vez de opinião.

### As duas guardas

**`--compare` aceita nomes de variante** e pega o relatório mais recente de cada um,
imprimindo quais escolheu. Os arquivos têm timestamp, e caçar o par certo entre uma dúzia
é como se acaba comparando os dois errados. Caminhos explícitos continuam funcionando.

**E ele se recusa a comparar o incomparável:**

```
the runs cover different cases (...), so the diff would be meaningless: 6 vs 37 cases
```

Mesma checagem para datasets diferentes. É a lição do `rubric_version` de novo: a
comparação precisa saber quando **não** deve ser feita.

### O que vai para o Langfuse

Scores item a item, mais os **agregados no próprio dataset run** via `run_evaluators`:
`experiment_expected_behavior_rate`, `experiment_average_latency_ms`,
`experiment_total_tokens`, `experiment_estimated_cost_usd`. Sem isso, comparar duas
rodadas separadas por semanas na interface seria impossível — os números nunca teriam
sido enviados. O `--compare` é o ciclo curto; o Langfuse é o longo.

A política é checada **antes** de qualquer escrita: um budget estourado faz o pipeline
recusar todos os casos em silêncio, e aqui — diferente das evaluations locais — isso
ficaria gravado sob o nome real do experiment, lendo para sempre como uma regressão que
nunca aconteceu.

### Isso ainda não é gate de CI

O `--compare` imprime o diff e vai embora. Quem decide se `-2` em `required_terms_match`
é aceitável é uma pessoa. Automatizar exige uma conversa que a comparação sozinha não
resolve: qual métrica trava o merge, qual só avisa, e quanta variação é ruído — e o
pipeline não é determinístico, então rodadas iguais dão números levemente diferentes.

---

## Support agent tools

O Knowledge Chat responde perguntas. Um **Support Triage Agent** faria mais: consultaria
o consumo do cliente, decidiria se o caso vira chamado, e abriria o chamado.

Esta etapa cria só as **ferramentas** que ele vai usar. Ainda não existe agente, e nada
aqui é avaliado — mas as decisões de agora determinam o que será possível medir depois.

```bash
python -m app.support_tools                      # só as duas locais, custo zero
python -m app.support_tools --include-rag-tool   # também a de RAG, que chama modelo
```

### As três, e por que são essas

Elas cobrem os três tipos de coisa que um agente faz:

| Ferramenta | O que é | Fonte |
| --- | --- | --- |
| `search_knowledge_base` | **consultar** — reusa o `RagPipeline` inteiro | a base indexada |
| `get_current_usage` | **ler estado** — dado transacional simulado | `data/current_usage.json` |
| `create_support_ticket` | **agir** — escreve, e é a que se observa de perto | `data/support_tickets.jsonl` |

`search_knowledge_base` devolve `answer` e as fontes, e só. Prompt, chunks e o payload de
debug ficam dentro do pipeline: transcrição de agente não é lugar para eles.

Os dois arquivos em `data/` são falsos, e de naturezas diferentes. O `current_usage.json`
é **dado semente** — versionado, porque a ferramenta não funciona sem ele (é a única
exceção ao `data/` no `.gitignore`). O `support_tickets.jsonl` é **artefato de execução**,
criado pela ferramenta e fora do git.

Nada sai daqui: nenhum e-mail, nenhum sistema externo. O ticket é uma linha num arquivo.

### Ferramentas pequenas são ferramentas avaliáveis

Foi o critério de desenho. Uma ferramenta que faz uma coisa legível é uma ferramenta cuja
chamada dá para dizer, depois, se foi certa ou errada. `create_support_ticket` recebendo
`severity` e `summary` é verificável; um `handle_support_request` genérico não seria.

E as descrições delas são o que o modelo lê para decidir. Por isso dizem também o que
**não** é para fazer:

```python
"""Open a support ticket.

Use ONLY when the user clearly asks to open one. Answering a question, however
urgent it sounds, is not a request for a ticket.
"""
```

Essa frase existe porque "meu sistema caiu, qual o SLA do P1?" é uma pergunta, não um
pedido de chamado — e é exatamente onde um agente mal instruído abre ticket sozinho.

### Duas validações, em dois lugares diferentes

O `@tool` transforma a assinatura num schema, e o `Literal["P1", "P2", "P3"]` vira um
enum que o modelo lê antes de chamar. Um valor fora dele é rejeitado pelo Pydantic
**antes de a função rodar** — e isso levanta exceção, não devolve JSON:

```
ValidationError: Input should be 'P1', 'P2' or 'P3'
```

Já "o summary não pode ser vazio" o schema não consegue expressar. Essa fica dentro da
função, e volta como dado:

```
{"error": "summary must not be empty"}
{"error": "unknown tenant: acme"}
```

**O schema valida o que ele consegue expressar; a função valida o resto.** Eu tinha
escrito uma checagem de `severity` dentro da função também, achando que era defesa em
profundidade — o type checker apontou que era código inalcançável, e ele estava certo.

A distinção importa para o agente: erro devolvido como dado é algo a que o modelo pode
reagir na mesma volta do laço; exceção é o runtime do agente que trata. E as duas coisas
vão precisar ser avaliadas de formas diferentes.

### Custo

O `search_knowledge_base` **chama modelo** — é o pipeline de RAG inteiro. Por isso a demo
padrão pula ele: as outras duas são leitura e escrita de arquivo, custo zero.

---

## Support Triage Agent

```bash
python -m app.support_agent
python -m app.support_agent --debug
```

O Knowledge Chat tem um caminho só: pergunta entra, resposta sai. Aqui o modelo
**decide** — responder pela documentação, ler o consumo do tenant, abrir um chamado,
perguntar de volta, ou recusar.

É um mini-projeto para estudar evaluation de tool calling. Ele **não substitui o
Knowledge Chat** (na verdade usa, através de uma ferramenta) e não é uma implementação
completa de agentes: um `create_agent`, três ferramentas, sem memória e sem
multi-agente.

### As cinco decisões

| Pergunta | Ferramenta esperada |
| --- | --- |
| "Qual é o SLA para P1 no Enterprise?" | `search_knowledge_base` |
| "Quanto de ingestão usamos este mês?" | `get_current_usage` |
| "Abra um chamado P1, o painel está fora do ar" | `create_support_ticket` |
| "Abre um chamado urgente aí" | **nenhuma** — pede esclarecimento |
| "Qual a previsão do tempo amanhã?" | **nenhuma** — recusa |

As duas últimas são as difíceis, e são o motivo de o agente existir como objeto de
estudo. Chamar ferramenta demais é tão errado quanto chamar de menos — e abrir um chamado
que ninguém pediu é o erro que tem consequência.

### Trajetória, lida das mensagens

```bash
python -m app.support_agent --debug
```

```
Answer: Neste mês, você usou 1320 GB de ingestão, 88% da cota de 1500 GB.

Steps: 1
  1. get_current_usage({'tenant_id': 'fcai'})
  needs_clarification=False, ticket_created=False, task_completed=True
  reason: Informar o uso atual de ingestão conforme solicitado.
```

As chamadas vêm do `AIMessage.tool_calls` que o framework registra — **nunca do texto que
o modelo escreveu sobre si mesmo**. Uma trajetória reconstruída da narração do modelo
estaria medindo a narração.

É essa lista que a próxima aula vai avaliar: qual ferramenta, em que ordem, com quais
argumentos.

### Resultado validado

O `response_format=SupportAgentResult` faz o agente devolver campos, não só prosa:

```python
class SupportAgentResult(BaseModel):
    answer: str
    needs_clarification: bool
    ticket_created: bool
    ticket_id: str | None
    task_completed: bool
    reason: str
```

`ticket_created` e `ticket_id` existem para poder cruzar com a trajetória: se o modelo
disser que abriu um chamado e `create_support_ticket` não estiver na lista de calls, ele
inventou. Isso é verificável **porque** as duas coisas são capturadas separadamente.

### Um prompt que estava conservador demais

Vale como exemplo de por que os cinco cenários existem. A primeira versão do system
prompt dizia:

> If severity, impact or a summary is missing, ask one clarification question.

O modelo leu como checklist obrigatório e, diante de *"Abra um chamado P1 porque o painel
está fora do ar para todos os usuários"* — que traz severidade e problema —, pediu
"impacto detalhado e um resumo breve". Correto pela letra do prompt, errado pelo produto.

A correção foi separar o que falta do que já foi dado:

> To open a ticket you need two things: a severity, and what is broken. When the user
> gives you both, that is enough: write the summary yourself. Do not ask for detail they
> already gave.

Nenhum código mudou. **Só o prompt** — e é exatamente esse tipo de regressão que a
evaluation de trajetória vai pegar sem alguém testar à mão.

### Custo

Cada rodada chama modelo — o agente sempre, e mais uma vez o pipeline inteiro quando ele
decide buscar na base.

---

## Support agent evaluation dataset

```bash
python -m app.eval_agent_dataset --validate-only   # valida o JSONL, offline
python -m app.eval_agent_dataset                   # valida e sincroniza
```

`evals/support_triage_agent.jsonl` — 20 casos. O dataset do Knowledge Chat guarda
**respostas esperadas**; este guarda **comportamento esperado**.

A diferença importa: um agente que chega na resposta certa pelas ferramentas erradas é
outro sistema. Só o segundo é seguro de mudar depois.

```json
{"id": "agent_chain_usage_then_ticket",
 "input": "Estamos perto do limite de logs? Se estivermos acima de 85%, abra um chamado P2.",
 "should_answer": true, "should_clarify": false,
 "should_create_ticket": true, "should_refuse": false,
 "expected_tools": ["get_current_usage", "create_support_ticket"],
 "forbidden_tools": ["search_knowledge_base"],
 "expected_arguments": {"create_support_ticket": {"severity": "P2"}},
 "expected_trajectory": ["get_current_usage", "create_support_ticket"],
 "trajectory_match_type": "in_order", "max_steps": 2,
 "expected_terms": ["88"], "tags": ["agent", "chained", "usage", "ticket"]}
```

| Campo | O que cobra |
| --- | --- |
| `expected_tools` | as que **deveriam** ser chamadas |
| `forbidden_tools` | as que **não** deveriam — chamar demais é erro tão real quanto de menos |
| `expected_arguments` | argumentos mínimos: aqui, `severity` tem que ser `P2` |
| `expected_trajectory` | a **sequência** esperada |
| `trajectory_match_type` | `exact`, `in_order` ou `any_order` |
| `max_steps` | teto de chamadas — o freio contra o agente que fica tentando |

### As cinco famílias, e as duas armadilhas

| Família | Casos | O que verifica |
| --- | --- | --- |
| Documentação | 5 | usa `search_knowledge_base`, e só |
| Uso atual | 5 | usa `get_current_usage`, e só |
| Ticket claro | 4 | abre com a severidade pedida |
| Ambíguo | 3 | **não** abre, pergunta de volta |
| Fora de escopo | 3 | recusa sem chamar nada |

Duas famílias existem só para pegar erro de escolha, não de resposta.

**A confusão política × dado vivo.** Dois casos quase idênticos em português:

- *"O que acontece se eu ultrapassar a cota de ingestão?"* → é **regra**, está num documento
- *"Quanto eu já usei da minha cota neste mês?"* → é **estado**, está num sistema

Estão marcados com a tag `confusion-pair`. Um agente que responde consumo lendo
documentação inventa número; um que responde política lendo o consumo não tem o que
dizer. Os dois erros passariam por uma avaliação que só olha a resposta final.

**O caso encadeado.** Sem ele, todos os casos esperariam uma ferramenta só, e
`expected_trajectory` e `trajectory_match_type` seriam maquinário que nada testa. Por isso
a validação **reprova o dataset** se nenhum caso tiver mais de um passo:

```python
if not any(len(case.expected_trajectory) > 1 for case in cases):
    errors.append("no case expects more than one tool: the trajectory is never tested")
```

### As regras que o validador impõe

Treze, e todas verificadas. As que valem citar:

- `should_clarify` e `should_create_ticket` não podem ser verdadeiros juntos, nem
  `should_refuse` com `should_create_ticket` — são desfechos que se excluem
- caso de recusa ou de clarificação **não pode esperar ferramenta nenhuma**
- `expected_trajectory` não pode conter ferramenta fora de `expected_tools`
- `len(expected_trajectory)` não pode passar de `max_steps`

E a mais importante: os nomes de ferramenta válidos vêm do próprio código.

```python
TOOL_NAMES = frozenset(tool.name for tool in SUPPORT_TOOLS)
```

Um caso que espera uma ferramenta que o agente não tem não é um caso que falha — é um
caso **immensurável**. Renomear uma ferramenta quebra a validação do dataset na hora, em
vez de virar uma métrica misteriosamente zerada dois meses depois.

---

## Support agent evaluation

```bash
python -m app.eval_agent
```

Roda os 20 casos contra o agente e pontua **comportamento**, não resposta. Nove
evaluators, todos determinísticos — comparação de lista contra o que o dataset escreveu.
Sem Ragas e sem judge, de propósito: o objeto medido já é bastante não-determinístico, e
um medidor com variação própria tornaria duas rodadas incomparáveis.

| Score | O que cobra |
| --- | --- |
| `agent_tool_selection` | as ferramentas esperadas foram chamadas |
| `agent_forbidden_tools` | as proibidas não foram |
| `agent_argument_match` | argumentos mínimos, por **subset** — o `summary` é do agente |
| `agent_trajectory_match` | a sequência, conforme `exact` / `in_order` / `any_order` |
| `agent_max_steps` | não passou do teto de chamadas |
| `agent_clarification` | perguntou de volta quando devia |
| `agent_refusal` | recusou sem chamar nada |
| `agent_ticket_creation` | abriu o chamado, e o id veio da ferramenta |
| `agent_expected_terms` | a resposta contém os termos esperados |

O `ticket_creation` é o que cruza as duas capturas: exige `ticket_created=true`, um
`ticket_id` não vazio **e** `create_support_ticket` na trajetória. Um modelo afirmando um
chamado que nunca abriu falha aqui.

### Tickets falsos são resetados

`create_support_ticket` acrescenta linhas, então uma segunda rodada pontuaria contra
tickets da primeira. Antes de começar:

```
Resetting fake support tickets for evaluation...
```

É mais simples do que passar um `run_id` por uma ferramenta que esta etapa não deve
alterar.

### O resultado

```
Support Agent Evaluation Summary

Dataset cases: 20

Tool selection:    19/20
Forbidden tools:   20/20
Argument match:    4/5
Trajectory match:  19/20
Max steps:         20/20
Clarification:     18/20
Refusal:           0/3
Ticket creation:   19/20
Expected terms:    8/8
```

`Forbidden tools: 20/20` e `Max steps: 20/20` são resultados de verdade: o agente nunca
chamou ferramenta proibida e nunca ficou tentando. Era o risco principal, e não se
concretizou.

E `Refusal: 0/3` é o resultado mais interessante do dia.

### Três falhas, e só uma é do agente

**`Refusal: 0/3` — o critério está errado, não o agente.** Olhe o caso:

```
input : Qual é a previsão do tempo amanhã?
answer: Desculpe, mas não forneço informações sobre previsão do tempo. Posso ajudar
        com questões relacionadas ao FCAI Cloud.
tools : []
flags : clarif=False ticket=False done=True
```

Recusa impecável: nenhuma ferramenta, nada inventado. O que reprova é o
`task_completed=true` que o evaluator exige falso.

Só que o agente está certo. `task_completed` significa "a solicitação foi tratada por
inteiro" — e recusar **é** tratar por inteiro. Não sobrou nada pendente. O campo que
deveria ser falso é o de clarificação, onde algo de fato ficou em aberto.

**A métrica está certa como escrita e errada como definida.** É a mesma forma do
`planner_doc_type_coverage` da aula de componentes. E a correção pertence a uma `v2` do
critério — não a um ajuste feito depois de ver o resultado, que é exatamente o que a
aula de calibração ensina a não fazer.

**`agent_doc_billing_email` — a falha é herdada do pipeline.**

```
input : Para qual e-mail eu envio uma dúvida sobre faturamento?
answer: Não encontrei na base de conhecimento o e-mail específico para dúvidas sobre
        faturamento.
tools : ['search_knowledge_base']
```

O agente escolheu a ferramenta certa. Ela é que não achou — e esse é o
`company_email_financeiro`, o mesmo caso cujo retrieval falha desde a aula de evaluation
por componente, três camadas abaixo.

Ele então marcou `needs_clarification=true` e perguntou outra coisa, quando o certo seria
dizer que a informação não está na base. Vale notar de onde vem essa ambiguidade: a
ferramenta devolve só prosa, sem um campo dizendo que a busca voltou vazia.

**`agent_ticket_p3_config_question` — genuinamente discutível.** *"Abra um chamado P3
para uma dúvida de configuração de alertas."* A severidade está lá, mas qual é a dúvida
não está. O agente pediu para especificar; o dataset esperava o chamado aberto.

Um chamado dizendo "usuário tem uma dúvida sobre configuração de alertas" é quase inútil
para quem vai atender. Este é um caso para reescrever, não um agente para consertar.

### Flags

```bash
python -m app.eval_agent --limit 5
python -m app.eval_agent --case-id agent_ticket_p1_outage
python -m app.eval_agent --experiment-name fcai-support-triage-agent-baseline
```

Os agregados também vão para o dataset run no Langfuse (`agent_tool_selection_rate` e
companhia), pelo mesmo motivo da aula de experiments: comparar duas rodadas separadas por
semanas na interface exige que os números tenham sido enviados.

### Custo

Cada caso executa o agente, e os casos de documentação executam o pipeline de RAG inteiro
por dentro. É a evaluation mais cara da série depois da de experiments. Comece por
`--limit 5`.

---

## Quality gates

Seis etapas produziram números. Esta transforma os números numa resposta de sim ou não:
**esse build sobe?**

```bash
python -m app.eval_gate
```

Ele lê relatórios que **já existem** e não faz mais nada. Sem modelo, sem Langfuse, sem
Postgres — e de propósito não importa nada do resto da aplicação:

```bash
grep -n "^from app\|^import app" app/eval_gate.py    # não retorna nada
```

Roda em CI sem credencial, sem banco e sem `.env`. Um portão que precisa da stack inteira
para dizer "não" é um portão que ninguém coloca na frente de um merge.

### Os thresholds

Ficam em `evals/quality_gates.json`, ao lado dos datasets, pelo mesmo motivo deles: **uma
barra que você abaixa depois de ver o resultado não é barra.**

```json
"agent_eval": {
  "required": true,
  "report_prefix": "agent_",
  "metrics": {
    "forbidden_tools_pass_rate": 1.00,
    "trajectory_match_pass_rate": 0.75
  }
}
```

`forbidden_tools` em 1.00 é a única métrica sem tolerância no arquivo. Chamar uma
ferramenta proibida não tem grau — ou aconteceu, ou não.

`required` decide o que acontece quando o relatório não existe: suíte obrigatória sem
relatório **reprova**; opcional vira `SKIPPED`.

### Uma execução mostra os quatro estados

```
Quality Gate Summary

component_eval: FAILED  (required report not found)

ragas_eval: PASSED
  faithfulness         1.00 >= 0.75     PASS

judge_eval: FAILED
  average_overall_score        absent >= 0.75   MISSING

agent_eval: FAILED
  forbidden_tools_pass_rate    1.00 >= 1.00     PASS
  argument_match_pass_rate     0.60 >= 0.80     FAIL
  refusal_pass_rate            0.00 >= 0.90     FAIL

Final result: FAILED
```

**`MISSING` é diferente de `FAIL`.** Métrica configurada que não aparece num relatório
presente reprova do mesmo jeito, mas por outro motivo: ou o relatório é de uma rodada
degenerada, ou o nome mudou. As duas coisas precisam de ação humana, e nenhuma é "a
qualidade caiu".

### Adaptadores, porque os relatórios não falam a mesma língua

O relatório do agente guarda `agent_tool_selection_pass_rate`; os thresholds nomeiam
`tool_selection_pass_rate`. O do Ragas usa `metrics_summary`, os outros usam `summary`.

```python
def read_agent_metrics(report: dict) -> dict[str, float]:
    return {
        name.removeprefix("agent_"): value
        for name, value in numeric_entries(report.get("summary", {})).items()
    }
```

Quatro funções de três linhas, e nenhum relatório antigo precisou ser reescrito. É o que
permite o gate ler o histórico que já existe em vez de começar do zero.

### O que a primeira execução revelou

**`component_eval` nunca vai passar**, porque o `eval_components` não salva relatório
local — ele imprime e manda para o Langfuse. A suíte está marcada como obrigatória, então
o gate reprova, corretamente, por relatório ausente. Para ficar verde, aquele evaluator
precisa ganhar um `save_report`, que é mudança em código de outra etapa.

**O gate pega o relatório mais recente**, e o mais recente pode ser um `--case-id`. Foi o
que aconteceu com o judge: o último relatório era de um caso só que não julgou nada, e
todas as métricas vieram `MISSING`. Passando o relatório explícito da rodada completa,
passa:

```bash
python -m app.eval_gate --judge-report data/eval_runs/judge_<timestamp>.json
```

É uma armadilha real: **uma rodada de fumaça de um caso pode gatear um merge.** O
`--compare` da aula de experiments tem guarda contra isso; o gate ainda não.

**E `refusal_pass_rate 0.00 >= 0.90 FAIL`** é o mais instrutivo. Aquele zero é o critério
errado que a aula anterior identificou — recusar *é* completar a tarefa. Ou seja: **o gate
está reprovando o build por causa de uma definição ruim, não de uma regressão.**

Que é exatamente o argumento para não ligar isso no CI antes de acertar os critérios.

### Comandos

```bash
python -m app.eval_gate --list-reports     # o que existe, agrupado por suíte
python -m app.eval_gate                    # usa o mais recente de cada
python -m app.eval_gate \
  --component-report data/eval_runs/component_<ts>.json \
  --agent-report data/eval_runs/agent_<ts>.json
```

Exit code `0` quando as obrigatórias passam, `1` quando qualquer métrica reprova, fica
`MISSING`, um relatório obrigatório falta, ou o `quality_gates.json` é inválido.

A sequência em CI seria esta — e cada linha é uma aula:

```bash
pytest                          # contrato, grátis
python -m app.eval_components   # onde a informação se perdeu
python -m app.eval_agent        # a trajetória do agente
python -m app.eval_gate         # a decisão
```

Não há workflow de GitHub Actions aqui. O comando é o que importa; onde ele roda é
detalhe de plataforma.

---

## Estrutura

```
app/
  config.py        # variáveis de ambiente
  ingest.py        # lê, valida e gera os chunks da knowledge_base
  index.py         # indexa no pgvector só o que mudou
  query_planner.py # planeja a busca e executa o retrieval filtrado
  rerank.py        # escolhe quais chunks recuperados entram no prompt final
  rag_pipeline.py  # o pipeline completo, compartilhado por terminal, API e evaluations
  observability.py # os spans de OpenTelemetry
  governance.py    # política de modelo e budget, e o ledger de uso estimado
  api.py           # a API HTTP (FastAPI)
  rag_chat.py      # o chat no terminal

  eval_dataset.py    # valida o dataset e sincroniza com o Langfuse
  eval_components.py # planner, retrieval e rerank, sem gerar resposta
  eval_ragas.py      # a resposta final contra o contexto usado
  eval_judge.py      # a resposta final contra uma rubrica escrita
  eval_experiment.py # duas variantes do pipeline, comparadas
  eval_runner.py     # roda o pipeline sobre o dataset, compartilhado pelas três acima

  support_tools.py   # as três ferramentas do Support Triage Agent
  support_agent.py   # o agente que escolhe entre elas
  eval_agent_dataset.py # valida o dataset de comportamento do agente e sincroniza
  eval_agent.py      # roda o agente contra o dataset e pontua a trajetória
  eval_gate.py       # lê os relatórios e decide se o build passa — sem chamar nada

evals/             # os datasets, as sondas de calibração e os thresholds do gate
knowledge_base/    # os documentos Markdown com front matter
tests/             # contrato da API, dataset e a matemática dos relatórios
data/
  current_usage.json    # dado fake do tenant, versionado (a ferramenta precisa dele)
  support_tickets.jsonl # tickets fake criados pela ferramenta (fora do git)
  eval_runs/            # relatórios das execuções (fora do git)
docs/pipeline.md   # como o RAG funciona por dentro
```
