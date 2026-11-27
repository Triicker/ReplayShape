"""Configuração via variáveis de ambiente.

Todos os valores sensíveis vêm do ambiente (ou de `data/secret.key`, gerado
automaticamente apenas fora de produção). Nada sensível é versionado.
"""
from __future__ import annotations

import os
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on", "sim"}


def _int(name: str, default: int) -> int:
    v = os.getenv(name)
    return int(v) if v not in (None, "") else default


def _float(name: str, default: float) -> float:
    v = os.getenv(name)
    return float(v) if v not in (None, "") else default


def _detect_font() -> str | None:
    candidates = [
        os.getenv("RS_FONT_FILE"),
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return c
    return None


@dataclass
class Settings:
    app_env: str = "development"  # development | test | production
    demo_mode: bool = True
    public_base_url: str = "http://localhost:8000"
    data_dir: Path = REPO_ROOT / "data"
    secret_key: str = ""
    ip_hash_salt: str = ""
    cookie_secure: bool = False

    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    font_file: str | None = None

    # Edge
    edge_enabled: bool = True
    segment_seconds: int = 2
    capture_wait_timeout: float = 8.0
    processing_workers: int = 2
    allow_public_camera_hosts: bool = False

    # Pagamentos
    payment_provider: str = "mock"  # mock | mercadopago
    mp_access_token: str = ""
    mp_webhook_secret: str = ""
    mp_api_base: str = "https://api.mercadopago.com"
    mp_payer_email: str = ""

    # Armazenamento
    storage_backend: str = "local"  # local | s3
    s3_endpoint: str = ""
    s3_region: str = "auto"
    s3_bucket: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""

    # Admin bootstrap
    admin_email: str = ""
    admin_password: str = ""

    # Rotinas
    cleanup_interval: float = 15.0
    reconcile_interval: float = 60.0
    start_background: bool = True

    extra: dict = field(default_factory=dict)

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "replayshape.db"

    @property
    def buffers_dir(self) -> Path:
        return self.data_dir / "buffers"

    @property
    def replays_dir(self) -> Path:
        return self.data_dir / "replays"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.buffers_dir, self.replays_dir, self.tmp_dir):
            d.mkdir(parents=True, exist_ok=True)

    def validate(self) -> None:
        """Bloqueia configurações perigosas em produção."""
        if not self.is_production:
            return
        problems = []
        if self.demo_mode:
            problems.append("RS_DEMO_MODE deve ser false em produção")
        if self.payment_provider == "mock":
            problems.append("RS_PAYMENT_PROVIDER=mock não é permitido em produção")
        if not os.getenv("RS_SECRET_KEY"):
            problems.append("RS_SECRET_KEY é obrigatório em produção")
        if not self.cookie_secure:
            problems.append("RS_COOKIE_SECURE deve ser true em produção (HTTPS)")
        if self.payment_provider == "mercadopago" and not (self.mp_access_token and self.mp_webhook_secret):
            problems.append("Credenciais do Mercado Pago ausentes")
        if problems:
            raise RuntimeError("Configuração inválida para produção: " + "; ".join(problems))


def _load_or_create_secret(data_dir: Path, filename: str) -> str:
    data_dir.mkdir(parents=True, exist_ok=True)
    p = data_dir / filename
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    value = secrets.token_urlsafe(48)
    p.write_text(value, encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return value


def load_settings(**overrides) -> Settings:
    data_dir = Path(os.getenv("RS_DATA_DIR", str(REPO_ROOT / "data"))).resolve()
    s = Settings(
        app_env=os.getenv("RS_ENV", "development"),
        demo_mode=_bool("RS_DEMO_MODE", True),
        public_base_url=os.getenv("RS_PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
        data_dir=data_dir,
        cookie_secure=_bool("RS_COOKIE_SECURE", False),
        ffmpeg_bin=os.getenv("RS_FFMPEG_BIN") or shutil.which("ffmpeg") or "ffmpeg",
        ffprobe_bin=os.getenv("RS_FFPROBE_BIN") or shutil.which("ffprobe") or "ffprobe",
        font_file=_detect_font(),
        edge_enabled=_bool("RS_EDGE_ENABLED", True),
        segment_seconds=_int("RS_SEGMENT_SECONDS", 2),
        capture_wait_timeout=_float("RS_CAPTURE_WAIT_TIMEOUT", 8.0),
        processing_workers=_int("RS_PROCESSING_WORKERS", 2),
        allow_public_camera_hosts=_bool("RS_ALLOW_PUBLIC_CAMERA_HOSTS", False),
        payment_provider=os.getenv("RS_PAYMENT_PROVIDER", "mock"),
        mp_access_token=os.getenv("RS_MP_ACCESS_TOKEN", ""),
        mp_webhook_secret=os.getenv("RS_MP_WEBHOOK_SECRET", ""),
        mp_api_base=os.getenv("RS_MP_API_BASE", "https://api.mercadopago.com"),
        mp_payer_email=os.getenv("RS_MP_PAYER_EMAIL", ""),
        storage_backend=os.getenv("RS_STORAGE_BACKEND", "local"),
        s3_endpoint=os.getenv("RS_S3_ENDPOINT", ""),
        s3_region=os.getenv("RS_S3_REGION", "auto"),
        s3_bucket=os.getenv("RS_S3_BUCKET", ""),
        s3_access_key=os.getenv("RS_S3_ACCESS_KEY", ""),
        s3_secret_key=os.getenv("RS_S3_SECRET_KEY", ""),
        admin_email=os.getenv("RS_ADMIN_EMAIL", ""),
        admin_password=os.getenv("RS_ADMIN_PASSWORD", ""),
        cleanup_interval=_float("RS_CLEANUP_INTERVAL", 15.0),
        reconcile_interval=_float("RS_RECONCILE_INTERVAL", 60.0),
        start_background=_bool("RS_START_BACKGROUND", True),
    )
    for k, v in overrides.items():
        setattr(s, k, v)
    s.data_dir = Path(s.data_dir).resolve()
    if not s.secret_key:
        s.secret_key = os.getenv("RS_SECRET_KEY") or (
            "" if s.is_production else _load_or_create_secret(s.data_dir, "secret.key")
        )
    if not s.ip_hash_salt:
        s.ip_hash_salt = os.getenv("RS_IP_HASH_SALT") or s.secret_key[:16]
    s.validate()
    s.ensure_dirs()
    return s
