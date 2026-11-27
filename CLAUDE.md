# CLAUDE.md — Replay Shape

## Contexto
Plataforma de captura e venda de replays esportivos. O cliente toca em "GERAR MEU REPLAY" (QR da
quadra/mesa ou botão ESP32). O sistema recorta os **últimos 60 s** do buffer da câmera, cobra via
Pix e libera o MP4 por 24 h. Depois o arquivo é apagado. O MVP roda tudo em um processo (Edge +
Cloud). O modo demo usa uma câmera sintética (mesa de sinuca com relógio gravado) e um Pix
simulado. A identidade visual é de mesa de sinuca: grafite, feltro verde, madeira e verde-limão
`#a3e635`.

## Arquitetura (resumo; detalhes em ARCHITECTURE.md)
- `backend/replayshape/main.py`: app factory, headers de segurança, rotas estáticas.
- `api/public.py` (comprador, token por pedido), `api/admin.py` (sessão+CSRF+RBAC), `api/internal.py` (webhooks, ESP32 HMAC).
- `services/replays.py`: acionamento e recorte. `services/orders.py`: Pix, webhook, conciliação, estorno, links. `services/cleanup.py`: retenção e recuperação no startup.
- `edge/buffer.py`: buffer circular (FFmpeg segment + CSV → horário de parede). `edge/manager.py`: um buffer por câmera ativa.
- `integrations/{cameras,payments,storage}`: adaptadores (demo/rtsp/intelbras, mock/mercadopago, s3).
- `database/migrations/*.sql`: esquema. Uma migração nova = um arquivo novo numerado. **Nunca edite migração já aplicada.**

## Comandos
```powershell
python -m venv .venv; .venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m uvicorn replayshape.main:create_app --factory --app-dir backend --port 8000
.venv\Scripts\python -m pytest -q                 # suíte completa (~1-2 min, usa FFmpeg real)
.venv\Scripts\python -m pytest -q -m "not slow"   # sem o teste de buffer em tempo real
.venv\Scripts\python scripts\make_sample_replay.py   # replay de exemplo (pipeline real)
.venv\Scripts\python scripts\build_static_demo.py    # dist/pages (Cloudflare Pages)
```

## Convenções
- Python 3.11+, `from __future__ import annotations`, sem ORM: `db.one/all/insert/update` com parâmetros `?`.
- Horários em epoch UTC (float). IDs `uuid4().hex`. Erros públicos `{"error": {"code", "message"}}` com mensagens em pt-BR.
- Frontend/admin em JS puro (módulos ES). **Todo texto dinâmico passa por `esc()`**. Sem scripts nem `style=` inline (a CSP bloqueia). Estilos por classes.
- Toda transição de pedido/pagamento acontece dentro de `ctx.db.tx()`.
- Configurações operacionais em `services/settings_store.py` (DEFAULTS + BOUNDS). Segredos só em env.
- Commits por funcionalidade. Branch de trabalho, nunca commit direto na `main`.

## Restrições e regras de segurança (não negociáveis)
- O intervalo do vídeo vem de `trigger_at` (acionamento), **nunca** do pagamento.
- Não permitir cobrança se o replay não foi gerado (`awaiting_payment` só após validação).
- O status de pagamento nunca vem do frontend. O webhook dispara a consulta ao provedor e a validação de valor, moeda e referência.
- Nenhuma listagem pública de vídeos. Acesso só com token do pedido ou link temporário.
- O público nunca informa URL ou IP. Câmeras só em IP privado (SSRF). FFmpeg sempre com lista de args e `textfile=`.
- Não logar senhas, tokens ou URLs RTSP com credenciais (use `redacted_url`).
- O provedor `mock` e o modo demo são bloqueados em produção (`config.validate`).
- Não declarar compatibilidade com Intelbras, Mercado Pago, iOS ou ESP32 sem teste real. Registrar evidências em `docs/TEST_REPORT.md`.

## Orientações para mudanças futuras
- Novo fabricante de câmera: subclasse de `CameraAdapter` em `integrations/cameras/base.py` e registro em `ADAPTERS`.
- Novo provedor de pagamento: implementar `PaymentProvider` (create_charge/get_payment/parse_notification/refund/cancel) e registrar em `get_provider`.
- Separar Edge/Cloud: transformar `ctx.executor.submit(process_replay)` em fila e mover `edge/` + `video/` para um agente com conexão de saída.
- PostgreSQL: portar `001_initial.sql` (REAL→DOUBLE PRECISION, AUTOINCREMENT→IDENTITY) e trocar `db.py`.
- Ao mudar o fluxo, rodar `pytest` e testar no navegador em largura mobile (375 px).
