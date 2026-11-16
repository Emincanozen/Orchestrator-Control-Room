"""Private Node staging/routing and native npm isolation, never original ACL grants."""

import asyncio
import hashlib
import json
import logging
import os
import subprocess
from pathlib import Path

import pytest

from orchestrator.scheduler import verification
from orchestrator.security import acl, appcontainer, node_runtime, python_runtime, sandbox


@pytest.fixture(autouse=True)
def verification_stage_evidence(caplog):
    caplog.set_level(logging.INFO, logger="orchestrator.scheduler.verification")


def sources(root):
    install = root / "installed-node"
    install.mkdir()
    (install / "node.exe").write_bytes(b"fake-node-for-staging-only")
    package = install / "node_modules/npm"
    for name in ("bin", "lib", "node_modules/dependency"):
        (package / name).mkdir(parents=True)
    (package / "package.json").write_text('{"name":"npm","version":"fixture"}')
    for name in ("npm-cli.js", "npx-cli.js"):
        (package / "bin" / name).write_text("require('../lib/cli.js')")
    (package / "lib/cli.js").write_text("module.exports = () => {}")
    (package / "lib/cache").mkdir()
    (package / "lib/cache/policy.js").write_text("module.exports = 'required-code-not-user-cache'")
    (package / "node_modules/dependency/package.json").write_text('{"main":"index.js"}')
    (package / "node_modules/dependency/index.js").write_text("module.exports = 42")
    for rel in ("npmrc", ".npmrc", "node_modules/dependency/.env", "node_modules/dependency/auth.json"):
        (package / rel).write_text("FAKE-CONFIG-DO-NOT-COPY")
    (install / "npm.cmd").write_text("UNTRUSTED-WRAPPER-MUST-NOT-BE-COPIED")
    return install / "node.exe", package


def snapshot(files):
    result = {}
    for file in files:
        digest = None
        if not file.is_dir():
            with file.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
        result[str(file)] = (digest, acl.read_security(file) if os.name == "nt" else file.stat().st_mode)
    return result


def test_copy_contains_code_and_owned_wrappers_but_no_install_config(tmp_path):
    source, package = sources(tmp_path)
    original = [source.parent, package, *python_runtime._plain_tree(source.parent, skip_cache=False)]
    before = snapshot(original)
    destination = tmp_path / "copy"
    exe = node_runtime.stage_node(destination, source=source, appcontainer_read=False)
    assert exe.read_bytes() == source.read_bytes()
    assert (destination / "node_modules/npm/lib/cli.js").read_bytes() == (package / "lib/cli.js").read_bytes()
    assert (destination / "node_modules/npm/node_modules/dependency/index.js").exists()
    assert (destination / "node_modules/npm/lib/cache/policy.js").is_file()
    assert not (destination / "node_modules/npm/npmrc").exists()
    assert not (destination / "node_modules/npm/.npmrc").exists()
    assert not (destination / "node_modules/npm/node_modules/dependency/.env").exists()
    assert not (destination / "node_modules/npm/node_modules/dependency/auth.json").exists()
    assert "UNTRUSTED-WRAPPER" not in (destination / "npm.cmd").read_text()
    assert "orchestrator-node-bootstrap.cjs" in (destination / "npm.cmd").read_text()
    assert python_runtime.validate_runtime(destination, required_executable="node.exe") == exe
    assert acl.check_private(destination) == []
    assert snapshot(original) == before


@pytest.mark.parametrize("relative", ["../outside", ".. /outside", "x:stream", "CON", "CON .txt", "dir./file"])
def test_shared_stager_refuses_windows_path_aliases_before_creating_anything(tmp_path, relative):
    source = tmp_path / "node.exe"
    source.write_bytes(b"fixture")
    outside = tmp_path / "outside"
    outside.write_bytes(b"original")
    destination = tmp_path / "copy"
    with pytest.raises(python_runtime.RuntimeUnavailable, match="unsafe runtime relative path"):
        python_runtime.stage_runtime_files(destination, [(source, Path("node.exe"))],
            required_executable="node.exe", generated={relative: b"must-not-write"})
    assert outside.read_bytes() == b"original"
    assert not destination.exists()


@pytest.mark.parametrize("name", [".. ", "runtime.", "CON.exe"])
def test_runtime_identity_components_refuse_windows_aliases_before_acquisition(tmp_path, name):
    with pytest.raises(python_runtime.RuntimeUnavailable, match="runtime"):
        python_runtime.RuntimeLease(tmp_path / "not-created", runtime_name=name)
    assert not (tmp_path / "not-created").exists()


def test_existing_destination_and_content_changes_are_refused(tmp_path):
    source, _ = sources(tmp_path)
    destination = tmp_path / "copy"
    exe = node_runtime.stage_node(destination, source=source, appcontainer_read=False)
    before = exe.read_bytes()
    with pytest.raises(python_runtime.RuntimeUnavailable, match="already exists"):
        node_runtime.stage_node(destination, source=source, appcontainer_read=False)
    assert exe.read_bytes() == before
    (destination / "node_modules/npm/lib/cli.js").write_text("modified")
    with pytest.raises(python_runtime.RuntimeUnavailable, match="content changed"):
        python_runtime.validate_runtime(destination, required_executable="node.exe")


