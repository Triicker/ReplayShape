"""Primitivas de segurança: hashes, tokens, criptografia de segredos, rate limit."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import deque

from cryptography.fernet import Fernet, InvalidToken

# Alfabeto sem caracteres ambíguos (0/O, 1/I/L) para códigos digitados por pessoas.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def short_code(length: int = 8) -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))
    return f"{raw[:4]}-{raw[4:]}" if length == 8 else raw


def normalize_code(code: str) -> str:
    c = "".join(ch for ch in (code or "").upper() if ch in _CODE_ALPHABET)
    return f"{c[:4]}-{c[4:]}" if len(c) == 8 else c


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def consteq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# --- senhas (scrypt da biblioteca padrão) ---------------------------------
_SCRYPT = dict(n=2**14, r=8, p=1, dklen=32)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt_b64), **_SCRYPT)
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except Exception:
        return False


# --- criptografia de segredos em repouso (senhas de câmera, segredos de dispositivo)
class SecretBox:
    def __init__(self, master_key: str):
        key = base64.urlsafe_b64encode(hashlib.sha256(("rs-secretbox:" + master_key).encode()).digest())
        self._f = Fernet(key)

    def encrypt(self, plaintext: str | None) -> str | None:
        if plaintext in (None, ""):
            return None
        return self._f.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str | None) -> str | None:
        if not token:
            return None
        try:
            return self._f.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken:
            return None


def ip_hash(ip: str | None, salt: str) -> str | None:
    if not ip:
        return None
    return hashlib.sha256((salt + "|" + ip).encode()).hexdigest()[:16]


# --- rate limiting em memória (janela deslizante) -------------------------
class RateLimiter:
    def __init__(self):
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window: float) -> bool:
        t = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and q[0] <= t - window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(t)
            if len(self._hits) > 50_000:  # proteção de memória
                self._hits = {k: v for k, v in self._hits.items() if v}
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


class NonceCache:
    """Impede reutilização de requisições assinadas (replay attack)."""

    def __init__(self, ttl: float = 300):
        self.ttl = ttl
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def check_and_add(self, key: str) -> bool:
        t = time.time()
        with self._lock:
            if len(self._seen) > 10_000:
                self._seen = {k: v for k, v in self._seen.items() if v > t - self.ttl}
            if key in self._seen and self._seen[key] > t - self.ttl:
                return False
            self._seen[key] = t
            return True


def device_signature(secret: str, ts: str, nonce: str, method: str, path: str, body: bytes) -> str:
    """Assinatura HMAC-SHA256 usada pelos botões físicos (ESP32).

    mensagem = ts \\n nonce \\n MÉTODO \\n caminho \\n sha256(corpo)
    """
    body_hash = hashlib.sha256(body or b"").hexdigest()
    msg = f"{ts}\n{nonce}\n{method.upper()}\n{path}\n{body_hash}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
