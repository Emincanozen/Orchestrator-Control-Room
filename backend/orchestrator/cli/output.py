"""Console output for the CLI: Rich tables on a UTF-8 terminal, plain ASCII everywhere else.

Windows PowerShell 5.1 consoles and redirected output often use a legacy code page, so unless stdout is an
interactive terminal with a UTF-8 encoding the output is ASCII only (ASCII table borders, non-ASCII text
replaced by `?`). Rich only emits ANSI when stdout is a TTY. Text coming from the API is data: control
characters are stripped and Rich markup is never interpreted.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any, Iterable, Optional, Sequence

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def is_utf8_tty(stream: Any) -> bool:
    try:
        tty = bool(stream.isatty())
    except Exception:  # noqa: BLE001
        tty = False
    enc = (getattr(stream, "encoding", None) or "").lower().replace("_", "-")
    return tty and enc in ("utf-8", "utf8")


class Out:
    def __init__(self, stream: Any = None, err_stream: Any = None):
        self.stream = stream if stream is not None else sys.stdout
        self.err_stream = err_stream if err_stream is not None else sys.stderr
        self.ascii = not is_utf8_tty(self.stream)
        try:
            tty = bool(self.stream.isatty())
        except Exception:  # noqa: BLE001
            tty = False
        self.console = Console(
            file=self.stream,
            force_terminal=tty,
            no_color=not tty,
            color_system="standard" if tty else None,
            highlight=False,
            emoji=False,
            markup=False,
            width=None if tty else 200,
            legacy_windows=False,
        )

    # ---- text safety ---------------------------------------------------------------------------
    def safe(self, value: Any) -> str:
        s = "" if value is None else str(value)
        s = _CONTROL.sub("", s.replace("\r\n", "\n").replace("\r", "\n"))
        if self.ascii:
            s = s.encode("ascii", "replace").decode("ascii")
        return s

    def line(self, text: str = "", *, style: Optional[str] = None) -> None:
        self.console.print(Text(self.safe(text), style=style or ""), soft_wrap=True)

    # ---- structures ----------------------------------------------------------------------------
    def table(self, columns: Sequence[str], rows: Iterable[Sequence[Any]], *, title: Optional[str] = None,
              keep: Sequence[str] = ()) -> None:
        """`keep`: column names that must never be wrapped or shortened (ids)."""
        t = Table(
            box=box.ASCII if self.ascii else box.SIMPLE_HEAD,
            title=self.safe(title) if title else None,
            show_header=True,
            header_style="bold" if not self.ascii else "",
        )
        for c in columns:
            if c in keep:
                t.add_column(c, no_wrap=True, overflow="ignore")
            else:
                t.add_column(c, overflow="fold")
        count = 0
        for r in rows:
            t.add_row(*[Text(self.safe(v)) for v in r])
            count += 1
        if count == 0:
            self.line("(none)")
            return
        self.console.print(t)

    def kv(self, pairs: Iterable[tuple[str, Any]], *, indent: int = 0) -> None:
        pairs = list(pairs)
        width = max((len(k) for k, _ in pairs), default=0)
        for k, v in pairs:
            self.line(f"{' ' * indent}{k.ljust(width)}  {self.safe(v)}")

    def json(self, data: Any) -> None:
        """Raw machine-readable output (never through Rich, never wrapped). Always valid ASCII JSON."""
        self.stream.write(json.dumps(data, indent=2, ensure_ascii=True, default=str) + "\n")
        self.stream.flush()

    def json_line(self, data: Any) -> None:
        self.stream.write(json.dumps(data, ensure_ascii=True, default=str) + "\n")
        self.stream.flush()

    def error(self, message: str) -> None:
        try:
            self.err_stream.write(f"error: {self.safe(message)}\n")
            self.err_stream.flush()
        except Exception:  # noqa: BLE001
            pass

    def note(self, message: str) -> None:
        self.err_stream.write(self.safe(message) + "\n")
        self.err_stream.flush()


def measured(v: Any) -> str:
    """Render an API `{value, exactness, source}` object; missing -> 'unknown'. Never invents a number."""
    if isinstance(v, dict) and "value" in v:
        val = v.get("value")
        if val is None:
            return "unknown"
        exact = v.get("exactness")
        return f"{val} ({exact})" if exact else str(val)
    if v is None:
        return "unknown"
    return str(v)


def short(value: Any, n: int = 60) -> str:
    s = "" if value is None else str(value).replace("\n", " ")
    return s if len(s) <= n else s[: n - 3] + "..."


def is_metric(v: Any) -> bool:
    return isinstance(v, dict) and "value" in v and "exactness" in v


def render_generic(out: Out, data: Any, *, indent: int = 0, title: Optional[str] = None) -> None:
    """Readable fallback for payloads without a dedicated layout (nested key/values; lists of objects as tables)."""
    pad = " " * indent
    if title:
        out.line(f"{pad}{title}")
    if isinstance(data, dict):
        scalars = [(k, v) for k, v in data.items() if not isinstance(v, (dict, list)) or is_metric(v)]
        if scalars:
            out.kv([(k, measured(v) if is_metric(v) else ("unknown" if v is None else v)) for k, v in scalars], indent=indent)
        for k, v in data.items():
            if is_metric(v):
                continue
            if isinstance(v, dict):
                if not v:
                    continue
                render_generic(out, v, indent=indent + 2, title=f"{k}:")
            elif isinstance(v, list):
                _render_list(out, k, v, indent + 2)
    elif isinstance(data, list):
        _render_list(out, "items", data, indent)
    else:
        out.line(f"{pad}{out.safe(data)}")


def _render_list(out: Out, name: str, items: list[Any], indent: int) -> None:
    pad = " " * indent
    if not items:
        out.line(f"{pad}{name}: (none)")
        return
    if all(isinstance(i, dict) for i in items):
        cols: list[str] = []
        for i in items:
            for k, v in i.items():
                if k not in cols and not isinstance(v, (dict, list)):
                    cols.append(k)
        cols = cols[:7]
        out.line(f"{pad}{name}:")
        out.table(cols, [[short(i.get(c), 70) for c in cols] for i in items])
    else:
        out.line(f"{pad}{name}: " + ", ".join(short(i, 80) for i in items))
