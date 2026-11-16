"""M9a: the API token must not reach server logs, and the WebSocket accepts it via Sec-WebSocket-Protocol."""

from __future__ import annotations

import io
import logging
import logging.config

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from orchestrator.api.app import create_app
from orchestrator.providers.mock import MockProvider
from orchestrator.security.logfilter import (
    TokenRedactingFilter,
    install_on_uvicorn_loggers,
    scrub,
    uvicorn_log_config,
)

BASE = "http://127.0.0.1:8765"
WS = "ws://127.0.0.1:8765"
SECRET = "Zx9-very_secret_token_value-1234567890abcdef"


@pytest.fixture
def client(tmp_path):
    app = create_app(data_dir=tmp_path / "data", providers={"mock": MockProvider()}, celebrate_seconds=0, poll_interval=0.05)
    with TestClient(app, base_url=BASE) as c:
        c.token = app.state.rt.token
        yield c


# ------------------------------------------------------------------ logging filter
def test_scrub_redacts_token_query_values_only():
    assert SECRET not in scrub(f"/ws?token={SECRET}&after_seq=5")
    assert scrub(f"/ws?token={SECRET}&after_seq=5") == "/ws?token=[REDACTED]&after_seq=5"
    assert scrub(f"/ws?after_seq=5&token={SECRET}") == "/ws?after_seq=5&token=[REDACTED]"
    assert scrub(f'GET /x?access_token={SECRET} HTTP/1.1') == "GET /x?access_token=[REDACTED] HTTP/1.1"
    assert scrub("/ws?after_seq=5") == "/ws?after_seq=5"
    assert scrub("/api/tasks?state=RUNNING") == "/api/tasks?state=RUNNING"


def test_filter_rewrites_uvicorn_access_and_error_records():
    f = TokenRedactingFilter()
    access = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1:5000", "GET", f"/ws?token={SECRET}&after_seq=1", "1.1", 403), None)
    assert f.filter(access) is True
    assert SECRET not in access.getMessage() and "token=[REDACTED]" in access.getMessage()
    err = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, f"WebSocket /ws?token={SECRET} [accepted]", (), None)
    assert f.filter(err) is True and SECRET not in err.getMessage()
    odd = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, "n=%d", (3,), None)  # non-string args untouched
    assert f.filter(odd) and odd.getMessage() == "n=3"
    dict_args = logging.LogRecord("x", logging.INFO, __file__, 1, "%(u)s", ({"u": f"/ws?token={SECRET}"},), None)
    assert f.filter(dict_args) and SECRET not in dict_args.getMessage()


def test_uvicorn_log_config_redacts_through_the_real_handlers():
    cfg = uvicorn_log_config()
    assert all("redact_token" in h["filters"] for h in cfg["handlers"].values())
    logging.config.dictConfig(cfg)
    try:
        buf = io.StringIO()
        for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
            lg = logging.getLogger(name)
            lg.setLevel(logging.INFO)
            for h in lg.handlers:  # uvicorn.error propagates to the "uvicorn" handler
                h.stream = buf
        logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET", f"/ws?token={SECRET}&after_seq=0", "1.1", 101)
        logging.getLogger("uvicorn.error").info("WebSocket %s [accepted]", f"/ws?token={SECRET}")
        out = buf.getvalue()
        assert out.count("token=[REDACTED]") == 2 and SECRET not in out
    finally:
        logging.config.dictConfig(uvicorn_log_config() | {"disable_existing_loggers": False})


def test_install_is_idempotent_and_app_startup_installs_it(client):
    install_on_uvicorn_loggers()
    install_on_uvicorn_loggers()
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        assert sum(isinstance(f, TokenRedactingFilter) for f in logging.getLogger(name).filters) == 1


def test_cli_serve_passes_the_redacting_log_config(monkeypatch, tmp_path):
    # hermetic: never probe the instance lock of the user's real data dir (a live server may hold it)
    monkeypatch.setenv("ORCHESTRATOR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ORCHESTRATOR_WORKTREES_DIR", raising=False)
    from typer.testing import CliRunner

    from orchestrator.cli import main as cli_main

    captured = {}
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: captured.update(kw))
    res = CliRunner().invoke(cli_main.app, ["serve", "--port", "8799", "--no-browser"])
    assert res.exit_code == 0, res.output
    assert "redact_token" in captured["log_config"]["filters"] and captured["access_log"] is True


# ------------------------------------------------------------------ WebSocket subprotocol auth
def test_ws_accepts_the_token_via_subprotocol_and_echoes_orch(client):
    with client.websocket_connect(f"{WS}/ws", subprotocols=["orch", f"orch.token.{client.token}"]) as ws:
        assert ws.accepted_subprotocol == "orch"


def test_ws_subprotocol_token_alone_is_accepted_and_echoes_what_the_client_offered(client):
    proto = f"orch.token.{client.token}"
    with client.websocket_connect(f"{WS}/ws", subprotocols=[proto]) as ws:
        assert ws.accepted_subprotocol == proto


def test_ws_query_token_still_works_without_a_subprotocol(client):
    with client.websocket_connect(f"{WS}/ws?token={client.token}") as ws:
        assert ws.accepted_subprotocol is None


@pytest.mark.parametrize("subprotocols", [["orch", "orch.token.wrong"], ["orch"], ["orch.token."], ["orch.token"]])
def test_ws_rejects_bad_subprotocol_tokens(client, subprotocols):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"{WS}/ws", subprotocols=subprotocols) as ws:
            ws.receive_json()
    assert exc.value.code == 4401


def test_ws_replays_events_with_the_subprotocol_token(client):
    first = client.get("/api/events", headers={"Authorization": f"Bearer {client.token}"}).json()
    with client.websocket_connect(f"{WS}/ws?after_seq=0", subprotocols=["orch", f"orch.token.{client.token}"]) as ws:
        seen = []
        while True:
            m = ws.receive_json()
            if m.get("type") == "replay.done":
                break
            seen.append(m["seq"])
    # Background events (scheduler ticks, startup notices) can be appended between the REST call and the replay.
    # Deterministic form of "the replay is the event log": everything listed before is replayed, in order, and the
    # replay equals the REST listing (taken afterwards) of exactly the replayed range - no gaps, no extras.
    assert first, "the app emits startup events, so the replay has something to show"
    assert seen[: len(first)] == [e["seq"] for e in first]
    after = client.get("/api/events", headers={"Authorization": f"Bearer {client.token}"}).json()
    assert seen == [e["seq"] for e in after if e["seq"] <= seen[-1]]
