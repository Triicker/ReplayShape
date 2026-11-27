"""Armazenamento remoto opcional (Cloudflare R2 / qualquer S3-compatível).

O replay é SEMPRE gerado e mantido localmente (privado). O upload só ocorre
após pagamento confirmado e quando RS_STORAGE_BACKEND=s3. Enquanto o upload
não conclui, o arquivo continua sendo servido a partir do disco local.

Implementação mínima de AWS Signature V4 (sem boto3) para manter o projeto
leve: PUT, DELETE e URL pré-assinada de GET.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx


class RemoteStorage:
    name = "none"

    def upload(self, local_path: Path, key: str, content_type: str = "video/mp4") -> None:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def presigned_get(self, key: str, expires: int, filename: str | None = None) -> str:
        raise NotImplementedError


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _uri_encode(s: str, slash: bool = True) -> str:
    return quote(s, safe="-_.~" + ("" if slash else "/"))


class S3Storage(RemoteStorage):
    name = "s3"

    def __init__(self, endpoint: str, region: str, bucket: str, access_key: str, secret_key: str,
                 path_style: bool = True, client: httpx.Client | None = None):
        if not (endpoint and bucket and access_key and secret_key):
            raise ValueError("configuração S3/R2 incompleta")
        self.endpoint = endpoint.rstrip("/")
        self.region = region or "auto"
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.path_style = path_style
        self.client = client or httpx.Client(timeout=120)

    def _host_and_path(self, key: str) -> tuple[str, str, str]:
        u = urlsplit(self.endpoint)
        if self.path_style:
            return u.scheme, u.netloc, f"/{self.bucket}/{_uri_encode(key, slash=False)}"
        return u.scheme, f"{self.bucket}.{u.netloc}", f"/{_uri_encode(key, slash=False)}"

    def _signing_key(self, datestamp: str) -> bytes:
        k = _sign(("AWS4" + self.secret_key).encode(), datestamp)
        k = _sign(k, self.region)
        k = _sign(k, "s3")
        return _sign(k, "aws4_request")

    def presign(self, method: str, key: str, expires: int, now: dt.datetime | None = None,
                extra_query: dict[str, str] | None = None) -> str:
        now = now or dt.datetime.now(dt.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        scheme, host, path = self._host_and_path(key)
        scope = f"{datestamp}/{self.region}/s3/aws4_request"
        q = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": f"{self.access_key}/{scope}",
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(expires),
            "X-Amz-SignedHeaders": "host",
        }
        if extra_query:
            q.update(extra_query)
        canonical_q = "&".join(f"{_uri_encode(k)}={_uri_encode(v)}" for k, v in sorted(q.items()))
        canonical = "\n".join([method, path, canonical_q, f"host:{host}\n", "host", "UNSIGNED-PAYLOAD"])
        sts = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
        sig = hmac.new(self._signing_key(datestamp), sts.encode(), hashlib.sha256).hexdigest()
        return f"{scheme}://{host}{path}?{canonical_q}&X-Amz-Signature={sig}"

    def presigned_get(self, key: str, expires: int, filename: str | None = None) -> str:
        extra = None
        if filename:
            extra = {"response-content-disposition": f'attachment; filename="{filename}"'}
        return self.presign("GET", key, expires, extra_query=extra)

    def upload(self, local_path: Path, key: str, content_type: str = "video/mp4") -> None:
        url = self.presign("PUT", key, 900)
        with open(local_path, "rb") as f:
            r = self.client.put(url, content=f.read(), headers={"content-type": content_type})
        if r.status_code >= 300:
            raise RuntimeError(f"upload falhou: HTTP {r.status_code}")

    def delete(self, key: str) -> None:
        r = self.client.delete(self.presign("DELETE", key, 300))
        if r.status_code >= 300 and r.status_code != 404:
            raise RuntimeError(f"exclusão remota falhou: HTTP {r.status_code}")


def get_remote_storage(settings) -> RemoteStorage | None:
    if settings.storage_backend != "s3":
        return None
    return S3Storage(settings.s3_endpoint, settings.s3_region, settings.s3_bucket,
                     settings.s3_access_key, settings.s3_secret_key)
