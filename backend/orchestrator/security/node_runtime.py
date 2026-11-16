"""Lease a private copy of installed Node/npm code for AppContainer verification.

No installer, registry request, original shim execution, or original ACL change.
The only shared package data is immutable code and bundled dependency assets.
User/global npm configuration, authentication, profiles and caches are excluded.
"""

from __future__ import annotations

import json
import os
import stat
from contextlib import ExitStack
from pathlib import Path
from typing import Sequence

from orchestrator.security import paths, python_runtime

RuntimeUnavailable = python_runtime.RuntimeUnavailable
ENV_PAYLOAD = "AI_ORCHESTRATOR_NODE_ENV"
BOOTSTRAP_NAME = "orchestrator-node-bootstrap.cjs"
_NPM = Path("node_modules/npm")

_BOOTSTRAP = r'''"use strict";
const path = require("node:path");
const payload = process.env.AI_ORCHESTRATOR_NODE_ENV;
if (!payload) throw new Error("private Node runtime requires its task environment");
const { environment, cwd } = JSON.parse(payload);
if (!environment || !path.isAbsolute(cwd)) throw new Error("invalid private Node environment");
const previous = { ...process.env };
for (const key of Object.keys(process.env)) delete process.env[key];
Object.assign(process.env, environment);
// npm adds lifecycle metadata and project .bin directories for already-installed
// local tools. Preserve those only; profile/config/credential pointers stay fixed.
const lifecycle = new Set(["npm_lifecycle_event", "npm_lifecycle_script", "npm_package_json",
  "npm_command", "npm_execpath", "npm_node_execpath", "INIT_CWD", "NODE_ENV",
  "FORCE_COLOR", "NO_COLOR"]);
for (const [key, value] of Object.entries(previous)) {
  if (lifecycle.has(key)) process.env[key] = value;
}
const within = candidate => {
  const relative = path.relative(cwd, path.resolve(candidate));
  return relative === "" || (!path.isAbsolute(relative) && relative !== ".." && !relative.startsWith(".." + path.sep));
};
const baseline = environment.PATH.split(path.delimiter);
const baselineSet = new Set(baseline.map(p => path.resolve(p).toLowerCase()));
const previousPath = previous.PATH || previous.Path || "";
const localBins = previousPath.split(path.delimiter).filter(p => p && within(p));
const publicBins = previousPath.split(path.delimiter).filter(p => p && baselineSet.has(path.resolve(p).toLowerCase()));
process.env.PATH = [...new Set([path.dirname(process.execPath), ...localBins, ...publicBins, ...baseline])].join(path.delimiter);
process.env.AI_ORCHESTRATOR_NODE_ENV = payload;
'''


def installed_node() -> Path:
    """Select the installed Program Files runtime, never a project's PATH shim."""
    candidates = [Path(os.environ.get(name, default)) / "nodejs/node.exe"
                  for name, default in (("ProgramFiles", "C:/Program Files"),
                                        ("ProgramFiles(x86)", "C:/Program Files (x86)"))]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.absolute()
    raise RuntimeUnavailable("installed Program Files Node runtime is unavailable")