def test_reparse_source_even_in_excluded_directory_is_refused(tmp_path):
    source, package = sources(tmp_path)
    target = tmp_path / "outside"
    target.mkdir()
    link = package / "node_modules/dependency/.cache"
    if os.name == "nt":
        subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                       check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)
    with pytest.raises(python_runtime.RuntimeUnavailable, match="link"):
        node_runtime.stage_node(tmp_path / "copy", source=source, appcontainer_read=False)
    assert not (tmp_path / "copy").exists()


@pytest.mark.parametrize("form", ["npm.cmd", "npx.cmd", "npm-cli.js", "node.exe"])
def test_routing_preserves_arguments_and_drops_original_environment(tmp_path, monkeypatch, form):
    source, package = sources(tmp_path)
    monkeypatch.setattr(node_runtime, "installed_node", lambda: source)
    exe = tmp_path / "private/node.exe"
    class Lease:
        def acquire(self):
            return exe
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    cache = tmp_path / "cache"
    argv = ([str(source), str(package / "bin/npm-cli.js"), "test"] if form == "npm-cli.js"
            else [str(source.parent / form), "-e", "process.exit(7)"] if form == "node.exe"
            else [str(source.parent / form), "test"])
    command, env = node_runtime.verification_command(argv, env={"PATH": "public-tools", "NODE_PATH": "secret",
        "NODE_OPTIONS": "--require=secret", "CODEX_HOME": "secret", "CUSTOM_API_KEY": "secret"},
        cwd=str(tmp_path), tmp=scratch, cache=cache, lease=Lease())
    assert command[0] == str(exe)
    assert command[1:5] == ["--preserve-symlinks", "--preserve-symlinks-main",
                            "--require", str(exe.parent / node_runtime.BOOTSTRAP_NAME)]
    assert "secret" not in str(env)
    assert env["PATH"].split(os.pathsep)[0] == str(exe.parent)
    assert env["HOME"] == env["TEMP"] == str(scratch)
    assert env["npm_config_offline"] == "true"
    assert env["npm_config_userconfig"] == str(scratch / "npm-user.npmrc")
    if form.startswith("npx"):
        assert "--offline" in command and "--no-install" in command
    if form == "node.exe":
        assert command[-2:] == ["-e", "process.exit(7)"]
    payload = json.loads(env[node_runtime.ENV_PAYLOAD])
    assert payload["environment"]["NODE_OPTIONS"] == env["NODE_OPTIONS"]


@pytest.mark.skipif(os.name != "nt", reason="native Windows AppContainer")
def test_node_lease_blocks_changes_and_rejects_unsafe_parent(tmp_path):
    source, _ = sources(tmp_path)
    parent = tmp_path / "data/verification-runtimes"
    lease = node_runtime.NodeRuntimeLease(parent, source=source)
    try:
        exe = lease.acquire()
        assert exe.name == "node.exe"
        with pytest.raises(OSError):
            exe.parent.rename(exe.parent.with_name("swapped-node"))
        with pytest.raises(OSError):
            (exe.parent / "node_modules/npm/lib/cli.js").write_text("replace")
    finally:
        lease.close()
    unsafe = tmp_path / "unsafe/verification-runtimes"
    unsafe.mkdir(parents=True)
    before = acl.read_security(unsafe)
    with pytest.raises(python_runtime.RuntimeUnavailable, match="not private and trusted"):
        node_runtime.NodeRuntimeLease(unsafe, source=source).acquire()
    assert acl.read_security(unsafe) == before


