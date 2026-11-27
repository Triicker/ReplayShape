# API REST (v1)

A documentação interativa (OpenAPI/Swagger) fica em **`/api/docs`**, e o JSON em `/api/openapi.json`.
Todas as respostas de erro seguem o formato `{"error": {"code": "...", "message": "..."}}`.

## Públicos — `/api/v1/public` (sem cadastro)

| Método | Rota | Auth | Descrição |
|---|---|---|---|
| GET | `/config` | — | Modo demo / pagamento simulado |
| GET | `/demo/spots` | — | Lista as mesas (somente em modo demo) |
| GET | `/spots/{code}` | — | Dados da quadra/mesa do QR: preço, duração, status da câmera |
| POST | `/spots/{code}/replays` | — | **Aciona o replay**. Devolve `order_id`, `access_token` (guarde!) e `short_code` |
| POST | `/spots/{code}/claim` | — | Reivindica o último acionamento do botão físico (ou o pedido de `{"code"}`) |
| POST | `/orders/recover` | — | `{"code": "K7P3-Q9XM"}` → novo token de acesso |
| GET | `/orders/{id}` | `X-Order-Token` | Status do replay, do pedido e do Pix |
| POST | `/orders/{id}/payment` | `X-Order-Token` | Cria (ou reaproveita) a cobrança Pix |
| POST | `/orders/{id}/demo-payment` | `X-Order-Token` | **Somente demo**: `{"result": "approved" \| "rejected"}` |
| POST | `/orders/{id}/media-link` | `X-Order-Token` | Link de 15 min (somente pedido pago) |
| GET | `/media/{token}/{nome}.mp4[?download=1]` | token no caminho | Vídeo (suporta `Range`) |
| POST | `/privacy-requests` | — | Solicitação LGPD |

## Webhooks — `/api/v1/webhooks/{provider}`
`mercadopago` (assinatura `x-signature`) ou `mock` (assinatura `x-mock-signature`). O status é
sempre reconfirmado com o provedor.

## Internos (botões físicos) — `/api/v1/internal/devices/{device_id}`
| Método | Rota | Descrição |
|---|---|---|
| POST | `/trigger` | Aciona o replay da câmera associada. Resposta `{"status": "accepted" \| "debounced", "short_code"}` |
| POST | `/heartbeat` | Sinal de vida (header opcional `X-RS-Firmware`) |

Headers obrigatórios: `X-RS-Timestamp` (epoch s, ±120 s), `X-RS-Nonce` (8–64 caracteres, uso
único) e `X-RS-Signature` = `hex(HMAC_SHA256(secret, ts\nnonce\nPOST\npath\nsha256(body)))`.

## Administrativos — `/api/v1/admin` (cookie de sessão + `X-CSRF-Token` em mutações)
`auth/login`, `auth/logout`, `me`, `me/password`; CRUD de `establishments`, `cameras`
(+ `/{id}/test`, `/{id}/qr.svg`, `/{id}/trigger`) e `devices` (+ `/{id}/rotate-secret`); `orders`
(filtros `start`, `end`, `establishment_id`, `camera_id`, `status`), `orders/export.csv`,
`orders/{id}/refund`; `dashboard`; `settings` (GET/PATCH); `audit`; `privacy-requests`; `buffers`.

## Saúde
`GET /api/v1/health` → `{"status": "ok", "cameras_online", "disk_low", ...}`
