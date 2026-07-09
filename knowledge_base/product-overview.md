---
title: Visão Geral do Produto
tenant: fcai
product: fcai-cloud
plan: all
doc_type: product
version: 2026-01
status: published
visibility: internal
---

# Visão Geral do Produto

O **FCAI Cloud** é uma plataforma de observabilidade que unifica métricas, logs e
traces em um único painel. Este documento descreve a arquitetura em alto nível, os
recursos por área, os planos disponíveis, os limites de cada plano, as integrações
suportadas e os principais casos de uso. Os números e valores citados são fictícios
e servem apenas como exemplo para a base de conhecimento.

## Arquitetura em Alto Nível

O FCAI Cloud é composto por três camadas principais:

1. **Coleta:** agentes e SDKs compatíveis com OpenTelemetry enviam dados de métricas,
   logs e traces para os endpoints de ingestão da plataforma.
2. **Processamento e Armazenamento:** os dados recebidos são normalizados, indexados e
   armazenados em backends otimizados para séries temporais, busca de texto e
   correlação de traces.
3. **Visualização e Alertas:** dashboards, exploradores de dados e um motor de alertas
   permitem consultar, correlacionar e reagir aos dados em tempo real.

A ingestão é feita por região, e o cliente escolhe a região de armazenamento no momento
da criação da organização. A retenção de dados varia conforme o plano contratado.

## Recursos por Área

### Métricas

- Coleta de métricas de infraestrutura (CPU, memória, disco, rede) e de aplicação.
- Suporte a métricas customizadas via SDK e via protocolo compatível com OpenTelemetry.
- Agregações por tags e dimensões, com granularidade configurável.
- Visualização em gráficos de linha, área, barras e mapas de calor.

### Logs

- Centralização de logs de múltiplas fontes e serviços.
- Busca de texto completo com filtros por atributos estruturados.
- Parsing e enriquecimento de logs por regras configuráveis.
- Retenção configurável conforme o plano, com arquivamento opcional.

### Tracing Distribuído

- Rastreamento de requisições que atravessam múltiplos serviços.
- Visualização de spans em linha do tempo, com identificação de gargalos.
- Correlação automática entre traces, logs e métricas do mesmo contexto.
- Amostragem configurável para controlar o volume de dados.

### Alertas

- Regras de alerta baseadas em limiares, ausência de dados e detecção de anomalias.
- Notificação por e-mail, Slack, Microsoft Teams, PagerDuty e webhook genérico.
- Políticas de silenciamento e agrupamento para reduzir ruído.
- Histórico de disparos e status de cada alerta.

### Dashboards

- Painéis customizáveis com widgets de métricas, logs e traces.
- Compartilhamento interno e links de leitura para stakeholders.
- Modelos (templates) de dashboard para cenários comuns.
- Variáveis de dashboard para filtrar por ambiente, serviço ou região.

## Planos

O FCAI Cloud é oferecido em três planos. As diferenças principais estão em número de
usuários, retenção de dados, volume de ingestão e nível de suporte.

### Starter

Plano gratuito voltado para times pequenos e para avaliação da plataforma.

- 3 usuários incluídos.
- Retenção de dados por 7 dias.
- Até 5 GB de ingestão de logs por mês.
- Até 100 séries temporais de métricas ativas.
- Suporte via comunidade e base de conhecimento, sem SLA garantido.

### Pro

Plano pago para times em crescimento — R$ 499/mês (valor fictício).

- Até 20 usuários.
- Retenção de dados por 30 dias.
- Até 100 GB de ingestão de logs por mês.
- Até 5.000 séries temporais de métricas ativas.
- Tracing com amostragem configurável.
- Suporte técnico com SLA padrão em horário comercial.

### Enterprise

Plano para empresas com necessidades avançadas — valores sob consulta comercial.

- Usuários ilimitados.
- Retenção de dados configurável, de até 15 meses.
- Volume de ingestão negociado por contrato.
- Séries temporais de métricas conforme contrato.
- Suporte 24x7 com SLA reduzido e gerente de conta dedicado.
- Recursos avançados de segurança: SSO, RBAC e trilhas de auditoria.

## Limites de Ingestão e Cotas

Cada plano possui um limite mensal de ingestão de logs e métricas, além de cotas de
séries temporais ativas. O comportamento ao atingir o limite varia por plano:

- **Starter:** ao atingir o limite, a ingestão é interrompida até o próximo ciclo.
- **Pro:** permite ingestão adicional, cobrada como consumo excedente por GB.
- **Enterprise:** o volume é negociado por contrato, com regras de excedente próprias.

Os limites são medidos por ciclo de faturamento e podem ser acompanhados no painel de
uso da organização. Notificações são enviadas ao atingir 80% e 100% da cota.

## Integrações

O FCAI Cloud integra com agentes de coleta compatíveis com **OpenTelemetry** e oferece
conectores nativos para os principais ambientes:

- **Kubernetes:** coleta de métricas de cluster, pods e nós, além de logs de containers.
- **Docker:** coleta de métricas e logs de containers em hosts individuais.
- **Provedores de nuvem:** conectores para métricas de serviços gerenciados populares.
- **Ferramentas de incidentes:** integração com PagerDuty e canais de mensageria.

SDKs oficiais estão disponíveis para as linguagens mais usadas em backend, permitindo
instrumentação manual quando necessário.

## Casos de Uso

- **Monitoramento de produção:** acompanhar a saúde de serviços críticos em tempo real.
- **Resposta a incidentes:** correlacionar métricas, logs e traces durante uma falha.
- **Análise de performance:** identificar gargalos em requisições distribuídas.
- **Capacidade e custo:** acompanhar tendências de uso para planejar infraestrutura.
- **Conformidade:** reter logs pelo período exigido por políticas internas.

## Roadmap (Informativo)

O roadmap fictício da plataforma inclui evoluções em detecção de anomalias, dashboards
colaborativos e relatórios de conformidade. As datas e prioridades são definidas
internamente e podem mudar sem aviso, não constituindo compromisso contratual.
