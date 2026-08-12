# Quality gates

As evaluations ([evaluation.md](evaluation.md), [agent.md](agent.md)) produzem
números. Este comando transforma os números numa decisão de sim ou não.

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
