"""Source discovery for the indexer: project files (globs, .gitignore via git, caps, secret/binary
skips, path policy) and cheap source-code metadata extraction. Read-only on the filesystem."""

from __future__ import annotations

import ast
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from orchestrator.gitops.git import GitError, _run as _git_run, git_prefix, target_args
from orchestrator.security import paths as path_policy
from orchestrator.security.paths import PathPolicyError, resolve_inside

DEFAULT_INCLUDE_GLOBS = ["README*", "docs/**/*.md", "CLAUDE.md", "AGENTS.md"]
DOC_EXTENSIONS = {".md", ".markdown", ".txt", ".rst", ""}
MAX_DOC_BYTES = 200_000
MAX_DOC_FILES = 500
MAX_CODE_BYTES = 1_000_000
MAX_CODE_FILES = 3000
MAX_SYMBOLS = 60

_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".mypy_cache", ".pytest_cache", ".idea"}
_SECRET_NAME = re.compile(
    r"(^|/)(\.env(\..*)?|\.npmrc|\.pypirc|\.netrc|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?|.*\.(pem|key|pfx|p12|jks|keystore|kdbx|ovpn)|"
    r".*(secret|credential|password|token|apikey|api[_-]key)s?(\..*)?)$", re.I)
_PEM = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_LANG = {".py": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript", ".go": "go",
         ".rs": "rust", ".java": "java", ".cs": "csharp", ".rb": "ruby", ".php": "php", ".c": "c", ".h": "c", ".cpp": "cpp",
         ".kt": "kotlin", ".swift": "swift", ".sql": "sql", ".ps1": "powershell", ".sh": "shell"}


class GlobError(ValueError):
    """An include glob is unsafe (absolute, drive, or contains '..')."""


@dataclass
class Discovery:
    files: list[str] = field(default_factory=list)  # posix paths relative to the project root
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (path, reason)
    errors: list[str] = field(default_factory=list)
    git: bool = False


def validate_globs(globs: list[str]) -> list[str]:
    out = []
    for g in globs:
        g = g.strip().replace("\\", "/")
        if not g or g.startswith("/") or re.match(r"^[A-Za-z]:", g) or "\x00" in g or ".." in g.split("/"):
            raise GlobError(f"unsafe include glob: {g!r}")
        out.append(g)
    return out


def glob_to_regex(glob: str) -> re.Pattern[str]:
    i, n, out = 0, len(glob), ""
    while i < n:
        c = glob[i]
        if c == "*":
            if glob[i:i + 3] == "**/":
                out += "(?:.*/)?"
                i += 3
                continue
            if glob[i:i + 2] == "**":
                out += ".*"
                i += 2
                continue
            out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        else:
            out += re.escape(c)
        i += 1
    return re.compile(f"^{out}$", re.I)


def looks_secret(rel: str) -> bool:
    return bool(_SECRET_NAME.search(rel.replace("\\", "/")))


def _git(root: Path, *args: str) -> Optional[list[str]]:
    try:  # confined (re-review N4); explicit trusted git dir, never discovery (N2 re-review S2)
        p = _git_run([*git_prefix(), *target_args(root), *args, "-z"], root, timeout=30, text=False)
    except (OSError, subprocess.TimeoutExpired, GitError):
        return None
    if p.returncode != 0:
        return None
    return [s.decode("utf-8", "replace") for s in p.stdout.split(b"\0") if s]


def is_git_repo(root: Path) -> bool:
    try:
        p = _git_run([*git_prefix(), *target_args(root), "rev-parse", "--is-inside-work-tree"], root, timeout=15,
                     text=False)
        return p.returncode == 0 and p.stdout.strip() == b"true"
    except (OSError, subprocess.TimeoutExpired, GitError):
        return False


def _walk(root: Path) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for f in filenames:
            out.append(Path(dirpath, f).relative_to(root).as_posix())
    return out


def candidate_files(root: Path, *, tracked_only: bool = False) -> tuple[list[str], bool]:
    """(relative posix paths, is_git). In a git repo: `git ls-files` (cached + untracked-not-ignored, or tracked only),
    which respects .gitignore. Otherwise a walk that skips vendor/build directories (nothing for tracked_only)."""
    if is_git_repo(root):
        args = ["ls-files", "--cached"] if tracked_only else ["ls-files", "--cached", "--others", "--exclude-standard"]
        files = _git(root, *args)
        if files is not None:
            return sorted(set(files)), True
    return ([] if tracked_only else sorted(_walk(root))), False


def _safe_file(root: Path, rel: str) -> Optional[Path]:
    """Path-policy check; returns the file or None (symlink escape, link, hardlink, not a regular file)."""
    try:
        p = resolve_inside(rel, [root], base=str(root))
    except PathPolicyError:
        return None
    return p if path_policy.file_size_inside(root, rel) is not None else None


def is_binary(sample: bytes) -> bool:
    if b"\0" in sample:
        return True
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError as exc:
        return exc.start < len(sample) - 4  # a multibyte char cut at the sample end is fine
    return False


def discover_docs(root: Path, globs: list[str], *, max_bytes: int = MAX_DOC_BYTES, max_files: int = MAX_DOC_FILES) -> Discovery:
    globs = validate_globs(globs)
    regs = [glob_to_regex(g) for g in globs]
    cands, is_git = candidate_files(root)
    d = Discovery(git=is_git)
    for rel in cands:
        if not any(r.match(rel) for r in regs):
            continue
        if Path(rel).suffix.lower() not in DOC_EXTENSIONS:
            d.skipped.append((rel, "not a text document"))
        elif looks_secret(rel):
            d.skipped.append((rel, "secret-looking file name"))
        elif (p := _safe_file(root, rel)) is None:
            d.skipped.append((rel, "outside project / not a regular file"))
        elif p.stat().st_size > max_bytes:
            d.skipped.append((rel, f"larger than {max_bytes} bytes"))
        elif len(d.files) >= max_files:
            d.skipped.append((rel, "file cap reached"))
        else:
            d.files.append(rel)
    return d


