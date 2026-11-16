"""Allowed skill sources and the safe, execution-free fetch.

Installing means fetching files. Nothing from a fetched repository is ever executed:

- a throw-away repository is created with an EMPTY template (no sample hooks), `core.hooksPath` pointing at the
  null device, no credential helper, no submodule recursion, no LFS filters, only the https (or injected local)
  protocol allowed;
- the pinned commit is fetched, its tree is *listed* (`git ls-tree`), validated entry by entry, and the blobs
  are written by this module (`git cat-file --batch`). No `git checkout` happens, so no smudge/clean filter,
  `.gitattributes` driver or post-checkout hook can ever run;
- symlinks, path traversal / reserved / ADS names and over-limit trees are rejected; submodules are skipped.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from orchestrator.gitops import git
from orchestrator.skills.errors import FetchFailed, FetchRejected, SourceNotAllowed

_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


@dataclass(frozen=True)
class SourceSpec:
    clone_url: str
    ref: Optional[str] = None  # branch / tag named in a /tree/<ref>/ URL
    subpath: str = ""  # only this folder is extracted ("" = whole repository)
    local: bool = False  # injected (test) sources: allows the file protocol


class SourcePolicy:
    """Decides which source URLs may be fetched. Inject a different policy in tests."""

    def parse(self, url: str) -> SourceSpec:  # pragma: no cover - interface
        raise NotImplementedError


_GH_RE = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9][A-Za-z0-9_.-]*)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?"
    r"(?:/tree/(?P<ref>[^/\s]+)(?:/(?P<sub>[^\s]+?))?)?/?$"
)


def check_subpath(sub: str) -> str:
    sub = sub.strip("/")
    if sub:
        for part in sub.split("/"):
            if part in ("", ".", "..") or ":" in part or "\\" in part:
                raise SourceNotAllowed(f"invalid sub-path {sub!r}")
    return sub


class GithubSourcePolicy(SourcePolicy):
    """Only https://github.com/<owner>/<repo>[/tree/<ref>/<sub/path>] sources."""

    def parse(self, url: str) -> SourceSpec:
        m = _GH_RE.match((url or "").strip())
        if not m:
            raise SourceNotAllowed("only https://github.com/<owner>/<repo>[/tree/<ref>/<path>] sources are allowed")
        ref = m.group("ref")
        if ref and ref.startswith("-"):
            raise SourceNotAllowed("invalid ref")
        return SourceSpec(
            clone_url=f"https://github.com/{m.group('owner')}/{m.group('repo')}.git",
            ref=ref,
            subpath=check_subpath(m.group("sub") or ""),
        )


def normalize_source(url: Optional[str]) -> Optional[str]:
    """Key for duplicate detection: lower-cased repo (+ sub-path), no scheme/.git/trailing slash/ref."""
    if not url:
        return None
    u = url.strip().lower().rstrip("/")
    u = re.sub(r"^https?://(www\.)?", "", u)
    m = re.match(r"^github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/tree/[^/]+(?:/(.+))?)?$", u)
    if m:
        key = f"github.com/{m.group(1)}/{m.group(2)}"
        return key + (f"#{m.group(3)}" if m.group(3) else "")
    return u[:-4] if u.endswith(".git") else u


@dataclass(frozen=True)
class FetchLimits:
    max_files: int = 3000
    max_total_bytes: int = 50 * 1024 * 1024
    max_file_bytes: int = 10 * 1024 * 1024
    timeout: float = 300.0


@dataclass
class FetchResult:
    sha: str
    path: Path
    subpath: str
    files: int
    total_bytes: int
    skipped: list[str] = field(default_factory=list)
    tree_sha256: str = ""


def hash_tree(root: Path) -> str:
    """Content hash of a directory tree: sha256 over the sorted (posix relative path, sha256 of bytes) pairs.
    Symlinks are not followed (a link contributes its target string), so a swapped file cannot hide."""
    h = hashlib.sha256()
    entries: list[tuple[str, str]] = []
    base = Path(root)
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(base).as_posix()
        if p.is_symlink():
            entries.append((rel, "link:" + os.readlink(p)))
        elif p.is_file():
            entries.append((rel, hashlib.sha256(p.read_bytes()).hexdigest()))
    h.update(json.dumps(entries).encode("utf-8"))
    return h.hexdigest()


def _safe_part(part: str) -> bool:
    if part in ("", ".", ".."):
        return False
    if ":" in part or "\\" in part or "\x00" in part:
        return False
    if part.lower() == ".git":
        return False
    if part != part.rstrip(" ."):
        return False
    if part.split(".")[0].lower() in _RESERVED:
        return False
    return True


def validate_entry_path(rel: str) -> None:
    if rel.startswith("/") or not all(_safe_part(p) for p in rel.split("/")):
        raise FetchRejected(f"unsafe path in repository: {rel!r}")


def remove_tree(path: Path) -> None:
    if not path.exists():
        return

    def onexc(func, p, _exc):  # read-only files (git objects) on Windows
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    shutil.rmtree(path, onexc=onexc)


def _safe_git(spec: SourceSpec) -> list[str]:
    protos = ["-c", "protocol.allow=never", "-c", "protocol.https.allow=always"]
    if spec.local:
        protos += ["-c", "protocol.file.allow=always"]
    return [
        "-c", f"core.hooksPath={os.devnull}",
        "-c", "core.fsmonitor=false",
        "-c", "credential.helper=",
        "-c", "core.symlinks=false",
        "-c", "transfer.fsckObjects=true",
        "-c", "filter.lfs.smudge=",
        "-c", "filter.lfs.process=",
        "-c", "filter.lfs.required=false",
        *protos,
    ]


