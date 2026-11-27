# Guia de configuração — Mercado Pago (Pix)

> Adaptador implementado em `backend/replayshape/integrations/payments/mercadopago.py`, testado
> apenas contra **respostas simuladas** (httpx MockTransport). **Não foi validado com credenciais
> reais nem em sandbox.**

## O que foi conferido na documentação oficial (nov/2026)
- Pix pela API de Pagamentos: `POST https://api.mercadopago.com/v1/payments` com
  `payment_method_id: "pix"`, `transaction_amount`, `payer.email` e o header **`X-Idempotency-Key`**.
  `date_of_expiration` é opcional e vai de 30 min a 30 dias (padrão 24 h).
- A resposta traz `point_of_interaction.transaction_data.qr_code` (copia e cola),
  `qr_code_base64` e `ticket_url`.
- Reembolso Pix: `POST /v1/payments/{id}/refunds` com `X-Idempotency-Key`. O header
  `X-Render-In-Process-Refunds: true` evita 400 em reembolsos em processamento.
- Webhooks: header `x-signature` (`ts=...,v1=...`), `x-request-id` e `data.id` na query. Responder 200/201.
- A documentação indica que a **API de Orders** é a recomendada para novas integrações. Avalie a
  migração antes de ir para produção.

## Pendências de validação (fazer em sandbox)
1. **Template da assinatura do webhook.** O código usa
   `id:{data.id};request-id:{x-request-id};ts:{ts};` com HMAC-SHA256 e a chave secreta do painel
   (padrão publicado anteriormente pelo MP). Esse trecho não foi lido na página oficial durante o
   desenvolvimento. Confirme no painel ou no SDK oficial.
2. A documentação informa que **notificações de "Código QR" não podem ser verificadas pela
   assinatura**. Configure o webhook para o tópico **Pagamentos**.
3. Campos `external_reference` e `notification_url` na criação.
4. Formato de `date_of_expiration`: o código envia ISO-8601 com milissegundos e fuso `-03:00`.
5. A taxa real do Pix no seu contrato. O painel usa 0,99% como valor **hipotético** até o provedor
   informar `fee_details`.

## Passo a passo
1. Crie uma aplicação no painel de desenvolvedores do Mercado Pago e copie o **Access Token de teste**.
2. Em *Webhooks*, configure a URL `https://SEU-DOMINIO/api/v1/webhooks/mercadopago`, marque o
   evento **Pagamentos** e revele a **assinatura secreta**.
3. No `.env`:
   ```
   RS_PAYMENT_PROVIDER=mercadopago
   RS_MP_ACCESS_TOKEN=TEST-...
   RS_MP_WEBHOOK_SECRET=...
   RS_MP_PAYER_EMAIL=email-generico@seudominio.com
   RS_PUBLIC_BASE_URL=https://SEU-DOMINIO
   ```
   O webhook exige HTTPS público. Para testar a partir de um PC, use um túnel.
4. Gere um replay, pague com um usuário de teste e confira em *Pedidos* (status `paid`) e em
   *Auditoria* (`payment.approved`).

## Critérios de aceite (sandbox)
- [ ] Pix criado com QR e copia e cola exibidos no app
- [ ] Webhook com assinatura **válida** aceito; assinatura inválida → 401
- [ ] Pedido liberado **somente** depois da consulta `GET /v1/payments/{id}` retornar `approved` com valor e referência corretos
- [ ] Webhook duplicado → `duplicate`, sem efeito
- [ ] Pix expirado → pedido `expired`; pagamento tardio dentro da tolerância → liberado
- [ ] Estorno pelo painel → pedido `refunded`, link revogado
- [ ] Nenhuma cobrança real durante os testes
