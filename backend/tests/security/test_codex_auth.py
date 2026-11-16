"""Synthetic subscription input only; never inspect the user's provider configuration."""

import json
import os

import pytest

from orchestrator.security import codex_runtime
from orchestrator.security.codex_auth import (
    MAX_AUTH_BYTES,
    CodexAuthUnavailable,
    read_subscription_auth,
    validate_auth_snapshot,
)


def credentials(tmp_path, content=None):
    home = tmp_path / "synthetic-codex-home"
    home.mkdir()
    raw = json.dumps(content or {"auth_mode": "chatgpt", "tokens": {"access_token": "FAKE-ACCESS", "refresh_token": "FAKE-REFRESH", "id_token": "FAKE-ID"}}).encode()
    (home / "auth.json").write_bytes(raw)
    return home, raw


def test_subscription_snapshot_preserves_original_and_does_not_read_settings(tmp_path):
    home, raw = credentials(tmp_path)
    (home / "config.toml").write_text("INVALID SETTINGS MUST NOT BE READ")
    assert read_subscription_auth({"CODEX_HOME": str(home)}) == raw
    assert (home / "auth.json").read_bytes() == raw
    assert sorted(p.name for p in home.iterdir()) == ["auth.json", "config.toml"]
    assert codex_runtime._auth_identity(raw)  # Reader and runtime must accept the same successful snapshot.


@pytest.mark.parametrize("content", [
    {"auth_mode": "api_key", "OPENAI_API_KEY": "FAKE-SECRET"},
    {"auth_mode": "unexpected", "tokens": {"access_token": "FAKE-SECRET"}},
    {"auth_mode": "chatgpt", "tokens": {"access_token": ""}},
    {"auth_mode": "chatgpt", "tokens": []},
])
def test_invalid_or_metered_auth_is_rejected_without_secret_in_error(tmp_path, content):
    home, _ = credentials(tmp_path, content)
    with pytest.raises(CodexAuthUnavailable) as caught:
        read_subscription_auth({"CODEX_HOME": str(home)})
    assert "FAKE-SECRET" not in str(caught.value)
    assert str(home) not in str(caught.value)


def test_oversized_source_is_bounded_and_unchanged(tmp_path):
    home, _ = credentials(tmp_path)
    raw = b"x" * (MAX_AUTH_BYTES + 1)
    (home / "auth.json").write_bytes(raw)
    with pytest.raises(CodexAuthUnavailable):
        read_subscription_auth({"CODEX_HOME": str(home)})
    assert (home / "auth.json").stat().st_size == len(raw)


def test_hardlinked_source_is_refused(tmp_path):
    home, _ = credentials(tmp_path)
    os.link(home / "auth.json", tmp_path / "outside-copy")
    with pytest.raises(CodexAuthUnavailable):
        read_subscription_auth({"CODEX_HOME": str(home)})


def test_api_key_environment_and_relative_home_are_rejected_before_open():
    with pytest.raises(CodexAuthUnavailable, match="API-key"):
        read_subscription_auth({"OPENAI_API_KEY": "FAKE-SECRET", "CODEX_HOME": "does-not-exist"})
    with pytest.raises(CodexAuthUnavailable, match="absolute"):
        read_subscription_auth({"CODEX_HOME": "relative-home"})


def test_windows_environment_casing_and_ambiguous_roots(tmp_path):
    home, raw = credentials(tmp_path)
    assert read_subscription_auth({"Codex_Home": str(home)}) == raw
    with pytest.raises(CodexAuthUnavailable, match="Ambiguous"):
        read_subscription_auth({"CODEX_HOME": str(home), "codex_home": "DIFFERENT-HOME"})
    with pytest.raises(CodexAuthUnavailable, match="API-key"):
        read_subscription_auth({"openai_api_key": "FAKE-SECRET"})


def test_deeply_nested_auth_fails_with_generic_error(tmp_path):
    home, _ = credentials(tmp_path)
    (home / "auth.json").write_bytes(b"[" * 2000 + b"0" + b"]" * 2000)
    with pytest.raises(CodexAuthUnavailable, match="unavailable or unsafe"):
        read_subscription_auth({"CODEX_HOME": str(home)})


def test_partial_token_snapshot_is_rejected_by_reader(tmp_path):
    home, _ = credentials(tmp_path, {"auth_mode": "chatgpt", "tokens": {"access_token": "FAKE-ACCESS"}})
    with pytest.raises(CodexAuthUnavailable):
        read_subscription_auth({"CODEX_HOME": str(home)})


def test_malformed_json_does_not_retain_parser_context_or_raw_in_error_frame(tmp_path):
    home, _ = credentials(tmp_path)
    secret = b'FAKE-CREDENTIAL-IN-BROKEN-JSON'
    (home / "auth.json").write_bytes(b'{"tokens":{"access_token":"' + secret)
    with pytest.raises(CodexAuthUnavailable) as caught:
        read_subscription_auth({"CODEX_HOME": str(home)})
    assert caught.value.__context__ is None
    tb = caught.value.__traceback__
    while tb:
        if tb.tb_frame.f_code.co_name == "read_subscription_auth":
            for value in tb.tb_frame.f_locals.values():
                assert secret.decode() not in repr(value)
        tb = tb.tb_next


@pytest.mark.parametrize("subject", [validate_auth_snapshot, codex_runtime._auth_identity])
def test_semantic_auth_failure_clears_validator_and_wrapper_frames(subject):
    secret = "FAKE-SEMANTIC-CREDENTIAL"
    raw = json.dumps({"auth_mode": "api_key", "tokens": {"access_token": secret}}).encode()
    with pytest.raises((CodexAuthUnavailable, codex_runtime.CodexRuntimeUnavailable)) as caught:
        subject(raw)
    assert caught.value.__context__ is None
    tb = caught.value.__traceback__
    while tb:
        if tb.tb_frame.f_code.co_name in {"validate_auth_snapshot", "_auth_identity"}:
            assert all(secret not in repr(value) for value in tb.tb_frame.f_locals.values())
        tb = tb.tb_next
