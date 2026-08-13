---
title: Plano Pro — Política de Retenção de Dados
tenant: fcai
product: fcai-cloud
plan: pro
doc_type: plan
version: 2026-01
status: published
visibility: internal
---

# Plano Pro — Política de Retenção de Dados

Este documento descreve a política de retenção de dados aplicada às contas do plano
**Pro** do FCAI Cloud, para métricas, logs e traces.

## Período de Retenção

A retenção de dados do plano **Pro** é de **24 meses** para métricas, logs e traces.
O período é contado a partir da ingestão de cada registro e vale igualmente para os três
tipos de telemetria.

| Tipo de dado | Retenção no Pro |
| --- | --- |
| Métricas | 24 meses |
| Logs | 24 meses |
| Traces | 24 meses |

## Arquivamento

Após o período de retenção, os dados são movidos para arquivamento frio por mais 6 meses
antes da exclusão definitiva. O arquivamento é consultável pela API de exportação.

## Exclusão

O cliente pode solicitar a exclusão antecipada de dados a qualquer momento pelo portal.
A exclusão é irreversível e concluída em até 7 dias.

## Perguntas Frequentes

**Por quanto tempo o plano Pro retém os dados?**
24 meses para métricas, logs e traces.
