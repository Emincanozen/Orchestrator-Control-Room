"""SQLite access per ADR-003.

- stdlib sqlite3, WAL, synchronous=NORMAL, foreign keys on.
- One writer connection guarded by a lock; all writes go through
  `await db.write(fn)` which runs `fn(tx)` in a worker thread inside one
  `BEGIN IMMEDIATE ... COMMIT` transaction. `fn` is plain synchronous code
  (repository functions); it must not await or call `db.write` itself.
- Callbacks registered with `tx.after_commit(cb)` run on the event loop thread
  after a successful commit (the event bus uses this to broadcast).
- A separate read-only connection serves `await db.read(fn)`; with WAL it only
  ever sees committed data.
- Schema versioning via PRAGMA user_version: version 1 is schema.sql, later
  versions are db/migrations/NNN_*.sql applied in order.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence, TypeVar

T = TypeVar("T")

DB_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = DB_DIR / "schema.sql"
MIGRATIONS_DIR = DB_DIR / "migrations"


class Tx:
    """A live transaction (or a read session) on one connection."""

    def __init__(self, conn: sqlite3.Connection, *, writable: bool):
        self.conn = conn
        self.writable = writable
        self._after: list[Callable[[], Any]] = []

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def executemany(self, sql: str, seq: Sequence[Sequence[Any]]) -> sqlite3.Cursor:
        return self.conn.executemany(sql, seq)

    def one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def scalar(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return None if row is None else row[0]

    def after_commit(self, callback: Callable[[], Any]) -> None:
        if not self.writable:
            raise RuntimeError("after_commit requires a write transaction")
        self._after.append(callback)


def _connect(path: Path, *, read_only: bool) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if read_only:
        conn.execute("PRAGMA query_only = ON")
    else:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _migration_files() -> list[tuple[int, Path]]:
    out: list[tuple[int, Path]] = []
    if MIGRATIONS_DIR.is_dir():
        for p in sorted(MIGRATIONS_DIR.glob("*.sql")):
            m = re.match(r"^(\d+)_", p.name)
            if m:
                out.append((int(m.group(1)), p))
    return out


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._writer: sqlite3.Connection | None = None
        self._reader: sqlite3.Connection | None = None
        self._wlock = threading.Lock()
        self._rlock = threading.Lock()

    # ---- lifecycle -------------------------------------------------------
    def open(self) -> "Database":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._writer = _connect(self.path, read_only=False)
        self._migrate()
        self._reader = _connect(self.path, read_only=True)
        return self

    def close(self) -> None:
        with self._wlock:
            if self._writer is not None:
                self._writer.close()
                self._writer = None
        with self._rlock:
            if self._reader is not None:
                self._reader.close()
                self._reader = None

    @property
    def schema_version(self) -> int:
        assert self._writer is not None
        with self._wlock:
            return int(self._writer.execute("PRAGMA user_version").fetchone()[0])

    def _migrate(self) -> None:
        assert self._writer is not None
        conn = self._writer
        with self._wlock:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            steps: list[tuple[int, str]] = []
            if version < 1:
                steps.append((1, SCHEMA_PATH.read_text(encoding="utf-8")))
            for number, path in _migration_files():
                if number > max(version, 1):
                    steps.append((number, path.read_text(encoding="utf-8")))
            for number, sql in steps:
                # executescript commits any pending tx first; wrap the step itself.
                try:
                    conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {int(number)};\nCOMMIT;")
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise

    # ---- transactions ----------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[Tx]:
        """Synchronous write transaction. Caller must hold no other db lock.

        Prefer `await db.write(fn)` from async code.
        """
        if self._writer is None:
            raise RuntimeError("database is not open")
        with self._wlock:
            conn = self._writer
            conn.execute("BEGIN IMMEDIATE")
            tx = Tx(conn, writable=True)
            try:
                yield tx
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def write_sync(self, fn: Callable[[Tx], T]) -> T:
        with self.transaction() as tx:
            result = fn(tx)
        for cb in tx._after:
            cb()
        return result

    def _write_in_thread(self, fn: Callable[[Tx], T]) -> tuple[T, list[Callable[[], Any]]]:
        with self.transaction() as tx:
            result = fn(tx)
        return result, list(tx._after)

    async def write(self, fn: Callable[[Tx], T]) -> T:
        result, callbacks = await asyncio.to_thread(self._write_in_thread, fn)
        for cb in callbacks:
            cb()
        return result

    def read_sync(self, fn: Callable[[Tx], T]) -> T:
        if self._reader is None:
            raise RuntimeError("database is not open")
        with self._rlock:
            return fn(Tx(self._reader, writable=False))

    async def read(self, fn: Callable[[Tx], T]) -> T:
        return await asyncio.to_thread(self.read_sync, fn)

    async def all(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        return await self.read(lambda tx: tx.all(sql, params))

    async def one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Row | None:
        return await self.read(lambda tx: tx.one(sql, params))
