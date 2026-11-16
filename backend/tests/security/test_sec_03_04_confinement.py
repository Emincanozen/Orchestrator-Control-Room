"""Findings #3 (Claude workspace_write not confined) and #4 (repository-controlled CLI config), argv level.

No CLI is started: commands are built and inspected. Flags were checked against `claude --help` 2.1.322,
`codex exec --help` / `codex exec resume --help` 0.167.1 and `agy --help` 1.5.6.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.providers.antigravity import AntigravityAdapter
from orchestrator.providers.base import ExecutionRequest
from orchestrator.providers.claude import ClaudeAdapter, claude_rule_paths
from orchestrator.providers.codex import CodexAdapter
from orchestrator.providers.process import InvalidRequestError
from orchestrator.security.approvals import permission_escalation_text, workspace_write_meaning


def _req(tmp_path, **kw):
    base = dict(run_id="r", prompt="p", cwd=str(tmp_path / "wt"), permission="workspace_write")
    base.update(kw)
    return ExecutionRequest(**base)


def _val(args, flag):
    return args[args.index(flag) + 1]


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (tmp_path / "wt").mkdir()
    return d


def claude(data_dir, **kw):
    return ClaudeAdapter(data_dir=data_dir, **kw)


# --------------------------------------------------------------------------- Claude: every profile


@pytest.mark.parametrize("permission", ["read_only", "workspace_write"])
@pytest.mark.parametrize("shell", [False, True])
def test_every_claude_run_is_restricted(tmp_path, data_dir, permission, shell):
    a = claude(data_dir, allow_shell=shell).build_command(_req(tmp_path, permission=permission)).args
    for flag in ("--restricted", "--strict-mcp-config", "--disable-slash-commands"):
        assert flag in a, flag
    denied = _val(a, "--disallowedTools").split(",")
    assert {"PowerShell", "WebFetch", "WebSearch"} <= set(denied)
    assert "--setting-sources" not in a and "--dangerously-skip-permissions" not in a
    assert "bypassPermissions" not in " ".join(a)


def test_read_only_tools(tmp_path, data_dir):
    a = claude(data_dir, allow_shell=True).build_command(_req(tmp_path, permission="read_only")).args
    assert _val(a, "--tools") == "Read,Glob,Grep" and _val(a, "--permission-mode") == "plan"
    assert "Bash" in _val(a, "--disallowedTools").split(",")  # even with claude_shell on


def test_workspace_write_has_no_bash_by_default(tmp_path, data_dir):
    a = claude(data_dir).build_command(_req(tmp_path)).args  # allow_shell=None -> config/policies.yaml (false)
    tools = _val(a, "--tools").split(",")
    assert set(tools) == {"Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep"}
    assert _val(a, "--allowedTools") == _val(a, "--tools")
    assert "Bash" in _val(a, "--disallowedTools").split(",")


def test_bash_only_with_claude_shell(tmp_path, data_dir):
    a = claude(data_dir, allow_shell=True).build_command(_req(tmp_path)).args
    assert "Bash" in _val(a, "--tools").split(",") and "Bash" in _val(a, "--allowedTools").split(",")
    assert "Bash" not in _val(a, "--disallowedTools").split(",")


def test_claude_shell_comes_from_yaml_only(tmp_path, data_dir, monkeypatch):
    import orchestrator.config as config

    monkeypatch.setattr(config, "yaml_claude_shell", lambda config_dir=None: True)
    assert ClaudeAdapter(data_dir=data_dir).allow_shell is True

    def boom(config_dir=None):
        raise OSError("unreadable")

    monkeypatch.setattr(config, "yaml_claude_shell", boom)
    assert ClaudeAdapter(data_dir=data_dir).allow_shell is False  # fail closed


def test_settings_deny_rules_cover_the_data_dir(tmp_path, data_dir):
    a = claude(data_dir).build_command(_req(tmp_path)).args
    deny = json.loads(_val(a, "--settings"))["permissions"]["deny"]
    bases = claude_rule_paths(data_dir)
    assert bases and all(b.startswith("//") and "\\" not in b for b in bases)
    for b in bases:
        for tool in ("Read", "Edit"):
            assert f"{tool}({b}/**)" in deny and f"{tool}({b})" in deny


def test_rule_path_syntax():
    import os

    if os.name == "nt":
        assert claude_rule_paths("C:\\Users\\x\\AppData\\Local\\AI-Orchestrator") == [
            "//c/Users/x/AppData/Local/AI-Orchestrator", "//C/Users/x/AppData/Local/AI-Orchestrator"]
    else:
        assert claude_rule_paths("/home/x/.local/share/ai-orchestrator") == ["//home/x/.local/share/ai-orchestrator"]


@pytest.mark.parametrize("rel", ["data", "data/sub", "."])
def test_claude_extra_dir_overlapping_the_data_dir_is_refused(tmp_path, data_dir, rel):
    with pytest.raises(InvalidRequestError):
        claude(data_dir).build_command(_req(tmp_path, extra_dirs=[str(tmp_path / rel)]))


def test_claude_harmless_extra_dir_is_passed(tmp_path, data_dir):
    other = tmp_path / "shared"
    other.mkdir()
    a = claude(data_dir).build_command(_req(tmp_path, extra_dirs=[str(other)])).args
    assert _val(a, "--add-dir") == str(other)


# --------------------------------------------------------------------------- Codex / agy (#4)


@pytest.mark.parametrize("resume", [None, "thread-1"])
def test_codex_ignores_repository_rules_and_hooks(tmp_path, data_dir, resume):
    a = CodexAdapter(data_dir=data_dir, isolate_user_tools=False).build_command(
        _req(tmp_path, resume_session_id=resume)).args
    assert "--ignore-rules" in a
    assert [a[i + 1] for i, x in enumerate(a) if x == "--disable"] == ["hooks"]
    assert "--dangerously-bypass-hook-trust" not in a and "--dangerously-bypass-approvals-and-sandbox" not in a


def test_agy_disables_slash_commands(tmp_path, data_dir):
    a = AntigravityAdapter(data_dir=data_dir).build_command(_req(tmp_path)).args
    assert "--disable-slash-commands" in a and "--dangerously-skip-permissions" not in a


@pytest.mark.parametrize("adapter", [CodexAdapter, AntigravityAdapter])
def test_other_adapters_refuse_extra_dirs_in_the_data_dir(tmp_path, data_dir, adapter):
    with pytest.raises(InvalidRequestError):
        adapter(data_dir=data_dir).build_command(_req(tmp_path, extra_dirs=[str(data_dir)]))


# --------------------------------------------------------------------------- approval text (#3)


def test_claude_text_says_confined_file_tools_without_shell():
    t = workspace_write_meaning("claude")
    assert "--restricted" in t and "no shell" in t and "confined to the task worktree" in t


def test_claude_text_with_shell_never_claims_confinement_for_the_shell():
    t = workspace_write_meaning("claude", claude_shell=True)
    assert "Bash" in t and "NOT confined" in t


def test_codex_and_agy_texts_defer_to_their_own_sandboxes():
    assert "workspace-write sandbox" in workspace_write_meaning("codex")
    agy = workspace_write_meaning("antigravity")
    assert "accept-edits" in agy and "auto-denied" in agy


@pytest.mark.parametrize("provider", ["claude", "codex", "antigravity", "mock"])
@pytest.mark.parametrize("used", ["read_only", "workspace_write"])
def test_escalation_text_never_overclaims(provider, used):
    summary, grant = permission_escalation_text("T", provider, "plan" if used == "read_only" else "execute", used, "x")
    assert grant == ("workspace_write" if used == "read_only" else None)
    assert "unrestricted shell" not in summary  # the old wording implied a shell existed and was restricted
    if provider != "claude":
        assert "confined to the task worktree" not in summary
    summary_shell, _ = permission_escalation_text("T", provider, "execute", used, "x", claude_shell=True)
    if provider == "claude":
        assert "NOT confined" in summary_shell
