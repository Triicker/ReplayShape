"""Contêiner de dependências da aplicação."""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from ..config import Settings
from ..db import Database
from ..edge.manager import BufferManager
from ..integrations.payments import get_provider
from ..integrations.storage import get_remote_storage
from ..security import NonceCache, RateLimiter, SecretBox
from .settings_store import SettingsStore

log = logging.getLogger("replayshape")


class AppContext:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.db.migrate()
        self.store = SettingsStore(self.db)
        self.box = SecretBox(settings.secret_key)
        self.provider = get_provider(settings, self.db)
        self.remote = get_remote_storage(settings)
        self.buffers = BufferManager(self)
        self.executor = ThreadPoolExecutor(max_workers=max(1, settings.processing_workers),
                                           thread_name_prefix="replay")
        self.rate = RateLimiter()
        self.nonces = NonceCache()
        self.disk_low = False
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start_background(self) -> None:
        from . import cleanup, orders

        def loop(fn, interval, name):
            def run():
                while not self._stop.wait(interval):
                    try:
                        fn(self)
                    except Exception:
                        log.exception("rotina %s falhou", name)
            t = threading.Thread(target=run, name=name, daemon=True)
            t.start()
            self._threads.append(t)

        loop(cleanup.run_cleanup, self.settings.cleanup_interval, "cleanup")
        loop(orders.reconcile_pending, self.settings.reconcile_interval, "reconcile")
        loop(lambda c: c.buffers.persist_snapshots(), 10, "buffer-status")

    def shutdown(self) -> None:
        self._stop.set()
        self.buffers.stop_all()
        self.executor.shutdown(wait=False, cancel_futures=True)