@pytest.mark.skipif(os.name != "nt", reason="native Windows verification filter")
def test_npm_cmd_metacharacters_are_still_refused_before_runtime_acquisition(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("cmd metacharacters reached runtime acquisition")
    monkeypatch.setattr(node_runtime.NodeRuntimeLease, "acquire", forbidden)
    result = asyncio.run(verification.run_command(
        [str(node_runtime.installed_node().parent / "npm.cmd"), "test", "&whoami"],
        str(tmp_path), 10, dict(os.environ)))
    assert not result.passed
    assert "shell metacharacters" in result.error


@pytest.mark.skipif(os.name != "nt", reason="native Windows AppContainer")
def test_node_runtime_failure_is_security_failure_without_running_script(tmp_path, monkeypatch):
    task = tmp_path / "task"
    task.mkdir()
    (task / ".git").write_text("gitdir: unused-by-this-test\n")
    (task / "package.json").write_text(json.dumps({"scripts": {
        "test": "node -e \"require('fs').writeFileSync('must-not-run','bad')\""}}))
    def broken(*args, **kwargs):
        raise python_runtime.RuntimeUnavailable("injected Node runtime failure")
    monkeypatch.setattr(node_runtime.NodeRuntimeLease, "acquire", broken)
    result = asyncio.run(verification.run_command(
        [str(node_runtime.installed_node().parent / "npm.cmd"), "test"], str(task), 10, dict(os.environ),
        writable_cwd=True, sandbox_root=tmp_path / "sandbox", tmp_dir=tmp_path / "logs"))
    assert not result.passed and result.error.startswith("sandbox_unavailable:")
    assert "injected Node runtime failure" in result.error
    assert not (task / "must-not-run").exists()


@pytest.mark.skipif(os.name != "nt", reason="native Windows AppContainer")
def test_native_npm_success_failure_hostile_script_and_npx_local_dependency(tmp_path, request):
    source = node_runtime.installed_node()
    package = source.parent / "node_modules/npm"
    originals = [source.parent, package, source, source.parent / "npm.cmd", package / "package.json", package / "bin/npm-cli.js",
                 Path.home(), Path(python_runtime.managed_runtime_parent().anchor)]
    before = snapshot(originals)
    task = tmp_path / "task-a"
    other = tmp_path / "task-b"
    for directory in (task, other):
        directory.mkdir()  # plain: the sandbox identity is granted on the per-command copy only
        (directory / ".git").write_text("gitdir: unused-by-this-test\n")
    outside = tmp_path / "fake-api-token"
    outside.write_text("FAKE-OUTSIDE-SECRET")
    victim = other / "private.txt"
    victim.write_text("FAKE-OTHER-TASK")
    outside_module = tmp_path / "outside-module"
    outside_module.mkdir()
    (outside_module / "probe.js").write_text("require('fs').writeFileSync('must-not-load-module','bad')")
    link = task / "module-link"
    subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside_module)],
                   capture_output=True, check=True)
    request.addfinalizer(lambda: link.rmdir() if link.is_junction() else None)
    (task / "package.json").write_text(json.dumps({"name": "private-native-test", "version": "1.0.0",
        "scripts": {"test": "node hostile.js", "fail": "node -e \"process.exit(7)\""}}))
    (task / "hostile.js").write_text("const fs=require('fs');\n" +
        "for (const p of " + json.dumps([str(outside), str(victim)]) + ") {\n" +
        " for(const operation of [()=>fs.readFileSync(p),()=>fs.appendFileSync(p,'hostile')]) { try { operation();"
        " throw Error('outside access allowed'); } catch(e) { if(!['EPERM','EACCES'].includes(e.code)) throw e; } }\n}\n"
        "for (const key of ['CODEX_HOME','CLAUDE_CONFIG_DIR','CUSTOM_API_KEY','NODE_PATH'])"
        " if(process.env[key]) throw Error('provider environment leaked');\n"
        "try { require('./module-link/probe.js'); throw Error('outside module loaded'); }"
        "catch(e) { if(!['EPERM','EACCES','MODULE_NOT_FOUND'].includes(e.code)) throw e; }\n"
        "fs.writeFileSync('npm-marker','ok');fs.writeFileSync(process.env.TEMP+'/own-temp','ok');\n"
        "console.log('NPM-MARKER-OK');\n")
    def execute(argv):
        return asyncio.run(verification.run_command(argv, str(task), 60, dict(os.environ), writable_cwd=True,
            sandbox_root=tmp_path / "sandbox", tmp_dir=tmp_path / "logs"))
    # Existing aliases are now refused before runtime/permission preparation.
    result = execute([str(source.parent / "npm.cmd"), "test"])
    assert not result.passed and verification.is_security_error(result), result.error or result.output
    assert "unsafe worktree or Git metadata before sandbox preparation" in (result.error or "")
    assert not (task / "npm-marker").exists()
    assert not (task / "must-not-load-module").exists()
    assert link.is_junction()
    link.rmdir()  # Only this fixture's own junction; never recursively remove its target.
    assert not link.exists() and not link.is_junction()
    result = execute([str(source.parent / "npm.cmd"), "test"])
    assert result.passed, result.error or result.output
    assert "NPM-MARKER-OK" in result.output and not (task / "npm-marker").exists()  # the copy only
    assert not (task / "must-not-load-module").exists()
    result = execute([str(source), str(package / "bin/npm-cli.js"), "run", "fail"])
    assert not result.passed and result.exit_code == 7, (result.error, result.output)
    dependency = task / "node_modules/tiny-local"
    dependency.mkdir(parents=True)
    (dependency / "package.json").write_text('{"name":"tiny-local","version":"1.0.0","bin":{"tiny-local":"cli.js"}}')
    (dependency / "cli.js").write_text("require('fs').writeFileSync('npx-local-marker','ok');console.log('NPX-LOCAL-OK')")
    bins = task / "node_modules/.bin"
    bins.mkdir()
    (bins / "tiny-local.cmd").write_text('@ECHO OFF\nnode "%~dp0..\\tiny-local\\cli.js" %*\n')
    result = execute([str(source.parent / "npx.cmd"), "tiny-local"])
    assert result.passed, result.error or result.output
    assert "NPX-LOCAL-OK" in result.output and not (task / "npx-local-marker").exists()
    assert outside.read_text() == "FAKE-OUTSIDE-SECRET"
    assert victim.read_text() == "FAKE-OTHER-TASK"
    assert snapshot(originals) == before
