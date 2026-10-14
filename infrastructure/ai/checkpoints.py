"""Owner-scoped, expiring checkpoints for validated model windows.

Only exact request identities are reused. Raw prompts and credentials are never
stored; a changed schema, prompt, model, configuration or inherited context misses.
This does not claim exactly-once billing for requests whose response was lost.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import threading
import uuid
from functools import wraps
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from pathlib import Path
from infrastructure.storage_locks import file_mutex, LockBusy


_pins = set()
_pins_lock = threading.Lock()


def _locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with file_mutex(self.path.parent / ".usage.lock"):
            return method(self, *args, **kwargs)
    return call


def _in_use(path):
    for marker in path.parent.glob(".pin-*"):
        with _pins_lock:
            if marker.resolve() in _pins:
                return True
        try:
            with file_mutex(marker, blocking=False):
                pass
        except LockBusy:
            return True
        marker.unlink(missing_ok=True)  # A crashed process released its OS lock.
    return False


@contextmanager
def _pin(store):
    marker = store.path.parent / f".pin-{uuid.uuid4().hex}"
    with ExitStack() as stack:
        with file_mutex(store.path.parent / ".usage.lock"):
            stack.enter_context(file_mutex(marker))
            with _pins_lock:
                _pins.add(marker.resolve())
        try:
            yield
        finally:
            with file_mutex(store.path.parent / ".usage.lock"):
                stack.close()
                with _pins_lock:
                    _pins.discard(marker.resolve())
                marker.unlink(missing_ok=True)


class WindowCheckpoints:
    """已验证模型窗口的检查点存储（owner 隔离 + TTL + 字节上界）。

    ``stage_fingerprint`` 是 L03 依赖图算出的阶段指纹。验证后锚点必须用
    ``legalize`` 指纹；原始模型响应另存于 ``route_raw`` 命名空间，验证规则变化时
    可以复用原文再跑新规则，而不把旧锚点当新结果。
    """

    def __init__(
        self,
        root: Path,
        namespace: str,
        *,
        ttl: float = 7 * 86400,
        max_bytes: int = 64 * 1024 * 1024,
        stage_fingerprint: str = "",
    ):
        self.path = Path(root) / hashlib.sha256(namespace.encode()).hexdigest() / "windows.sqlite3"
        self.ttl, self.max_bytes = ttl, max_bytes
        self.stage_fingerprint = stage_fingerprint
        #: 命中/未命中计数（12.2 的「断点复用率」生产者；进程内计数，不落盘）。
        self.hits = 0
        self.misses = 0
        self.last_access = time.time()

    @staticmethod
    def key(identity: dict) -> str:
        return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS windows ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL, created REAL NOT NULL, last_used REAL NOT NULL)"
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(windows)")}
        if "last_used" not in columns:
            connection.execute("ALTER TABLE windows ADD COLUMN last_used REAL")
            connection.execute("UPDATE windows SET last_used = created WHERE last_used IS NULL")
            connection.commit()
        return connection

    @_locked
    def load(self, key: str):
        if not self.path.is_file():
            self.misses += 1
            return None
        connection = self._connect()
        try:
            now = time.time()
            row = connection.execute(
                "SELECT value FROM windows WHERE key=? AND created>?",
                (key, now - self.ttl),
            ).fetchone()
            if row:
                self.hits += 1
                self.last_access = now
                connection.execute("UPDATE windows SET last_used=? WHERE key=?", (now, key))
                connection.commit()
                return json.loads(row[0])
            self.misses += 1
            return None
        finally:
            connection.close()

    def reuse_stats(self) -> dict:
        """断点复用率的**生产者**（12.2 此前标未测量的一项）。

        计数是进程内的、不落盘：它描述「这一次运行复用了多少窗口」，跨进程累计没有意义
        （不同任务、不同文档）。分母为 0 时不报比率——0/0 报成 0 会被读成「一次都没复用」。
        """
        total = self.hits + self.misses
        stats = {"hits": self.hits, "misses": self.misses, "lookups": total}
        if total:
            stats["reuse_ratio"] = self.hits / total
        return stats

    @_locked
    def save(self, key: str, value: dict) -> None:
        serialized = json.dumps(value, ensure_ascii=False)
        if len(serialized.encode()) > self.max_bytes:
            return
        connection = self._connect()
        try:
            with connection:
                now = time.time()
                connection.execute("DELETE FROM windows WHERE created<=?", (now - self.ttl,))
                connection.execute(
                    "INSERT OR REPLACE INTO windows(key, value, created, last_used) VALUES (?, ?, ?, ?)",
                    (key, serialized, now, now),
                )
                rows = connection.execute(
                    "SELECT key, length(CAST(value AS BLOB)) FROM windows ORDER BY last_used DESC"
                ).fetchall()
                size = 0
                for old_key, weight in rows:
                    size += weight
                    if size > self.max_bytes:
                        connection.execute("DELETE FROM windows WHERE key=?", (old_key,))
        finally:
            connection.close()
        self.last_access = time.time()

    @_locked
    def content_bytes(self) -> int:
        if not self.path.is_file():
            return 0
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT COALESCE(SUM(length(CAST(value AS BLOB))), 0) FROM windows"
            ).fetchone()
            return int(row[0] if row else 0)
        finally:
            connection.close()

    def disk_bytes(self) -> int:
        try:
            return int(self.path.stat().st_size)
        except OSError:
            return 0

    @_locked
    def collect_expired(self, *, now: float | None = None, min_idle: float | None = None) -> int:
        """删除过期或长期未访问的行。返回删除条数；不 VACUUM（避开活跃任务）。"""
        if not self.path.is_file():
            return 0
        now = now if now is not None else time.time()
        idle_cut = now - (min_idle if min_idle is not None else self.ttl)
        connection = self._connect()
        try:
            with connection:
                before = connection.execute("SELECT COUNT(*) FROM windows").fetchone()[0]
                connection.execute(
                    "DELETE FROM windows WHERE created<=? OR COALESCE(last_used, created)<=?",
                    (now - self.ttl, idle_cut),
                )
                after = connection.execute("SELECT COUNT(*) FROM windows").fetchone()[0]
            return int(before - after)
        finally:
            connection.close()

    @_locked
    def compact_if_idle(self) -> bool:
        """仅在库内已无行时尝试回收文件。活跃检查点不要 VACUUM。"""
        if not self.path.is_file() or _in_use(self.path):
            return False
        connection = self._connect()
        try:
            remaining = connection.execute("SELECT COUNT(*) FROM windows").fetchone()[0]
            if remaining:
                return False
        finally:
            connection.close()
        try:
            self.path.unlink()
            return True
        except OSError:
            return False

    @_locked
    def vacuum_if_fragmented(self, *, min_disk: int = 64 * 1024, ratio: float = 2.0) -> bool:
        """空闲库在磁盘远大于有效内容时 VACUUM。不碰仍有活跃引用的库（由调用方保证）。"""
        if not self.path.is_file() or _in_use(self.path):
            return False
        content = self.content_bytes()
        disk = self.disk_bytes()
        if disk < min_disk or content <= 0 or disk <= content * ratio:
            return False
        connection = self._connect()
        try:
            connection.isolation_level = None
            connection.execute("VACUUM")
            return True
        except sqlite3.Error:
            return False
        finally:
            connection.close()


_current: ContextVar[WindowCheckpoints | None] = ContextVar("window_checkpoints", default=None)
_raw_current: ContextVar[WindowCheckpoints | None] = ContextVar("raw_window_checkpoints", default=None)


def current_checkpoints() -> WindowCheckpoints | None:
    return _current.get()


def current_raw_checkpoints() -> WindowCheckpoints | None:
    return _raw_current.get()


@contextmanager
def checkpoint_scope(store: WindowCheckpoints, raw: WindowCheckpoints | None = None):
    token = _current.set(store)
    raw_token = _raw_current.set(raw)
    try:
        with ExitStack() as stack:
            for item in (store, raw):
                if item is not None and getattr(item, "path", None) is not None:
                    stack.enter_context(_pin(item))
            yield store
    finally:
        _raw_current.reset(raw_token)
        _current.reset(token)


def reclaim_checkpoint_root(root: Path, *, ttl: float = 7 * 86400, keep_paths: set[Path] | None = None) -> dict:
    """回收过期检查点命名空间。不碰 keep_paths 里仍被活跃任务引用的文件。"""
    root = Path(root)
    keep = {path.resolve() for path in (keep_paths or set())}
    scanned = 0
    deleted_rows = 0
    removed_files = 0
    vacuumed = 0
    if not root.is_dir():
        return {"scanned": 0, "deleted_rows": 0, "removed_files": 0, "vacuumed": 0}
    for db in root.glob("*/windows.sqlite3"):
        scanned += 1
        try:
            resolved = db.resolve()
        except OSError:
            continue
        if resolved in keep:
            continue
        store = WindowCheckpoints(root, db.parent.name, ttl=ttl)
        store.path = db
        try:
            with file_mutex(db.parent / ".usage.lock", blocking=False):
                if _in_use(db):
                    continue
                deleted_rows += store.collect_expired()
                if store.compact_if_idle():
                    removed_files += 1
                elif store.vacuum_if_fragmented():
                    vacuumed += 1
        except LockBusy:
            continue
    return {"scanned": scanned, "deleted_rows": deleted_rows, "removed_files": removed_files, "vacuumed": vacuumed}