def read_text_file(root: Path, rel: str, *, max_bytes: int) -> tuple[Optional[bytes], Optional[str]]:
    """(raw bytes, None) or (None, skip reason). Applies path policy, size, binary and private-key checks."""
    if _safe_file(root, rel) is None:
        return None, "outside project / not a regular file"
    try:  # opened without following links, containment checked on the handle (N2 re-review S11)
        raw = path_policy.read_inside(root, rel, max_bytes=max_bytes)
    except path_policy.FileTooLarge:
        return None, f"larger than {max_bytes} bytes"
    except (PathPolicyError, FileNotFoundError, IsADirectoryError):
        return None, "outside project / not a regular file"
    except OSError as exc:
        return None, f"unreadable: {exc.__class__.__name__}"
    if is_binary(raw[:4096]):
        return None, "binary file"
    if _PEM.search(raw.decode("utf-8", "replace")):
        return None, "contains a private key"
    return raw, None


# ---- source-code metadata ---------------------------------------------------

_GENERIC_SYMS = [
    re.compile(r"^\s*export\s+(?:default\s+)?(?:async\s+)?(function|class|const|let|interface|type|enum)\s+([A-Za-z_$][\w$]*)", re.M),
    re.compile(r"^(?:async\s+)?(function|class)\s+([A-Za-z_$][\w$]*)", re.M),
    re.compile(r"^\s*(?:pub\s+)?(fn|struct|enum|trait|impl)\s+([A-Za-z_][\w]*)", re.M),
    re.compile(r"^\s*(func|type)\s+(?:\([^)]*\)\s*)?([A-Za-z_][\w]*)", re.M),
    re.compile(r"^\s*(?:public\s+|internal\s+)?(?:static\s+)?(class|interface|struct|enum)\s+([A-Za-z_][\w]*)", re.M),
]


def language_of(rel: str) -> Optional[str]:
    return _LANG.get(Path(rel).suffix.lower())


def extract_symbols(rel: str, text: str) -> list[str]:
    """Top-level symbols only; names, never source lines."""
    lang = language_of(rel)
    syms: list[str] = []
    if lang == "python":
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            tree = None
        if tree is not None:
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    syms.append(f"class {node.name}")
                elif isinstance(node, ast.AsyncFunctionDef):
                    syms.append(f"async def {node.name}")
                elif isinstance(node, ast.FunctionDef):
                    syms.append(f"def {node.name}")
                elif isinstance(node, ast.Assign):
                    syms.extend(f"var {t.id}" for t in node.targets if isinstance(t, ast.Name) and t.id.isupper())
            return syms[:MAX_SYMBOLS]
    for rx in _GENERIC_SYMS:
        for m in rx.finditer(text):
            s = f"{m.group(1)} {m.group(2)}"
            if s not in syms:
                syms.append(s)
            if len(syms) >= MAX_SYMBOLS:
                return syms
    return syms


def code_meta(rel: str, raw: bytes) -> tuple[str, str]:
    """(title, body) for one source file: path, language, size, top-level symbols. No content dump."""
    text = raw.decode("utf-8", "replace")
    syms = extract_symbols(rel, text)
    body = (f"path: {rel}\nlanguage: {language_of(rel)}\nsize_bytes: {len(raw)}\nlines: {text.count(chr(10)) + 1}\n"
            "symbols: " + ("; ".join(syms) if syms else "(none found)"))
    return rel, body


def discover_code(root: Path, *, max_files: int = MAX_CODE_FILES) -> Discovery:
    """Tracked source files only (git ls-files). Non-git projects yield nothing: 'tracked' is undefined there."""
    cands, is_git = candidate_files(root, tracked_only=True)
    d = Discovery(git=is_git)
    if not is_git:
        d.skipped.append(("*", "project is not a git repository (code metadata covers tracked files only)"))
        return d
    for rel in cands:
        if language_of(rel) is None:
            continue
        if looks_secret(rel):
            d.skipped.append((rel, "secret-looking file name"))
        elif len(d.files) >= max_files:
            d.skipped.append((rel, "file cap reached"))
        else:
            d.files.append(rel)
    return d


# ---- error signatures --------------------------------------------------------

_PATH_RX = re.compile(r"(?:[A-Za-z]:)?(?:[\\/][\w.\-@~]+)+[\\/]?|\b[\w.\-]+(?:[\\/][\w.\-]+)+")
_HEX_RX = re.compile(r"\b0x[0-9a-fA-F]+\b|\b[0-9a-f]{7,40}\b")
_ID_RX = re.compile(r"\b(?:tsk|run|vrf|chk|apr|ckp)_[0-9a-z]{10,}\b")
_NUM_RX = re.compile(r"\d+")


def normalise_error(text: str, limit: int = 240) -> str:
    """Stable signature: first non-empty line, lowercase, ids / paths / hex / numbers stripped, whitespace collapsed."""
    lines = (text or "").strip().splitlines()
    first = next((ln for ln in lines if ln.strip()), "")
    s = first.lower()
    s = _ID_RX.sub("<id>", s)
    s = _PATH_RX.sub("<path>", s)
    s = _HEX_RX.sub("<hex>", s)
    s = _NUM_RX.sub("N", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]
