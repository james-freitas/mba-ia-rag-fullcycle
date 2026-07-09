---
title: Política de Faturamento
tenant: fcai
product: fcai-cloud
plan: all
doc_type: policy
version: 2026-01
status: published
visibility: internal
---

# Política de Faturamento

Este documento descreve as formas de pagamento do **FCAI Cloud**, os ciclos de
cobrança, a cobrança por consumo excedente, a emissão de notas fiscais, o tratamento de
falhas de cobrança, o processo de regularização de acesso e as regras de reembolso.
Todos os valores e prazos são fictícios e servem como exemplo.

## Formas de Pagamento

### Cartão de Crédito

O plano **Pro** é cobrado por cartão de crédito de forma recorrente, mensal ou anual. A
confirmação da cobrança é imediata, e o acesso permanece ativo enquanto o pagamento for
aprovado. O cliente pode atualizar os dados do cartão a qualquer momento no painel de
faturamento.

### Boleto Bancário

Empresas podem optar por **boleto** em contratações anuais. Regras principais:

- A compensação do boleto pode levar até **3 dias úteis**.
- A emissão de um novo ciclo depende da confirmação do pagamento anterior.
- Boletos vencidos precisam ser reemitidos no portal de faturamento.

### Pagamento Corporativo (Enterprise)

No plano **Enterprise**, o pagamento é feito de forma corporativa, com faturamento por
contrato. As condições incluem:

- Emissão de nota fiscal conforme o acordo comercial.
- Prazos de pagamento negociados (por exemplo, 15, 30 ou 45 dias).
- Possibilidade de faturamento por centro de custo ou por unidade de negócio.

## Ciclos de Cobrança

- **Mensal:** a cobrança ocorre na data de aniversário da assinatura, a cada mês.
- **Anual:** a cobrança ocorre uma vez a cada 12 meses, com desconto em relação ao
  mensal.
- **Enterprise:** o ciclo é definido em contrato, podendo ser mensal, trimestral ou
  anual.

A data de início do ciclo é a data de ativação do plano pago. Alterações de plano
podem gerar cobrança proporcional (pro rata), conforme a política comercial.

## Cobrança por Consumo Excedente

Nos planos **Pro** e **Enterprise**, o volume de ingestão que ultrapassar o limite do
plano é cobrado como **consumo excedente**:

- O excedente é medido por **GB** de ingestão, no fechamento de cada ciclo.
- O painel de uso mostra o consumo atual e projeta o excedente estimado do ciclo.
- Notificações são enviadas ao atingir **80%** e **100%** da cota do plano.
- No **Enterprise**, as regras e os valores de excedente seguem o contrato.

O consumo excedente já faturado não é reembolsável.

## Impostos e Notas Fiscais

- Os preços divulgados podem não incluir impostos, que são aplicados conforme a
  legislação vigente e o endereço de cobrança.
- A **nota fiscal** é emitida a cada cobrança e disponibilizada no portal de
  faturamento.
- Clientes corporativos podem configurar dados fiscais específicos, como razão social,
  CNPJ e endereço de faturamento.

## Falha de Cobrança

Em caso de falha na cobrança — por recusa do cartão, boleto não pago ou fatura
corporativa em atraso — a FCAI adota o seguinte fluxo fictício:

1. **Notificação:** o responsável financeiro é notificado por e-mail sobre a falha.
2. **Reprocessamento:** cobranças por cartão são reprocessadas automaticamente por até
   **3 tentativas**, em intervalos de alguns dias.
3. **Período de tolerância:** a conta permanece ativa durante um período de tolerância
   para regularização.
4. **Restrição de acesso:** persistindo a inadimplência, a conta entra em modo restrito.

## Modo Restrito e Suspensão

Durante a pendência financeira, a conta pode entrar em **modo restrito**, com:

- Ingestão de novos dados **suspensa**.
- Acesso **somente leitura** aos dados ainda retidos.
- Alertas e integrações pausados.

Se a pendência não for resolvida dentro do prazo, a conta pode ser **suspensa** e, após
prazo adicional, os dados podem ser removidos conforme a política de retenção.

## Regularização de Acesso

Após a confirmação do pagamento pendente:

- O acesso é restabelecido **automaticamente**, sem necessidade de nova contratação.
- As configurações, dashboards e integrações são preservados, desde que a conta não
  tenha sido removida.
- A ingestão de dados é retomada a partir do momento da regularização; dados do período
  de suspensão podem não ser recuperados.

## Reembolso e Créditos

- O reembolso do plano **Pro** segue a política comercial: até **7 dias corridos** após
  a contratação.
- **Créditos de SLA** (Enterprise) são aplicados na fatura seguinte e não são
  convertidos em dinheiro (ver documento de SLA de suporte).
- Ajustes de cobrança por erro de faturamento são corrigidos na próxima fatura ou por
  nota de crédito, conforme o caso.

## Contato do Financeiro

Dúvidas sobre faturas, notas fiscais, formas de pagamento e regularização devem ser
enviadas para financeiro@fcai.example.com, em horário comercial.
