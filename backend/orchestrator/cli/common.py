"""Shared plumbing for CLI commands: global options, the per-command session and error -> exit-code mapping."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Callable, Iterator, Optional

import typer

from orchestrator.cli.client import Api, ApiError, CliError, resolve_url, scrub
from orchestrator.cli.output import Out

JsonOpt = Annotated[bool, typer.Option("--json", help="Print the raw API JSON instead of tables.")]
UrlOpt = Annotated[
    Optional[str],
    typer.Option("--url", help="Backend base URL (default: env ORCHESTRATOR_URL, else http://127.0.0.1:8765)."),
]

TERMINAL_OR_STOPPED = {"COMPLETED", "FAILED", "CANCELLED", "BLOCKED"}


@dataclass
class Session:
    api: Api
    out: Out
    json: bool

    def show(self, data: Any, render: Callable[[Out, Any], None]) -> None:
        if self.json:
            self.out.json(data)
        else:
            render(self.out, data)


def global_opts(ctx: typer.Context) -> dict[str, Any]:
    obj = ctx.find_root().obj
    return obj if isinstance(obj, dict) else {}


@contextmanager
def session(ctx: typer.Context, json_out: bool = False, url: Optional[str] = None) -> Iterator[Session]:
    """Open an API session for one command; CliError -> message on stderr + the documented exit code."""
    g = global_opts(ctx)
    out = Out()
    api = Api(resolve_url(url or g.get("url")))
    try:
        yield Session(api=api, out=out, json=bool(json_out or g.get("json")))
    except CliError as exc:
        text = str(exc)
        if isinstance(exc, ApiError) and exc.code and exc.status not in (0, 401):
            text = f"{text} [{exc.code}]"
        out.error(scrub(text))
        raise typer.Exit(code=exc.exit_code) from None
    finally:
        api.close()


def fail(message: str, code: int = 1) -> "typer.Exit":
    Out().error(scrub(message))
    return typer.Exit(code=code)


def optional_endpoint(s: Session, fn: Callable[[], Any], render: Callable[[Out, Any], None], what: str) -> None:
    """Run a call against an endpoint that may not exist in this build; 404 -> 'not available in this build'."""
    try:
        data = fn()
    except ApiError as exc:
        if exc.is_missing_endpoint():
            if s.json:
                s.out.json({"available": False, "feature": what, "message": "not available in this build"})
            else:
                s.out.line(f"{what}: not available in this build")
            return
        raise
    s.show(data, render)
