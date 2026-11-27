# Arquitetura

## Visão geral

```mermaid
flowchart LR
  subgraph Local["Rede do estabelecimento"]
    CAM1[Câmera IP / NVR Intelbras] -- RTSP (leitura) --> EDGE
    CAM2[Câmera simulada<br/>modo demo] --> EDGE
    BTN[Botão ESP32] -- HTTPS + HMAC --> API
    subgraph EDGE["Replay Shape Edge"]
      BUF[Buffer circular<br/>segmentos .ts de 2 s<br/>~120 s por câmera]
      CUT[Recorte FFmpeg<br/>H.264 + faststart]
      LOC[(Disco local privado)]
      BUF --> CUT --> LOC
    end
  end
  subgraph Cloud["Replay Shape Cloud"]
    API[FastAPI REST /api/v1]
    DB[(SQLite → PostgreSQL)]
    ADM[Painel admin]
    WEB[App público]
    API --- DB
  end
  USER[Celular do cliente] -- QR Code --> WEB --> API
  API -- pedido --> CUT
  API <-- webhook + consulta --> MP[Mercado Pago Pix]
  LOC -- após pagamento (opcional) --> R2[(Cloudflare R2 / S3)]
  API -- link temporário --> USER
```

No MVP, **Edge e Cloud rodam no mesmo processo** (um computador, Cenário A). As fronteiras
existem no código e permitem separar os dois depois (Cenário B/C):

- `edge/` cuida do buffer por câmera, do recorte e do disco local. Não conhece pagamentos.
- `services/orders.py` cuida de pedidos, Pix, webhooks e conciliação. Não conhece FFmpeg.
- `services/replays.py` faz a ponte entre os dois. Ao separar, ela vira uma fila: a Cloud publica a
  solicitação e o Edge busca o trabalho por conexão de **saída**. Assim, nenhuma câmera fica
  exposta à internet.

## Fluxo principal

```mermaid
sequenceDiagram
  autonumber
  participant U as Cliente
  participant W as App (QR)
  participant A as API
  participant E as Edge (buffer)
  participant P as Provedor Pix
  U->>W: abre /m/{código da mesa}
  U->>W: GERAR MEU REPLAY
  W->>A: POST /spots/{código}/replays
  A->>A: trigger_at = agora (define o intervalo)
  A-->>W: order_id + token secreto
  A->>E: recortar [trigger_at-60, trigger_at]
  E->>E: aguarda segmento final, protege (lock) segmentos
  E->>E: concat + corte preciso + H.264 + validação ffprobe
  E-->>A: arquivo privado pronto → pedido awaiting_payment
  W->>A: POST /orders/{id}/payment
  A->>P: cria cobrança Pix (idempotente)
  P-->>U: paga com app do banco
  P->>A: webhook assinado
  A->>P: GET pagamento (confirmação independente)
  A->>A: valida valor/moeda/referência → pedido paid (24 h)
  W->>A: POST /orders/{id}/media-link
  A-->>W: link de 15 min (renovável)
  U->>A: GET /media/{token}/replay.mp4 (Range)
  Note over A: rotina de limpeza apaga arquivos vencidos
```

## Estados (modelados separadamente)

| Entidade | Estados |
|---|---|
| Captura (`replay_requests`) | `queued → capturing → processing → ready \| failed` |
| Pedido (`orders`) | `processing → awaiting_payment → paid → closed`; `failed`, `expired`, `paid_unfulfillable`, `refund_pending`, `refunded` |
| Pagamento (`payments`) | `pending → approved \| rejected \| cancelled \| expired`; `refunded`, `charged_back` |
| Arquivo (`replay_files`) | `private → available → deleted`; `missing`; remoto `none/pending/uploaded/failed/deleted` |

Regras de consistência:

- Só é possível criar cobrança com o pedido em `awaiting_payment`, ou seja, com o arquivo gerado e
  validado. **Falha na geração nunca gera cobrança.**
- As transições de pagamento acontecem em transações `BEGIN IMMEDIATE`, o que serializa webhooks
  concorrentes.
- Pagamento aprovado com o pedido já pago é tratado como duplicado e estornado.
- Pagamento aprovado depois de o arquivo sumir leva a `paid_unfulfillable` e ao estorno.
- Pagamento aprovado depois do prazo, com o arquivo ainda na tolerância, libera normalmente.

## Modelo de dados

```mermaid
erDiagram
  establishments ||--o{ cameras : possui
  cameras ||--o{ devices : "botões"
  cameras ||--|| camera_buffers : status
  cameras ||--o{ replay_requests : gera
  replay_requests ||--|| replay_files : arquivo
  replay_requests ||--|| orders : pedido
  orders ||--o{ payments : cobranças
  payments ||--o{ payment_events : webhooks
  orders ||--o{ download_tokens : links
  admin_users ||--o{ admin_sessions : sessões
  establishments ||--o{ admin_users : "operadores (RBAC)"
```

Também há as tabelas `audit_logs`, `system_settings`, `privacy_requests` e `mock_pix_charges`
(esta última é o provedor simulado). Esquema completo em `database/migrations/001_initial.sql`.
Os campos de data são epoch UTC. O SQL é portável para PostgreSQL com ajustes mínimos de tipos.

## Buffer circular (Estratégia A)

- Um processo FFmpeg por câmera grava segmentos MPEG-TS de 2 s. A lista CSV do muxer `segment`
  informa o início e o fim de cada segmento no tempo do stream.
- **Conversão para horário de parede**: `âncora = min(observado − fim_relativo)` numa janela
  deslizante de 30 segmentos. Como o registro só aparece depois que o segmento termina, o mínimo
  converge para o menor atraso real. A precisão medida nos testes está em
  [docs/TEST_REPORT.md](docs/TEST_REPORT.md).
- Segmentos com mais de `buffer_seconds` são apagados. Os segmentos em uso por um recorte ficam com
  *lock* e não são apagados.
- Um *watchdog* reinicia o FFmpeg quando ele morre ou para de produzir segmentos, com *backoff*
  exponencial de até 30 s. O status da câmera é atualizado (`online/offline/error`).
- Câmeras RTSP reais usam `-c copy`, sem recodificar, o que custa pouca CPU. O recorte final
  recodifica para H.264 e garante corte preciso e compatibilidade.

## Estratégia B (gravação histórica no NVR)

`IntelbrasAdapter.playback_url()` gera a URL `/cam/playback?...starttime=...&endtime=...`, mas
ela só é usada se o administrador marcar **"Playback histórico validado"** na câmera depois de
testar no equipamento. A compatibilidade **não** é presumida. Veja [docs/INTELBRAS.md](docs/INTELBRAS.md).

## Decisões técnicas

| Decisão | Motivo |
|---|---|
| FastAPI + SQLite + FFmpeg, sem ORM | Menos dependências. As consultas são parametrizadas, e as migrações são SQL puro. |
| Frontend sem framework | Carrega rápido no 4G e funciona com CSP restrita, sem scripts inline. |
| Recorte recodificado por padrão | Precisão de quadro. O modo `fast_copy` fica disponível para máquinas fracas. |
| Upload remoto só depois do pagamento | Economiza upload e armazenamento. O replay não pago nunca sai da rede local. |
| Token secreto por pedido + links de 15 min | Evita IDOR e não existe galeria pública. |
| Assinatura SigV4 própria (sem boto3) | Mantém a imagem pequena. Está validada pelo vetor oficial da AWS. |
| Provedor Pix simulado com tabela própria | A demo usa o mesmo caminho do provedor real: webhook assinado, consulta e validação. |
