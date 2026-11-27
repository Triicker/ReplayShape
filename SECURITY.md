# Segurança e privacidade

## Controles implementados

| Ameaça | Controle | Onde |
|---|---|---|
| IDOR (ver vídeo de outra pessoa) | Cada pedido exige um token secreto de 256 bits (guardado apenas como hash SHA-256). Sem token válido, a resposta é 404. Os links de mídia são tokens próprios de 15 min. | `api/deps.py::order_for_token`, `services/orders.py` |
| Galeria pública | Não existe. `/demo/spots` responde só em modo demo e lista apenas mesas, nunca vídeos. | `api/public.py` |
| Pagamento forjado | O status nunca é aceito do frontend. O webhook é verificado por HMAC e dispara uma **consulta independente** ao provedor, que é validada (valor, moeda, `external_reference`). O endpoint de simulação só existe com o provedor `mock`, e o provedor `mock` é **bloqueado em produção**. | `services/orders.py`, `config.py::validate` |
| Webhook repetido | Há idempotência por `event_key` (UNIQUE), e as transições ficam em transação serializada. | `payment_events` |
| SQL Injection | Todas as consultas são parametrizadas. Os nomes de colunas e tabelas são validados por *allowlist*. | `db.py` |
| XSS | `esc()` em toda interpolação no frontend/admin. CSP restrita (`script-src 'self'`, sem inline). | `frontend/assets/app.js`, `admin/admin.js`, `main.py` |
| CSRF | O cookie de sessão usa `HttpOnly` e `SameSite=Strict`, e toda mutação do admin exige o header `X-CSRF-Token`. | `api/deps.py::admin_session` |
| Força bruta | Rate limit no login (por IP+e-mail e por IP), com scrypt para senhas e tempo constante para e-mail inexistente. | `api/admin.py` |
| Abuso do botão | Rate limit por IP, cooldown por câmera (web) e por dispositivo (físico). | `api/public.py`, `api/internal.py` |
| SSRF | O público nunca informa URL ou IP. O admin só aceita hosts que resolvem para IP privado (`RS_ALLOW_PUBLIC_CAMERA_HOSTS=false`) e esquema `rtsp://`. | `integrations/cameras/base.py::check_host_allowed` |
| Command injection (FFmpeg) | `subprocess` com lista de argumentos (sem shell), caminhos gerados pelo sistema e textos de marca d'água via `textfile=`. Há teste com nome hostil. | `video/ffmpeg.py` |
| Vazamento de credenciais de câmera | Senhas e URLs RTSP ficam cifradas (Fernet) no banco, nunca são devolvidas pela API e saem como `***:***@` em logs e telas. | `security.py::SecretBox`, `edge/buffer.py` |
| Botão físico falsificado | HMAC-SHA256 com segredo por dispositivo, janela de ±120 s e cache de nonce contra replay. | `api/internal.py` |
| Fórmulas em CSV | Células iniciadas com `= + - @` recebem um prefixo `'`. | `services/metrics.py` |
| Clickjacking / sniffing | `X-Frame-Options: DENY`, `frame-ancestors 'none'`, `nosniff`, `Referrer-Policy: no-referrer`, HSTS quando `RS_COOKIE_SECURE=true`. | `main.py` |
| Configuração insegura em produção | O app **não inicia** em produção com demo, `mock`, sem `RS_SECRET_KEY` ou sem cookie seguro. | `config.py::validate` |
| Auditoria | `audit_logs` registra login, alterações, pagamentos, estornos, exclusões e reinícios, sem segredos. | `services/audit.py` |

## Regras operacionais

- **Nunca exponha câmeras, DVRs ou NVRs à internet.** O Edge abre conexões RTSP apenas na rede
  local. Para acesso externo, publique só a aplicação, via proxy HTTPS ou túnel.
- Use HTTPS em produção, com `RS_COOKIE_SECURE=true` e o certificado raiz no firmware do ESP32.
- Guarde `.env`, `data/secret.key`, `data/ADMIN_INITIAL_PASSWORD.txt` e `firmware/.../config.h`
  fora do Git, como já está no `.gitignore`.
- Troque a senha inicial do admin e apague `ADMIN_INITIAL_PASSWORD.txt`. A troca pelo painel já
  apaga o arquivo.
- Monitore dependências com `pip list --outdated` e o Dependabot do GitHub.

## LGPD e direito de imagem

O que existe no sistema:

- O aviso de gravação por estabelecimento aparece na tela da mesa.
- A página `/privacidade` informa finalidade, retenção e acesso, e tem um formulário de solicitação
  (exclusão ou informação) que fica registrado em `privacy_requests` e visível em *Auditoria*.
- A retenção é mínima e configurável: buffer de ~120 s, replay não pago em 30 min e pago em 24 h.
  Tudo é apagado automaticamente e registrado.
- Não há cadastro nem coleta de dados pessoais do comprador. O IP é guardado só como hash com sal.

**Pontos que dependem de validação jurídica** (responsável pelo produto/estabelecimento):

1. A base legal da gravação contínua e do tratamento de imagem para venda de replays.
2. O texto final do aviso de gravação e da política de privacidade. O texto atual é um **modelo**.
3. O tratamento de **menores de idade**: autorização dos responsáveis, restrição de horários e
   ambientes, ou desativação de câmeras em aulas infantis.
4. Restrição de gravação em ambientes inadequados, como vestiários e banheiros. Esses locais
   **não devem ter câmera cadastrada**.
5. A situação de terceiros que aparecem no replay de outra pessoa, e o prazo e a forma de resposta
   às solicitações de exclusão.
6. O contrato com o estabelecimento: controlador ou operador, repasse e responsabilidades.

## Reportar vulnerabilidades

Abra uma *issue* privada (Security Advisory) no GitHub do projeto.