def _parse_ls_tree(out: str) -> list[tuple[str, str, str, int, str]]:
    entries = []
    for rec in out.split("\x00"):
        if not rec:
            continue
        meta, _, path = rec.partition("\t")
        parts = meta.split()
        if len(parts) < 4:
            raise FetchRejected("unparseable tree listing")
        mode, typ, oid, size = parts[0], parts[1], parts[2], parts[3]
        entries.append((mode, typ, oid, int(size) if size.isdigit() else 0, path))
    return entries


def _cat_blobs(repo: Path, oids: list[str], timeout: float) -> dict[str, bytes]:
    if not oids:
        return {}
    unique = list(dict.fromkeys(oids))
    try:  # confined in the worker job like every orchestrator git child (re-review N4)
        proc = git._run([*git.git_prefix(), *git.target_args(repo), "cat-file", "--batch"], repo, timeout=timeout,
                        input_text=("\n".join(unique) + "\n").encode(), text=False)
    except (OSError, subprocess.TimeoutExpired, git.GitError) as exc:
        raise FetchFailed(f"git cat-file failed: {exc}") from exc
    if proc.returncode != 0:
        raise FetchFailed(f"git cat-file failed: {proc.stderr.decode(errors='replace')[:300]}")
    data, pos, out = proc.stdout, 0, {}
    for oid in unique:
        nl = data.index(b"\n", pos)
        header = data[pos:nl].decode().split()
        if len(header) != 3 or header[1] != "blob":
            raise FetchRejected(f"unexpected object {oid}")
        size = int(header[2])
        out[oid] = data[nl + 1 : nl + 1 + size]
        pos = nl + 1 + size + 1
    return out


def resolve_and_fetch(
    spec: SourceSpec,
    dest_root: Path,
    *,
    ref_request: Optional[str],
    limits: FetchLimits = FetchLimits(),
) -> FetchResult:
    """Fetch `ref_request` (a commit sha, branch/tag, or None = spec.ref or the remote HEAD) of the source and
    extract its tree (or sub-path) into `dest_root/<sha>/`. Synchronous; call through a worker thread."""
    request = ref_request or spec.ref or "HEAD"
    if request.startswith("-") or any(c in request for c in " \t\r\n\x00~^:?*[\\"):
        raise SourceNotAllowed(f"invalid ref {request!r}")
    dest_root.mkdir(parents=True, exist_ok=True)
    work = dest_root / f".tmp-{uuid.uuid4().hex[:10]}"
    repo = work / "repo"
    try:
        repo.mkdir(parents=True)
        safe = _safe_git(spec)
        try:
            git.run_git(["init", "-q", "--template="], repo, timeout=60)
            git.run_git([*safe, "remote", "add", "origin", spec.clone_url], repo, timeout=60)
            base = [*safe, "fetch", "--no-tags", "--no-recurse-submodules", "--quiet"]
            try:
                git.run_git([*base, "--depth=1", "origin", request], repo, timeout=limits.timeout)
            except git.GitError:
                if not _SHA_RE.match(request):
                    raise
                git.run_git([*base, "origin", request], repo, timeout=limits.timeout)  # server refuses shallow-by-sha
            sha = git.run_git(["rev-parse", "--verify", "FETCH_HEAD^{commit}"], repo, timeout=60).stdout.strip()
        except git.GitError as exc:
            raise FetchFailed(f"could not fetch {request!r}: {str(exc)[:400]}") from exc
        if _SHA_RE.match(request) and sha.lower() != request.lower():
            raise FetchRejected(f"fetched commit {sha} does not match the pinned commit {request}")

        out = git.run_git(["ls-tree", "-r", "-l", "-z", "--full-tree", sha], repo, timeout=120).stdout
        prefix = spec.subpath + "/" if spec.subpath else ""
        chosen, skipped, total = [], [], 0
        for mode, typ, oid, size, path in _parse_ls_tree(out):
            if prefix and not path.startswith(prefix):
                continue
            rel = path[len(prefix):]
            if mode == "160000" or typ == "commit":
                skipped.append(f"{path} (submodule, not fetched)")
                continue
            if mode == "120000":
                raise FetchRejected(f"symlink entry rejected: {path}")
            validate_entry_path(rel)
            if size > limits.max_file_bytes:
                raise FetchRejected(f"file too large ({size} bytes): {path}")
            total += size
            chosen.append((rel, oid))
            if len(chosen) > limits.max_files:
                raise FetchRejected(f"too many files (> {limits.max_files})")
            if total > limits.max_total_bytes:
                raise FetchRejected(f"repository too large (> {limits.max_total_bytes} bytes)")
        if not chosen:
            raise FetchRejected(f"nothing to extract (sub-path {spec.subpath!r} missing or empty)")
        folded: dict[str, str] = {}
        for rel, _o in chosen:  # case-insensitive filesystems: two entries must not collide
            key = rel.lower()
            if key in folded and folded[key] != rel:
                raise FetchRejected(f"case-colliding paths: {rel} / {folded[key]}")
            folded[key] = rel

        blobs = _cat_blobs(repo, [o for _r, o in chosen], limits.timeout)
        out_dir = work / "out"
        out_dir.mkdir()
        real_out = Path(os.path.realpath(out_dir))
        for rel, oid in chosen:
            target = out_dir.joinpath(*rel.split("/"))
            if not Path(os.path.realpath(target)).is_relative_to(real_out):
                raise FetchRejected(f"path escapes the archive: {rel}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blobs[oid])

        final = dest_root / sha
        remove_tree(final)
        os.replace(out_dir, final)
        return FetchResult(sha=sha, path=final, subpath=spec.subpath, files=len(chosen), total_bytes=total, skipped=skipped,
                           tree_sha256=hash_tree(final))
    finally:
        remove_tree(work)
