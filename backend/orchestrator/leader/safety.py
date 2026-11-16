"""Safety filter for leader-PROPOSED verification.

A model's proposal is data: it becomes a task's verification only if it passes this filter. Two kinds of
proposal exist:

* free commands (`verify_commands`: command lines) and `cmd` entries of structured checks (argv lists);
  both are allowlisted test/lint/build entry points plus a READ-ONLY git subset, validated argument by
  argument (no shell metacharacters, no path outside the worktree, no code-execution flags);
* structured checks (`checks`): `file_exists` (+`contains`), `file_absent`, `file_unchanged`, `json_valid`
  and `cmd`. File checks are implemented by the verifier (no process is started), so they only need a
  relative, in-worktree path.

Anything else is returned as `unsafe` with a reason and goes through a human approval instead (never
auto-run).
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from orchestrator.scheduler.verification import CommandSpecError, split_command
from orchestrator.security.paths import PathPolicyError, resolve_inside

MAX_COMMANDS = 8
MAX_COMMAND_CHARS = 500
MAX_CHECKS = 12
MAX_CONTAINS_CHARS = 1000
MAX_PATH_CHARS = 300
_META = re.compile(r"[|&;<>`\n\r]|\$\(|\$\{|%[A-Za-z_]+%|\^")

PYTHON_NAMES = {"python", "python3", "py"}
PYTHON_MODULES = {"pytest", "unittest", "compileall", "py_compile"}
NODE_PMS = {"npm", "pnpm", "yarn"}
NODE_SCRIPTS = {"test", "lint", "build", "typecheck", "check"}
GIT_READONLY = ("diff --exit-code [paths]", "status --porcelain", "ls-files [paths]")
FILE_CHECK_KEYS = ("file_exists", "file_absent", "file_unchanged", "json_valid")
CHECK_KEYS = (*FILE_CHECK_KEYS, "cmd")


@dataclass
class Assessment:
    safe: list[Any]  # command lines (str) and/or structured check dicts
    unsafe: list[dict[str, str]]  # {"command": ..., "reason": ...}


def _exe(argv0: str) -> str:
    name = os.path.basename(argv0.replace("\\", "/")).lower()
    for suffix in (".exe", ".cmd"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name


def argv0_problem(argv0: str) -> Optional[str]:
    """argv[0] must be a bare command name resolved through PATH: no directory, no drive, only .exe/.cmd or no extension."""
    if not argv0 or any(c in argv0 for c in "/\\:") or argv0 != argv0.strip() or argv0.endswith("."):
        return f"executable {argv0!r} must be a bare command name resolved through PATH"
    ext = os.path.splitext(argv0)[1].lower()
    if ext not in ("", ".exe", ".cmd"):
        return f"executable {argv0!r}: only .exe/.cmd or no extension is accepted"
    return None


def _pathlike(value: str) -> bool:
    return any(c in value for c in "/\\:") or ".." in value or value.startswith("~")


def _path_problem(value: str, roots: list[str]) -> Optional[str]:
    """None when `value` (a path-looking argument) resolves inside one of the roots."""
    if not _pathlike(value):
        return None
    if value.startswith("~") or not roots:
        return f"argument {value!r} points outside the project/worktree"
    for root in roots:
        try:
            resolve_inside(value, [root], base=root)
            return None
        except PathPolicyError:
            continue
    return f"argument {value!r} points outside the project/worktree"


def _positional(value: str, roots: list[str]) -> Optional[str]:
    """A positional argument: never a flag; a path-looking one must stay inside the worktree."""
    if value.startswith("-"):
        return f"flag {value!r} is not allowed here"
    if not value:
        return "empty argument"
    return _path_problem(value, roots)


def _node_id(value: str, roots: list[str]) -> Optional[str]:
    """pytest selector `path/to/test.py::Class::test`: the path part is validated, the node part is not a path."""
    if value.startswith("-"):
        return f"flag {value!r} is not allowed here"
    head = value.split("::", 1)[0]
    return _path_problem(head, roots) if head else "empty test path"


_PYTEST_FLAGS = {"-q", "-qq", "-v", "-vv", "-x", "-s", "--lf", "--ff", "--co", "--collect-only", "--no-header", "--strict-markers", "-ra", "-rA"}
_PYTEST_PATTERNS = (re.compile(r"^--maxfail=\d{1,4}$"), re.compile(r"^--tb=(short|long|line|no|auto|native)$"))
_PYTEST_VALUE_FLAGS = {"-k", "-m"}  # followed by a test-selection expression
_EXPR = re.compile(r"^[\w .()\-:\[\]]{1,200}$")


def _pytest_args(args: list[str], roots: list[str]) -> Optional[str]:
    i = 0
    while i < len(args):
        a = args[i]
        if a in _PYTEST_FLAGS or any(p.match(a) for p in _PYTEST_PATTERNS):
            i += 1
        elif a in _PYTEST_VALUE_FLAGS:
            if i + 1 >= len(args) or not _EXPR.match(args[i + 1]):
                return f"{a} needs a plain test-selection expression"
            i += 2
        elif a.startswith("-"):
            return f"pytest flag {a!r} is not on the argument allowlist"
        else:
            why = _node_id(a, roots)
            if why:
                return why
            i += 1
    return None


def _positional_only(args: list[str], roots: list[str], what: str) -> Optional[str]:
    for a in args:
        why = _positional(a, roots)
        if why:
            return f"{what}: {why}"
    return None


def _python_args(args: list[str], roots: list[str]) -> Optional[str]:
    if len(args) < 2 or args[0] != "-m" or args[1] not in PYTHON_MODULES:
        return f"python is only allowed as '-m {'|'.join(sorted(PYTHON_MODULES))}'"
    mod, rest = args[1], args[2:]
    if mod == "pytest":
        return _pytest_args(rest, roots)
    if mod == "unittest":
        return _positional_only([a for a in rest if a not in ("-v", "-q", "-f", "-b", "discover")], roots, "unittest")
    return _positional_only([a for a in rest if a != "-q"], roots, mod)


def _node_args(exe: str, args: list[str]) -> Optional[str]:
    if args == ["test"]:
        return None
    if len(args) == 2 and args[0] == "run" and args[1] in NODE_SCRIPTS and re.match(r"^[\w:-]+$", args[1]):
        return None
    return f"{exe} is only allowed as exactly 'test' or 'run {'|'.join(sorted(NODE_SCRIPTS))}' (no extra flags)"


def _go_args(args: list[str], roots: list[str]) -> Optional[str]:
    if not args or args[0] not in ("test", "vet", "build"):
        return "go is only allowed as test/vet/build"
    sub, rest, i = args[0], args[1:], 0
    while i < len(rest):
        a = rest[i]
        if sub == "test" and a in ("-v", "-short", "-race"):
            i += 1
        elif sub == "test" and re.match(r"^-count=\d{1,3}$", a):
            i += 1
        elif sub == "test" and a == "-run":
            if i + 1 >= len(rest) or not re.match(r"^[\w.^()\[\]/ -]{1,120}$", rest[i + 1]) or rest[i + 1].startswith("-"):
                return "go test -run needs a plain regular expression"
            i += 2
        elif a.startswith("-"):
            return f"go {sub} flag {a!r} is not on the argument allowlist"
        else:
            why = _positional(a, roots)
            if why:
                return why
            i += 1
    return None


def _cargo_args(args: list[str]) -> Optional[str]:
    if not args or args[0] not in ("test", "check", "build", "clippy"):
        return "cargo is only allowed as test/check/build/clippy"
    for a in args[1:]:
        if a in ("--lib", "--tests", "--all-targets", "--quiet", "-q"):
            continue
        if a.startswith("-") or not re.match(r"^[\w:]{1,100}$", a):
            return f"cargo flag/argument {a!r} is not on the argument allowlist"
    return None


def _npx_args(args: list[str], roots: list[str]) -> Optional[str]:
    rest = args[1:] if args[:1] == ["--no-install"] else args
    if rest[:1] == ["vitest"]:
        tail = rest[1:]
        return _positional_only(tail[1:] if tail[:1] == ["run"] else tail, roots, "vitest")
    if rest[:2] == ["playwright", "test"]:
        return _positional_only(rest[2:], roots, "playwright test")
    return "npx is only allowed as 'vitest [run] [paths]' or 'playwright test [paths]' (no package installs)"


def _git_allowed(args: list[str], roots: list[str]) -> Optional[str]:
    if args[:2] == ["diff", "--exit-code"]:
        return _positional_only(args[2:], roots, "git diff")
    if args == ["status", "--porcelain"]:
        return None
    if args[:1] == ["ls-files"]:
        return _positional_only(args[1:], roots, "git ls-files")
    return f"git is only allowed as: {', '.join('git ' + g for g in GIT_READONLY)}"


def _allowed_argv(argv: list[str], roots: list[str]) -> Optional[str]:
    """Default-deny, per-tool argument allowlists. None when the argv is accepted, else the reason it is not."""
    if not argv:
        return "empty command"
    bad = argv0_problem(argv[0])
    if bad:
        return bad
    exe, args = _exe(argv[0]), argv[1:]
    if exe == "pytest":
        return _pytest_args(args, roots)
    if exe in PYTHON_NAMES:
        return _python_args(args, roots)
    if exe == "uv":
        rest = args[1:] if args and args[0] == "run" else None
        if rest is None:
            return "uv is only allowed as 'uv run <allowlisted command>'"
        if rest and rest[0].startswith("-"):  # uv run options (--project x, --with y ...) are refused
            return "uv run options are not allowed"
        if not rest:
            return "uv run needs a command"
        if argv0_problem(rest[0]) is None and _exe(rest[0]) not in {"pytest", *PYTHON_NAMES}:
            return "uv run is only allowed with pytest or python -m <test module>"
        return _allowed_argv(rest, roots)
    if exe in NODE_PMS:
        return _node_args(exe, args)
    if exe == "npx":
        return _npx_args(args, roots)
    if exe == "node":
        if args[:1] != ["--test"]:
            return "node is only allowed as 'node --test [paths]'"
        return _positional_only(args[1:], roots, "node --test")
    if exe == "cargo":
        return _cargo_args(args)
    if exe == "go":
        return _go_args(args, roots)
    if exe == "dotnet":
        if args[:1] != ["test"]:
            return "dotnet is only allowed as 'dotnet test [path]'"
        return _positional_only(args[1:], roots, "dotnet test")
    if exe == "git":
        return _git_allowed(args, roots)
    return f"executable {exe!r} is not on the verify-command allowlist"


def is_test_command(argv: list[str]) -> bool:
    """A real test run (counts as a POSITIVE check); lint/build/vet and read-only git forms do not."""
    if not argv:
        return False
    exe, args = _exe(argv[0]), argv[1:]
    if exe == "pytest":
        return True
    if exe in PYTHON_NAMES:
        return args[:2] in (["-m", "pytest"], ["-m", "unittest"])
    if exe == "uv":
        return is_test_command(args[1:]) if args[:1] == ["run"] else False
    if exe in NODE_PMS:
        return args == ["test"] or args == ["run", "test"]
    if exe == "npx":
        rest = args[1:] if args[:1] == ["--no-install"] else args
        return rest[:1] == ["vitest"] or rest[:2] == ["playwright", "test"]
    if exe == "node":
        return args[:1] == ["--test"]
    if exe in ("go", "cargo", "dotnet"):
        return args[:1] == ["test"]
    return False


def is_positive_check(spec: Any) -> bool:
    """True when a (safe) proposal item verifies the deliverable: file_exists (+contains), json_valid, or a test
    command. file_absent / file_unchanged / git read-only forms may accompany but never suffice alone."""
    if isinstance(spec, dict):
        if spec.get("file_exists") is not None or spec.get("json_valid") is not None:
            return True
        cmd = spec.get("cmd")
        return isinstance(cmd, list) and is_test_command(cmd)
    if isinstance(spec, str):
        try:
            return is_test_command(split_command(spec))
        except CommandSpecError:
            return False
    return isinstance(spec, list) and is_test_command(spec)


def assess_command(spec: Any, roots: list[str]) -> Optional[str]:
    """None = safe; otherwise the reason it needs approval. `spec` is a command line (str) or argv list."""
    if isinstance(spec, str):
        if len(spec) > MAX_COMMAND_CHARS:
            return "command too long"
        if _META.search(spec):
            return "contains shell metacharacters"
        try:
            argv = split_command(spec)
        except CommandSpecError as exc:
            return f"not a plain command line: {exc}"
    elif isinstance(spec, list) and spec and all(isinstance(a, str) for a in spec):
        argv = list(spec)
        if sum(len(a) for a in argv) > MAX_COMMAND_CHARS:
            return "command too long"
        if any(_META.search(a) for a in argv):
            return "contains shell metacharacters"
    else:
        return "unsupported command form (use a plain command line or an argv list)"
    return _allowed_argv(argv, [r for r in roots if r])


def assess_verify_commands(commands: Iterable[Any], roots: Iterable[str]) -> Assessment:
    """Split proposed commands into safe (adoptable as-is) and unsafe (need approval)."""
    root_list = [r for r in roots if r]
    safe: list[Any] = []
    unsafe: list[dict[str, str]] = []
    items = list(commands)
    for i, spec in enumerate(items):
        text = spec if isinstance(spec, str) else " ".join(map(str, spec)) if isinstance(spec, list) else str(spec)
        if i >= MAX_COMMANDS:
            unsafe.append({"command": text[:MAX_COMMAND_CHARS], "reason": f"more than {MAX_COMMANDS} commands proposed"})
            continue
        why = assess_command(spec, root_list)
        if why is None:
            safe.append(spec if isinstance(spec, str) else text)
        else:
            unsafe.append({"command": text[:MAX_COMMAND_CHARS], "reason": why})
    return Assessment(safe=safe, unsafe=unsafe)


# ---------------------------------------------------------------------------- structured checks
def check_path_problem(raw: Any) -> Optional[str]:
    """Why `raw` is not an acceptable check path (it must be relative and stay inside the worktree)."""
    if not isinstance(raw, str) or not raw.strip():
        return "path must be a non-empty string"
    if len(raw) > MAX_PATH_CHARS:
        return "path too long"
    if "\x00" in raw or _META.search(raw):
        return "path contains forbidden characters"
    norm = raw.replace("\\", "/")
    if norm.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", norm):
        return "path must be relative to the worktree"
    if ":" in norm:
        return "path contains ':'"
    if ".." in norm.split("/"):
        return "path must not contain '..'"
    return None


def assess_check(check: Any, roots: list[str]) -> Optional[str]:
    """None = the structured check is safe to adopt; otherwise the reason it needs approval."""
    if not isinstance(check, dict):
        return "a check must be an object"
    kinds = [k for k in CHECK_KEYS if check.get(k) is not None]
    if len(kinds) != 1:
        return f"a check must use exactly one of {', '.join(CHECK_KEYS)}"
    extra = set(check) - set(CHECK_KEYS) - {"contains"}
    if extra:
        return f"unknown check keys: {sorted(extra)}"
    kind = kinds[0]
    contains = check.get("contains")
    if contains is not None:
        if kind != "file_exists":
            return "contains is only valid together with file_exists"
        if not isinstance(contains, str) or len(contains) > MAX_CONTAINS_CHARS:
            return f"contains must be a string of at most {MAX_CONTAINS_CHARS} characters"
    if kind == "cmd":
        cmd = check["cmd"]
        if not isinstance(cmd, list):
            return "cmd must be an argv list (shell strings are not accepted)"
        return assess_command(cmd, roots)
    return check_path_problem(check[kind])


def clean_check(check: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in check.items() if v is not None}


def describe_check(check: Any) -> str:
    if isinstance(check, dict):
        return ", ".join(f"{k}={v!r}" for k, v in clean_check(check).items())[:MAX_COMMAND_CHARS]
    return str(check)[:MAX_COMMAND_CHARS]


def assess_checks(checks: Iterable[Any], roots: Iterable[str]) -> Assessment:
    root_list = [r for r in roots if r]
    safe: list[Any] = []
    unsafe: list[dict[str, str]] = []
    for i, check in enumerate(list(checks)):
        cleaned = clean_check(check) if isinstance(check, dict) else check
        if i >= MAX_CHECKS:
            unsafe.append({"command": describe_check(cleaned), "reason": f"more than {MAX_CHECKS} checks proposed"})
            continue
        why = assess_check(cleaned, root_list)
        if why is None:
            safe.append(cleaned)
        else:
            unsafe.append({"command": describe_check(cleaned), "reason": why})
    return Assessment(safe=safe, unsafe=unsafe)


def assess_proposal(commands: Iterable[Any], checks: Iterable[Any], roots: Iterable[str]) -> tuple[list[Any], Assessment]:
    """(everything proposed, assessment over structured checks followed by free commands). Use `has_positive` on
    `assessment.safe` to decide whether the proposal may be adopted without a human."""
    root_list = [r for r in roots if r]
    checks = [clean_check(c) if isinstance(c, dict) else c for c in checks]
    commands = list(commands)
    a_chk = assess_checks(checks, root_list)
    a_cmd = assess_verify_commands(commands, root_list)
    return [*checks, *commands], Assessment(safe=[*a_chk.safe, *a_cmd.safe], unsafe=[*a_chk.unsafe, *a_cmd.unsafe])


def has_positive(items: Iterable[Any]) -> bool:
    """At least one item that verifies the deliverable itself (see `is_positive_check`)."""
    return any(is_positive_check(i) for i in items)


# ---------------------------------------------------------------------------- prompt block
_WINDOWS_STUB = "windowsapps"


def _which(name: str) -> Optional[str]:
    found = shutil.which(name)
    if found and name.startswith("python") and _WINDOWS_STUB in found.lower().replace("\\", "/"):
        return None  # the Microsoft Store launcher stub, not a real interpreter
    return found


def available_executables() -> dict[str, bool]:
    names = ["uv", "py", "python", "pytest", "npm", "npm.cmd", "npx.cmd", "node", "go", "cargo", "dotnet", "git"]
    return {n: _which(n) is not None for n in names}


def verify_prompt_block() -> str:
    """The verification instructions both leader prompts carry. Built at call time from what is really on PATH."""
    have = available_executables()
    present = [n for n, ok in have.items() if ok]
    absent = [n for n, ok in have.items() if not ok]
    windows = os.name == "nt"
    lines = [
        "## How to propose verification (read carefully)",
        "Prefer STRUCTURED `checks`; they are checked by the orchestrator itself and need no approval:",
        '- {"file_exists": "<relative path>"} or {"file_exists": "<relative path>", "contains": "<text>"}',
        '- {"file_absent": "<relative path>"}',
        '- {"file_unchanged": "<relative path>"}  (compared with the task\'s base commit via git)',
        '- {"json_valid": "<relative path>"}',
        '- {"cmd": ["exe", "arg", ...]}  (argv LIST form only)',
        "Paths are relative to the task worktree: no absolute paths, no `..`, no `~`.",
        "In the JSON reply give every check object all of the keys file_exists, contains, file_absent, file_unchanged, "
        "json_valid and cmd, using null for the ones you do not use.",
        "A `cmd` (and every entry of verify_commands) is accepted ONLY when it is one of: "
        "`pytest ...`; `uv run pytest ...`; `npm|npm.cmd test` or `npm run test|lint|build|typecheck|check`; "
        "`npx.cmd vitest [run]`; `npx.cmd playwright test`; `node --test`; `go test|vet|build`; "
        "`cargo test|check|build|clippy`; `dotnet test`; `python -m pytest|unittest|compileall|py_compile`; and the read-only git forms "
        "`git diff --exit-code [paths]`, `git status --porcelain`, `git ls-files [paths]` (no other git flags).",
        "NOT accepted (they go to a human approval and delay the task): shell strings, pipes, redirects, `&&`, `python -c`, "
        "`node -e`, any `-c/-e/--eval` flag, absolute paths outside the worktree, any other executable.",
        "To check that a file exists, contains text, is absent or is unchanged use a structured check, not a command. "
        "Use `verify_commands` only for real test/lint/build runs; leave it empty otherwise.",
        f"This machine is {'Windows' if windows else os.name}. Executables found on PATH: {', '.join(present) or '(none)'}. "
        f"Not available: {', '.join(absent) or '(none)'}."
        + (" `python` is NOT on PATH here unless listed above; use `uv run pytest` or `uv run python -m pytest`." if windows and not have.get("python") else ""),
    ]
    return "\n".join(lines) + "\n"
