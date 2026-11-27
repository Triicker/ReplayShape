"""Acesso ao SQLite com migrações versionadas.

Todas as consultas usam parâmetros (`?`) — nunca interpolação de strings com
entrada do usuário (proteção contra SQL Injection).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import REPO_ROOT

MIGRATIONS_DIR = REPO_ROOT / "database" / "migrations"


def now() -> float:
    return time.time()


def new_id() -> str:
    return uuid.uuid4().hex


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._local = threading.local()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._connect()
            self._local.conn = c
        return c

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Transação serializada (BEGIN IMMEDIATE) — evita condições de corrida
        nas transições de estado de pedido/pagamento."""
        c = self.conn
        depth = getattr(self._local, "depth", 0)
        if depth:
            self._local.depth = depth + 1
            try:
                yield c
            finally:
                self._local.depth -= 1
            return
        with self._lock:
            c.execute("BEGIN IMMEDIATE")
            self._local.depth = 1
            try:
                yield c
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
            finally:
                self._local.depth = 0

    def one(self, sql: str, params: tuple | list = ()) -> dict | None:
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params: tuple | list = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def insert(self, table: str, values: dict[str, Any]) -> None:
        _check_ident(table, *values.keys())
        cols = ", ".join(values.keys())
        qs = ", ".join("?" for _ in values)
        self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", list(values.values()))

    def update(self, table: str, id_value: Any, values: dict[str, Any], id_col: str = "id") -> int:
        _check_ident(table, id_col, *values.keys())
        sets = ", ".join(f"{k} = ?" for k in values)
        cur = self.conn.execute(
            f"UPDATE {table} SET {sets} WHERE {id_col} = ?", [*values.values(), id_value]
        )
        return cur.rowcount

    # --- migrações ---------------------------------------------------------
    def migrate(self) -> list[str]:
        c = self.conn
        c.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at REAL NOT NULL)"
        )
        applied = {r["version"] for r in self.all("SELECT version FROM schema_migrations")}
        done = []
        for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = f.stem
            if version in applied:
                continue
            sql = f.read_text(encoding="utf-8")
            with self._lock:
                c.execute("BEGIN IMMEDIATE")
                try:
                    for stmt in _split_sql(sql):
                        c.execute(stmt)
                    c.execute("INSERT INTO schema_migrations VALUES (?, ?)", (version, now()))
                    c.execute("COMMIT")
                except BaseException:
                    c.execute("ROLLBACK")
                    raise
            done.append(version)
        return done

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None


_IDENT_OK = set("abcdefghijklmnopqrstuvwxyz_0123456789")


def _check_ident(*names: str) -> None:
    for n in names:
        if not n or set(n) - _IDENT_OK:
            raise ValueError(f"identificador SQL inválido: {n!r}")


def _split_sql(sql: str) -> list[str]:
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