def _plain_file(path: Path) -> None:
    info = path.lstat()
    if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
        raise RuntimeUnavailable(f"Node source is a reparse point: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeUnavailable(f"Node source is not a regular file: {path}")


def _package_code(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    # Enumerate all entries first (including excluded ones), so source links
    # cannot be hidden behind an exclusion. Never read these excluded contents.
    # These roots are installed package code, not user cache directories. Names
    # such as make-fetch-happen/lib/cache contain required JavaScript modules.
    if any(part.startswith(".") for part in rel.parts):
        return False
    if path.name.casefold() in {"npmrc", "credentials", "credentials.json", "auth.json"}:
        return False
    return path.suffix.casefold() in {".js", ".cjs", ".mjs", ".json", ".node", ".wasm",
                                     ".py", ".gyp", ".gypi", ".h", ".c", ".cc", ".cpp",
                                     ".sh", ".cmd", ".bat", ".ps1"}


def stage_node(destination: Path, *, source: Path | None = None, appcontainer_read: bool = True) -> Path:
    """Copy only installed Node and bundled npm code into a NEW private tree."""
    source = Path(source or installed_node()).absolute()
    destination = Path(destination).absolute()
    package = source.parent / _NPM
    if source.parent == destination or source.parent in destination.parents or destination in source.parents:
        raise RuntimeUnavailable("Node destination overlaps the original installation")
    try:
        with ExitStack() as held:
            held.enter_context(paths.directory_guard(source.parent))
            held.enter_context(paths.directory_guard(package))
            held.enter_context(paths.directory_guard(destination.parent))
            _plain_file(source)
            files = [(source, Path("node.exe"))]
            for name in ("bin", "lib", "node_modules"):
                root = package / name
                held.enter_context(paths.directory_guard(root))
                for file in python_runtime._plain_tree(root, skip_cache=False):
                    if _package_code(file, root):
                        files.append((file, _NPM / name / file.relative_to(root)))
            for name in ("package.json", "index.js"):
                file = package / name
                if name == "index.js" and not os.path.lexists(file):
                    continue
                _plain_file(file)
                files.append((file, _NPM / name))
            required = {_NPM / "bin/npm-cli.js", _NPM / "bin/npx-cli.js", _NPM / "lib/cli.js"}
            if not required.issubset({rel for _, rel in files}):
                raise RuntimeUnavailable("installed npm package is missing required CLI code")
            generated = {BOOTSTRAP_NAME: _BOOTSTRAP.encode("utf-8")}
            for command in ("npm", "npx"):
                # Orchestrator-owned wrappers only, for scripts already inside
                # the container. No original %~dp0/global-prefix wrapper runs.
                generated[command + ".cmd"] = (
                    '@ECHO OFF\r\n"%~dp0node.exe" --require "%~dp0' + BOOTSTRAP_NAME +
                    '" "%~dp0node_modules\\npm\\bin\\' + command + '-cli.js" %*\r\n').encode("utf-8")
            return python_runtime.stage_runtime_files(destination, files, required_executable="node.exe",
                appcontainer_read=appcontainer_read, generated=generated)
    except (OSError, paths.PathPolicyError) as exc:
        raise RuntimeUnavailable(f"unsafe or inaccessible Node runtime source: {exc}") from exc


class NodeRuntimeLease(python_runtime.RuntimeLease):
    def __init__(self, parent: Path | None = None, *, source: Path | None = None):
        super().__init__(parent, runtime_name="node-v2", required_executable="node.exe",
                         stage=lambda destination: stage_node(destination, source=source))


def command_kind(argv: Sequence[str]) -> str | None:
    if not argv:
        return None
    name = Path(argv[0]).name.casefold()
    if name in {"npm", "npm.cmd", "npx", "npx.cmd"}:
        return name.split(".", 1)[0]
    if name not in {"node", "node.exe"}:
        return None
    if len(argv) > 1 and Path(argv[1]).name.casefold() in {"npm-cli.js", "npx-cli.js"}:
        expected = installed_node().parent / _NPM / "bin" / Path(argv[1]).name
        if os.path.normcase(os.path.abspath(argv[1])) == os.path.normcase(str(expected)):
            return "npm" if Path(argv[1]).name.casefold() == "npm-cli.js" else "npx"
    return "node"


def _task_environment(env: dict[str, str], *, exe: Path, cwd: str, tmp: Path, cache: Path) -> dict[str, str]:
    allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE",
               "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "AI_ORCHESTRATOR_DEPTH"}
    clean = {k.upper(): v for k, v in env.items() if k.upper() in allowed}
    clean.update({key: str(tmp) for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "TMPDIR")})
    clean["HOMEDRIVE"], clean["HOMEPATH"] = os.path.splitdrive(str(tmp))
    clean["PATH"] = str(exe.parent) + os.pathsep + clean.get("PATH", "")
    with paths.directory_guard(tmp):
        for name in ("npm-user.npmrc", "npm-global.npmrc"):
            with (tmp / name).open("xb"):
                pass
    clean.update({"npm_config_userconfig": str(tmp / "npm-user.npmrc"),
                  "npm_config_globalconfig": str(tmp / "npm-global.npmrc"),
                  "npm_config_cache": str(cache / "npm"), "npm_config_prefix": str(cache / "npm-prefix"),
                  "npm_config_offline": "true", "npm_config_yes": "false",
                  "npm_config_audit": "false", "npm_config_fund": "false",
                  "npm_config_update_notifier": "false", "NODE_COMPILE_CACHE": str(cache / "node-compile"),
                  "NODE_OPTIONS": "--preserve-symlinks --preserve-symlinks-main --require=" +
                      json.dumps((exe.parent / BOOTSTRAP_NAME).as_posix(), ensure_ascii=False)})
    clean[ENV_PAYLOAD] = json.dumps({"environment": dict(clean), "cwd": os.path.abspath(cwd)})
    return clean


def verification_command(argv: Sequence[str], *, env: dict[str, str], cwd: str, tmp: Path, cache: Path,
                         lease: NodeRuntimeLease) -> tuple[list[str], dict[str, str]]:
    kind = command_kind(argv)
    if kind is None:
        raise RuntimeUnavailable("not a Node verification command")
    exe = lease.acquire()
    clean = _task_environment(env, exe=exe, cwd=cwd, tmp=tmp, cache=cache)
    args = list(argv[1:])
    if kind in {"npm", "npx"}:
        if Path(argv[0]).name.casefold() in {"node", "node.exe"}:
            args = args[1:]
        entry = str(exe.parent / _NPM / "bin" / (kind + "-cli.js"))
        if kind == "npx":
            args = ["--offline", "--no-install", *args]
        args = [entry, *args]
    # The leased copy has no links and its complete ancestor chain is pinned.
    # Node's canonicalization probes the ungranted drive root even when the
    # final copied file is readable. Skip that loader probe, never grant root
    # access. Kernel AppContainer access checks still apply to every file open,
    # including hostile project links; Medium Git separately refuses all links.
    return [str(exe), "--preserve-symlinks", "--preserve-symlinks-main",
            "--require", str(exe.parent / BOOTSTRAP_NAME), *args], clean
