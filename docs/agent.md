# O Support Triage Agent

Um mini-projeto dentro do repositório, para estudar evaluation de tool calling.
Tudo acima dele mede uma **resposta**; aqui o objeto é a **trajetória** — quais
ferramentas o modelo escolheu, em que ordem, com quais argumentos.

As camadas que avaliam o Knowledge Chat estão em [evaluation.md](evaluation.md).

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

Roda os 20 casos contra o agente e pontua **comportamento**, não resposta.

### De onde vem a trajetória

Antes dos scores, a fonte do dado. A lista de ferramentas chamadas sai do
`AIMessage.tool_calls` que o framework registra:

```python
return [
    AgentToolCall(name=call["name"], arguments=call.get("args") or {})
    for message in messages
    for call in getattr(message, "tool_calls", None) or []
]
```

**Nunca do texto que o modelo escreveu sobre si mesmo.** Se você perguntasse "quais
ferramentas você usou?" e confiasse na resposta, estaria avaliando a narração — e modelo
erra ao narrar o que fez, justamente nos casos em que você mais precisa saber.

### Os nove scores

Todos determinísticos. Sem Ragas e sem judge, de propósito: o objeto medido já é bastante
não-determinístico, e um medidor com variação própria tornaria duas rodadas incomparáveis.

| Score | O que cobra | Aplica quando |
| --- | --- | --- |
| `agent_tool_selection` | as ferramentas esperadas foram chamadas | sempre |
| `agent_forbidden_tools` | as proibidas não foram | sempre |
| `agent_argument_match` | argumentos mínimos, por **subset** | há `expected_arguments` |
| `agent_trajectory_match` | a sequência | sempre |
| `agent_max_steps` | não passou do teto de chamadas | sempre |
| `agent_clarification` | perguntou de volta quando devia | sempre |
| `agent_refusal` | recusou sem chamar nada | `should_refuse=true` |
| `agent_ticket_creation` | abriu o chamado, e o id veio da ferramenta | sempre |
| `agent_expected_terms` | a resposta contém os termos esperados | há `expected_terms` |

Três merecem detalhe, porque são os que não existiriam numa avaliação de resposta.

**`forbidden_tools` — chamar demais é erro.** Nenhuma avaliação de resposta tem esse
conceito: se o texto final está certo, tanto faz quantas buscas foram feitas. Para um
agente não tanto faz. Cada chamada custa tempo e token, e uma delas escreve em disco. O
threshold desse score no quality gate é **1.00**, o único sem tolerância: ou aconteceu, ou
não.

**`argument_match` — subset, não igualdade.** O dataset fixa o que importa e deixa o resto
livre:

```json
"expected_arguments": {"create_support_ticket": {"severity": "P2"}}
```

A chamada real traz `severity`, `summary` e `tenant_id`. Exigir o objeto inteiro obrigaria
o dataset a ditar o texto do resumo — que é justamente a parte que o agente deve escrever
sozinho. Cobra-se a decisão, não a redação.

**`ticket_creation` — o cruzamento.** Exige três coisas juntas: `ticket_created=true`, um
`ticket_id` não vazio, **e** `create_support_ticket` na trajetória.

As duas primeiras vêm do modelo; a terceira vem de fora dele. Um modelo dizendo "abri o
chamado TCK-12345" sem nunca ter chamado a ferramenta passa nas duas e falha na terceira.
É por isso que `ticket_created` e a trajetória são capturas **separadas** — se uma fosse
derivada da outra, não haveria o que cruzar.

### Os três modos de comparar a sequência

O `trajectory_match_type` de cada caso decide como a lista real é comparada:

| Modo | Passa quando | Para quê |
| --- | --- | --- |
| `exact` | a sequência é idêntica | caso de uma ferramenta só, ou de nenhuma |
| `in_order` | as esperadas aparecem na ordem, extras tolerados | caso encadeado |
| `any_order` | todas aparecem, ordem livre | quando a ordem não importa |

O `in_order` é implementado em duas linhas, e vale entender:

```python
remaining = iter(actual)
return all(name in remaining for name in expected)
```

`in` sobre um **iterador** consome ele. Então cada ferramenta esperada só casa depois da
anterior — que é exatamente o significado de "mesma ordem, extras permitidos".

Isso importa no caso encadeado: *"estamos perto do limite? se acima de 85%, abra um P2"*
espera `get_current_usage` e depois `create_support_ticket`. Se o agente abrisse o chamado
**antes** de consultar, teria acertado as duas ferramentas e errado o raciocínio — a
segunda chamada só se justifica pelo resultado da primeira. Só `in_order` pega isso.

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
