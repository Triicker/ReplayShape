# Replay Shape

**Capture. Reviva. Compartilhe.** Plataforma de captura e venda automatizada de replays para
quadras, mesas de sinuca, arenas e academias. Um toque (QR Code na quadra ou botão físico) recorta
os **últimos 60 segundos** da câmera, o cliente paga via **Pix** e baixa/compartilha o MP4.
O arquivo é apagado automaticamente depois.

```
Câmera gravando → Botão → Últimos 60 s → MP4 → Pix → Confirmação → Download → Exclusão automática
```

> **Status (MVP):** o fluxo de ponta a ponta funciona localmente em **modo demonstração**:
> câmera simulada, buffer circular real com FFmpeg, recorte real, Pix simulado, download e
> expiração. As integrações reais (Intelbras, Mercado Pago, R2, ESP32) estão implementadas
> como adaptadores. Elas **não foram validadas** com equipamentos nem credenciais reais.
> Veja [docs/TEST_REPORT.md](docs/TEST_REPORT.md).

## Início rápido (modo demonstração, sem serviços externos)

Requisitos: Python 3.11+ e FFmpeg/FFprobe no PATH.

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m uvicorn replayshape.main:create_app --factory --app-dir backend --port 8000
```

- Público: http://localhost:8000. Escolha uma mesa, aguarde o buffer encher (~60 s na primeira
  vez) e toque em **GERAR MEU REPLAY**.
- Painel admin: http://localhost:8000/admin. A senha inicial é gerada em
  `data/ADMIN_INITIAL_PASSWORD.txt`. Troque-a em *Configurações*.
- API (OpenAPI/Swagger): http://localhost:8000/api/docs

Com Docker (o build não foi validado nesta sessão, ver relatório de testes):

```powershell
docker compose -f docker-compose.demo.yml up --build
```

Testes:

```powershell
.venv\Scripts\python -m pytest -q
```

## Versão estática navegável (Cloudflare Pages)

`dist/pages` é gerado por `python scripts/build_static_demo.py`. É a mesma interface, mas **sem
backend**: câmera, processamento e pagamento são simulados no navegador. O vídeo exibido é um
replay de exemplo gerado previamente pelo pipeline real (`scripts/make_sample_replay.py`).

Dependem do backend e **não** funcionam na versão estática: buffer e recorte reais, Pix/webhooks,
expiração e exclusão, painel admin, botões físicos e recuperação de pedido entre aparelhos.

## Estrutura

| Pasta | Conteúdo |
|---|---|
| `backend/replayshape/` | FastAPI: `api/` (público, admin, interno), `services/`, `edge/` (buffer circular), `video/` (FFmpeg), `integrations/` (câmeras, pagamentos, armazenamento) |
| `frontend/` | App público mobile-first (HTML/CSS/JS, sem framework) com tema de mesa de sinuca |
| `admin/` | Painel administrativo (dashboard, pedidos, câmeras, botões, configurações, simulador financeiro, auditoria) |
| `database/migrations/` | Migrações SQL versionadas |
| `firmware/esp32/` | Firmware inicial do botão físico |
| `scripts/` | Build da demo estática e geração do replay de exemplo |
| `tests/` | Pytest (unidade, integração, FFmpeg real) |
| `docs/` | Guias: API, Intelbras, Mercado Pago, ESP32, custos, relatório de testes |

Documentos: [ARCHITECTURE.md](ARCHITECTURE.md) · [SECURITY.md](SECURITY.md) ·
[DEPLOYMENT.md](DEPLOYMENT.md) · [ROADMAP.md](ROADMAP.md) · [CLAUDE.md](CLAUDE.md)
