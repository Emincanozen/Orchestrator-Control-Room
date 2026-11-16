"""Shared helpers for repository modules (explicit SQL, no ORM)."""

from __future__ import annotations

from enum import Enum
from typing import Any, Iterable

from pydantic import BaseModel

from orchestrator.db.database import Tx
from orchestrator.util import dumps


def encode(value: Any, *, json_col: bool) -> Any:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if json_col:
        if isinstance(value, list):
            value = [v.model_dump(mode="json") if isinstance(v, BaseModel) else v for v in value]
        return None if value is None else dumps(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bool):
        return int(value)
    return value


def insert(tx: Tx, table: str, values: dict[str, Any], json_cols: Iterable[str] = ()) -> None:
    jc = set(json_cols)
    cols = list(values)
    params = [encode(values[c], json_col=c in jc) for c in cols]
    tx.execute(
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        params,
    )


def update(
    tx: Tx,
    table: str,
    key_col: str,
    key: Any,
    fields: dict[str, Any],
    *,
    allowed: Iterable[str],
    json_cols: Iterable[str] = (),
) -> int:
    if not fields:
        return 0
    allowed_set, jc = set(allowed), set(json_cols)
    bad = set(fields) - allowed_set
    if bad:
        raise ValueError(f"cannot update {table} columns {sorted(bad)}")
    cols = list(fields)
    sets = ", ".join(f"{c} = ?" for c in cols)
    params = [encode(fields[c], json_col=c in jc) for c in cols] + [key]
    return tx.execute(f"UPDATE {table} SET {sets} WHERE {key_col} = ?", params).rowcount
