-- Replay Shape — esquema inicial (SQLite; compatível em grande parte com PostgreSQL)
-- Datas/horas: epoch em segundos (REAL, UTC). IDs: UUID4 hex (TEXT).

CREATE TABLE establishments (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    slug          TEXT NOT NULL UNIQUE,
    active        INTEGER NOT NULL DEFAULT 1,
    price_cents   INTEGER NOT NULL DEFAULT 500,
    currency      TEXT NOT NULL DEFAULT 'BRL',
    brand_color   TEXT NOT NULL DEFAULT '#a3e635',
    venue_type    TEXT NOT NULL DEFAULT 'sinuca',
    privacy_notice TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE TABLE cameras (
    id              TEXT PRIMARY KEY,
    establishment_id TEXT NOT NULL REFERENCES establishments(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    area_label      TEXT NOT NULL,              -- ex.: "Mesa 01", "Quadra 02"
    public_code     TEXT NOT NULL UNIQUE,       -- código aleatório usado no QR Code
    adapter         TEXT NOT NULL DEFAULT 'demo', -- demo | rtsp | intelbras
    manufacturer    TEXT,
    model           TEXT,
    host            TEXT,
    port            INTEGER,
    username        TEXT,
    password_enc    TEXT,                       -- criptografado (Fernet)
    rtsp_path       TEXT,
    rtsp_url_enc    TEXT,                       -- URL completa opcional, criptografada
    channel         INTEGER NOT NULL DEFAULT 1,
    subtype         INTEGER NOT NULL DEFAULT 0,
    protocol        TEXT NOT NULL DEFAULT 'rtsp',
    resolution      TEXT,
    fps             INTEGER,
    replay_seconds  INTEGER NOT NULL DEFAULT 60,
    price_cents     INTEGER,                    -- NULL = herda do estabelecimento
    quality_profile TEXT,                       -- NULL = configuração global
    watermark       INTEGER,                    -- NULL = configuração global
    supports_playback INTEGER NOT NULL DEFAULT 0, -- Estratégia B validada?
    active          INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'unknown', -- unknown|starting|online|offline|error|disabled
    status_detail   TEXT,
    last_seen_at    REAL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX idx_cameras_est ON cameras(establishment_id);

CREATE TABLE devices (
    id               TEXT PRIMARY KEY,
    camera_id        TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    name             TEXT NOT NULL,
    secret_enc       TEXT NOT NULL,
    active           INTEGER NOT NULL DEFAULT 1,
    cooldown_seconds INTEGER NOT NULL DEFAULT 15,
    firmware_version TEXT,
    last_seen_at     REAL,
    last_trigger_at  REAL,
    last_ip_hash     TEXT,
    created_at       REAL NOT NULL
);

CREATE TABLE camera_buffers (
    camera_id        TEXT PRIMARY KEY REFERENCES cameras(id) ON DELETE CASCADE,
    state            TEXT NOT NULL,
    segments         INTEGER NOT NULL DEFAULT 0,
    seconds_buffered REAL NOT NULL DEFAULT 0,
    oldest_at        REAL,
    newest_at        REAL,
    restarts         INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    updated_at       REAL NOT NULL
);

-- Estados de captura/processamento:
-- queued -> capturing -> processing -> ready | failed
CREATE TABLE replay_requests (
    id              TEXT PRIMARY KEY,
    camera_id       TEXT NOT NULL REFERENCES cameras(id),
    establishment_id TEXT NOT NULL REFERENCES establishments(id),
    source          TEXT NOT NULL,              -- web | device | admin
    device_id       TEXT,
    trigger_at      REAL NOT NULL,              -- momento do acionamento (define o recorte)
    window_start    REAL NOT NULL,
    window_end      REAL NOT NULL,
    duration_s      INTEGER NOT NULL,
    status          TEXT NOT NULL,
    error_code      TEXT,
    error_detail    TEXT,
    processing_ms   INTEGER,
    claimed         INTEGER NOT NULL DEFAULT 1,
    client_ip_hash  TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX idx_rr_camera ON replay_requests(camera_id, created_at);

-- Estados do arquivo: private -> available -> deleted | missing
CREATE TABLE replay_files (
    id              TEXT PRIMARY KEY,
    replay_request_id TEXT NOT NULL UNIQUE REFERENCES replay_requests(id),
    local_path      TEXT,
    remote_key      TEXT,
    remote_status   TEXT NOT NULL DEFAULT 'none', -- none|pending|uploaded|failed|deleted
    upload_attempts INTEGER NOT NULL DEFAULT 0,
    size_bytes      INTEGER,
    sha256          TEXT,
    duration_s      REAL,
    width           INTEGER,
    height          INTEGER,
    codec           TEXT,
    status          TEXT NOT NULL,
    delete_after    REAL,
    deleted_at      REAL,
    deleted_reason  TEXT,
    created_at      REAL NOT NULL
);

-- Estados do pedido:
-- processing -> awaiting_payment -> paid -> closed
--            \-> failed            \-> expired (não pago)
-- paid_unfulfillable / refund_pending / refunded / cancelled
CREATE TABLE orders (
    id              TEXT PRIMARY KEY,
    replay_request_id TEXT NOT NULL UNIQUE REFERENCES replay_requests(id),
    establishment_id TEXT NOT NULL REFERENCES establishments(id),
    camera_id       TEXT NOT NULL REFERENCES cameras(id),
    access_token_hash TEXT NOT NULL,
    short_code      TEXT NOT NULL UNIQUE,
    amount_cents    INTEGER NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'BRL',
    status          TEXT NOT NULL,
    payment_deadline REAL,
    available_until REAL,
    paid_at         REAL,
    closed_at       REAL,
    demo            INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX idx_orders_status ON orders(status);
CREATE INDEX idx_orders_created ON orders(created_at);

-- Estados de pagamento: pending | approved | rejected | cancelled | expired | refunded | charged_back
CREATE TABLE payments (
    id              TEXT PRIMARY KEY,
    order_id        TEXT NOT NULL REFERENCES orders(id),
    provider        TEXT NOT NULL,
    provider_payment_id TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL,
    status_detail   TEXT,
    amount_cents    INTEGER NOT NULL,
    currency        TEXT NOT NULL,
    pix_copy_paste  TEXT,
    ticket_url      TEXT,
    expires_at      REAL,
    approved_at     REAL,
    fee_cents       INTEGER,
    refund_status   TEXT,                       -- NULL | requested | done | failed
    duplicate       INTEGER NOT NULL DEFAULT 0,
    last_checked_at REAL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    UNIQUE(provider, provider_payment_id)
);
CREATE INDEX idx_payments_order ON payments(order_id);

CREATE TABLE payment_events (
    id              TEXT PRIMARY KEY,
    provider        TEXT NOT NULL,
    event_key       TEXT NOT NULL UNIQUE,       -- idempotência de webhooks
    provider_payment_id TEXT,
    type            TEXT,
    signature_valid INTEGER NOT NULL,
    payload         TEXT,
    result          TEXT,
    received_at     REAL NOT NULL
);

CREATE TABLE download_tokens (
    id              TEXT PRIMARY KEY,
    order_id        TEXT NOT NULL REFERENCES orders(id),
    token_hash      TEXT NOT NULL UNIQUE,
    expires_at      REAL NOT NULL,
    uses            INTEGER NOT NULL DEFAULT 0,
    last_used_at    REAL,
    created_at      REAL NOT NULL
);

CREATE TABLE admin_users (
    id              TEXT PRIMARY KEY,
    email           TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL,              -- superadmin | operator
    establishment_id TEXT REFERENCES establishments(id),
    active          INTEGER NOT NULL DEFAULT 1,
    last_login_at   REAL,
    created_at      REAL NOT NULL
);

CREATE TABLE admin_sessions (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
    token_hash      TEXT NOT NULL UNIQUE,
    csrf_token      TEXT NOT NULL,
    expires_at      REAL NOT NULL,
    created_at      REAL NOT NULL
);

CREATE TABLE audit_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    at              REAL NOT NULL,
    actor_type      TEXT NOT NULL,              -- system | admin | public | device | provider
    actor_id        TEXT,
    action          TEXT NOT NULL,
    entity          TEXT,
    entity_id       TEXT,
    details         TEXT
);
CREATE INDEX idx_audit_at ON audit_logs(at);

CREATE TABLE system_settings (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,              -- JSON
    updated_at      REAL NOT NULL
);

CREATE TABLE privacy_requests (
    id              TEXT PRIMARY KEY,
    order_code      TEXT,
    contact         TEXT,
    message         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
    created_at      REAL NOT NULL
);

-- "Provedor" simulado de Pix (somente modo demonstração). Fica numa tabela
-- própria para se comportar como um sistema externo.
CREATE TABLE mock_pix_charges (
    id              TEXT PRIMARY KEY,
    external_reference TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    amount_cents    INTEGER NOT NULL,
    currency        TEXT NOT NULL,
    status          TEXT NOT NULL,
    expires_at      REAL NOT NULL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
