"""Incremental indexer: builds `knowledge_docs` rows (FTS5 is kept in sync by the schema triggers).

Unit of incrementality is a *source*: (project_id, source_path, family). Each source carries a `source_hash`;
a source whose hash matches every stored chunk is skipped, a changed one is replaced (re-chunked), and
a source that no longer exists is removed. Report counters (added / updated / removed / skipped) count SOURCES
(one file, one task, one skill ...), not chunks.

Writes go to knowledge_docs / knowledge_vectors ONLY. Task/run state is read, never written (see package docstring).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from orchestrator.db import telemetry
from orchestrator.db.database import Database, Tx
from orchestrator.gitops import git as gitops
from orchestrator.knowledge import sources as src
from orchestrator.knowledge.chunking import chunk_markdown
from orchestrator.security.paths import PathPolicyError, validate_root
from orchestrator.security.redact import redact
from orchestrator.util import dumps, loads, now_iso, truncate

CHUNK_VERSION = "c1"
ALL_SOURCES = ("docs", "tasks", "errors", "code", "skills", "decisions", "models")
MAX_BODY = 4000
DOC_KINDS = ("doc", "adr", "requirement")

_ADR_HEAD = re.compile(r"^ADR[-\s_:]?\d+", re.I)
_ADR_PATH = re.compile(r"(^|/)(adrs?|decisions?)(/|$)|(^|/)adr[-_ ]?\d+", re.I)
_REQ_PATH = re.compile(r"requirement|(^|[/_\-.])prd([/_\-.]|$)|user[-_ ]stor", re.I)


@dataclass
class Report:
    added: int = 0
    updated: int = 0
    removed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    skipped_details: list[dict[str, str]] = field(default_factory=list)  # files refused by policy (secret/binary/ignored...)

    def as_dict(self) -> dict[str, Any]:
        return {"added": self.added, "updated": self.updated, "removed": self.removed, "skipped": self.skipped,
                "errors": self.errors[:50], "skipped_details": self.skipped_details[:50]}


@dataclass
class Unit:
    project_id: Optional[str]
    source_path: str
    source_hash: str
    make: Callable[[], list[tuple[str, str, str]]]  # -> [(kind, title, body)]
    updated_at: Optional[str] = None


@dataclass(frozen=True)
class Family:
    name: str
    kinds: tuple[str, ...]
    path_sql: str = ""  # extra WHERE on source_path (family isolation within a kind)


FAMILIES = {
    "docs": Family("docs", DOC_KINDS, "source_path NOT LIKE '%:%'"),
    "code": Family("code", ("code_meta",)),
    "tasks": Family("tasks", ("task",)),
    "errors": Family("errors", ("error_resolution",)),
    "skills": Family("skills", ("skill",)),
    "decisions": Family("decisions", ("adr",), "source_path LIKE 'decision:%'"),
    "models": Family("models", ("model_history",)),
}


def sha(*parts: Any) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p if isinstance(p, bytes) else str(p).encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def doc_id(project_id: Optional[str], kind: str, source_path: str, idx: int) -> str:
    return "kd_" + hashlib.sha1(f"{project_id or ''}|{kind}|{source_path}|{idx}".encode()).hexdigest()[:24]


def clean(text: str, limit: int = MAX_BODY) -> str:
    return truncate(redact(text or ""), limit, keep="head")


# ---- apply -------------------------------------------------------------------


def _existing(tx: Tx, fam: Family, project_id: Optional[str]) -> dict[str, list[Any]]:
    sql = f"SELECT id, source_path, source_hash FROM knowledge_docs WHERE kind IN ({','.join('?' * len(fam.kinds))})"
    params: list[Any] = list(fam.kinds)
    if fam.path_sql:
        sql += f" AND {fam.path_sql}"
    if project_id is None:
        sql += " AND project_id IS NULL"
    else:
        sql += " AND project_id = ?"
        params.append(project_id)
    out: dict[str, list[Any]] = {}
    for r in tx.all(sql, params):
        out.setdefault(r["source_path"] or "", []).append(r)
    return out


def apply_units(tx: Tx, fam: Family, project_id: Optional[str], units: Iterable[Unit], report: Report, *,
                full: bool = False, complete: bool = True) -> None:
    """Upsert `units` for one (family, project). `complete`=True means `units` enumerates every live source, so
    stored sources that are absent are removed."""
    existing = _existing(tx, fam, project_id)
    seen: set[str] = set()
    for u in units:
        seen.add(u.source_path)
        old = existing.get(u.source_path, [])
        if old and not full and all(r["source_hash"] == u.source_hash for r in old):
            report.skipped += 1
            continue
        try:
            chunks = u.make()
        except Exception as exc:  # one bad source must not abort the run
            report.errors.append(f"{fam.name}:{u.source_path}: {exc.__class__.__name__}: {exc}")
            continue
        for r in old:
            tx.execute("DELETE FROM knowledge_docs WHERE id = ?", (r["id"],))
        if not chunks:
            if old:
                report.removed += 1
            else:
                report.skipped += 1
            continue
        ts = u.updated_at or now_iso()
        for i, (kind, title, body) in enumerate(chunks):
            tx.execute(
                "INSERT INTO knowledge_docs (id, project_id, kind, title, body, source_path, source_hash, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (doc_id(project_id, kind, u.source_path, i), project_id, kind, clean(title, 300), clean(body), u.source_path, u.source_hash, ts),
            )
        if old:
            report.updated += 1
        else:
            report.added += 1
    if complete:
        for path, rows in existing.items():
            if path not in seen:
                for r in rows:
                    tx.execute("DELETE FROM knowledge_docs WHERE id = ?", (r["id"],))
                report.removed += 1


# ---- file sources ---------------------------------------------------------------


def classify_chunk(rel: str, heading: str) -> str:
    if _ADR_PATH.search(rel) or any(_ADR_HEAD.match(p.strip()) for p in heading.split(">")):
        return "adr"
    if _REQ_PATH.search(rel):
        return "requirement"
    return "doc"


def doc_units(root: Path, project_id: str, globs: list[str], max_bytes: int, report: Report) -> list[Unit]:
    disc = src.discover_docs(root, globs, max_bytes=max_bytes)
    report.skipped_details += [{"path": p, "reason": r} for p, r in disc.skipped]
    units: list[Unit] = []
    for rel in disc.files:
        raw, why = src.read_text_file(root, rel, max_bytes=max_bytes)
        if raw is None:
            report.skipped_details.append({"path": rel, "reason": why or "unreadable"})
            continue

        def make(rel: str = rel, raw: bytes = raw) -> list[tuple[str, str, str]]:
            text = raw.decode("utf-8", "replace")
            out = []
            for c in chunk_markdown(text, fallback_title=Path(rel).name):
                title = rel if not c.heading or c.heading == Path(rel).name else f"{rel} > {c.heading}"
                out.append((classify_chunk(rel, c.heading), title, c.text))
            return out

        units.append(Unit(project_id, rel, sha(raw, CHUNK_VERSION), make))
    return units


def code_units(root: Path, project_id: str, report: Report) -> list[Unit]:
    disc = src.discover_code(root)
    report.skipped_details += [{"path": p, "reason": r} for p, r in disc.skipped[:100]]
    units: list[Unit] = []
    for rel in disc.files:
        raw, why = src.read_text_file(root, rel, max_bytes=src.MAX_CODE_BYTES)
        if raw is None:
            report.skipped_details.append({"path": rel, "reason": why or "unreadable"})
            continue

        def make(rel: str = rel, raw: bytes = raw) -> list[tuple[str, str, str]]:
            title, body = src.code_meta(rel, raw)
            return [("code_meta", title, body)]

        units.append(Unit(project_id, rel, sha(raw, CHUNK_VERSION), make))
    return units


# ---- DB-derived sources -------------------------------------------------------------


def _jl(v: Any) -> list[Any]:
    x = loads(v, [])
    return x if isinstance(x, list) else []


def task_unit(tx: Tx, t: Any) -> Unit:
    """Compact structured summary of a finished task. Never a transcript."""
    runs = tx.all("SELECT provider, model, phase, status FROM runs WHERE task_id = ? AND phase IN ('execute','repair') ORDER BY started_at", (t["id"],))
    last = runs[-1] if runs else None
    v = tx.one("SELECT status, checks FROM verifications WHERE task_id = ? ORDER BY rowid DESC LIMIT 1", (t["id"],))
    failed_checks = []
    if v is not None:
        failed_checks = [c.get("name", "?") for c in _jl(v["checks"]) if isinstance(c, dict) and not c.get("passed")]
    ac = [str(x) for x in _jl(t["acceptance_criteria"])]
    files = [str(x) for x in _jl(t["relevant_files"])]
    desc = (t["description"] or "").strip()
    lines = [
        f"Objective: {t['title']}" + (f"\n{truncate(desc, 600, keep='head')}" if desc else ""),
        "Acceptance criteria: " + ("; ".join(ac[:10]) if ac else "(none recorded)"),
        f"Outcome: {t['state']}" + (f" ({t['state_reason']})" if t["state_reason"] else ""),
        "Verification: " + (v["status"] + (f"; failing checks: {', '.join(failed_checks[:5])}" if failed_checks else "") if v else "none recorded"),
        "Files changed (task relevant_files): " + (", ".join(files[:30]) if files else "(none recorded)"),
        "Provider/model: " + (f"{last['provider']}/{last['model'] or '?'}" if last else "(no execute run)"),
        f"Duration: {round(float(t['active_seconds'] or 0))} s active; task_type={t['task_type']}; retries={t['retry_count']}; repair_cycles={t['repair_cycles']}",
    ]
    body = "\n".join(lines)
    return Unit(t["project_id"], f"task:{t['id']}", sha(body, CHUNK_VERSION), lambda: [("task", t["title"], body)],
                updated_at=t["finished_at"] or t["updated_at"])


def finished_tasks(tx: Tx, project_id: Optional[str], task_id: Optional[str] = None) -> list[Any]:
    sql, params = "SELECT * FROM tasks WHERE state IN ('COMPLETED','FAILED')", []
    if project_id:
        sql += " AND project_id = ?"
        params.append(project_id)
    if task_id:
        sql += " AND id = ?"
        params.append(task_id)
    return tx.all(sql + " ORDER BY created_at", params)


def _check_failure_text(checks: list[Any]) -> str:
    for c in checks:
        if isinstance(c, dict) and not c.get("passed"):
            tail = [ln for ln in str(c.get("error") or c.get("output") or "").strip().splitlines() if ln.strip()]
            return f"{c.get('name', 'check')}: {tail[-1] if tail else 'failed (exit ' + str(c.get('exit_code')) + ')'}"
    return ""


def collect_error_evidence(tx: Tx, t: Any) -> Optional[dict[str, Any]]:
    """Failed run/verification followed (later) by a passing verification, from real rows. None if no such history."""
    vers = tx.all("SELECT * FROM verifications WHERE task_id = ? ORDER BY started_at, rowid", (t["id"],))
    runs = tx.all("SELECT * FROM runs WHERE task_id = ? AND phase IN ('execute','repair') ORDER BY started_at, rowid", (t["id"],))
    def failed_before(at: str) -> bool:
        return any(f["status"] in ("failed", "error") and f["started_at"] < at for f in vers) or any(
            r["status"] == "failed" and r["started_at"] < at for r in runs)

    passing = next((v for v in vers if v["status"] == "passed" and failed_before(v["started_at"])), None)
    if passing is None:
        return None
    p_at = passing["started_at"]
    f_vers = [v for v in vers if v["status"] in ("failed", "error") and v["started_at"] < p_at]
    f_runs = [r for r in runs if r["status"] == "failed" and r["started_at"] < p_at]
    fv = f_vers[-1] if f_vers else None
    fr = f_runs[-1] if f_runs else None
    fail_at = max([x for x in ((fv["started_at"] if fv else None), (fr["finished_at"] or fr["started_at"] if fr else None)) if x])
    pr = next((r for r in runs if r["status"] == "succeeded" and r["started_at"] >= (fr["started_at"] if fr else "") and r["started_at"] <= p_at), None)
    pr = pr or next((r for r in reversed(runs) if r["status"] == "succeeded" and r["started_at"] <= p_at), None)
    check_text = _check_failure_text(_jl(fv["checks"])) if fv else ""
    run_text = ""
    if fr is not None:
        run_text = f"{fr['error_category'] or 'error'}: {fr['error_message'] or ''}".strip()
    cps = tx.all("SELECT git_ref, created_at FROM checkpoints WHERE task_id = ? AND git_ref IS NOT NULL ORDER BY created_at", (t["id"],))
    ref_a = next((c["git_ref"] for c in reversed(cps) if c["created_at"] <= fail_at), None)
    ref_b = next((c["git_ref"] for c in cps if c["created_at"] >= fail_at and c["git_ref"] != ref_a), None) or t["head_commit"]
    proj = tx.one("SELECT path FROM projects WHERE id = ?", (t["project_id"],))
    return {
        "task_id": t["id"], "title": t["title"], "project_id": t["project_id"], "project_path": proj["path"] if proj else None,
        "signature_source": check_text or run_text or "unknown failure",
        "category": (fr["error_category"] if fr else None) or ("verification_failed" if fv else None),
        "what_failed": " | ".join(x for x in (check_text and f"verification {fv['status']}: {check_text}", run_text and f"run {fr['phase']}: {run_text}") if x),
        "failing_provider": f"{fr['provider']}/{fr['model'] or '?'}" if fr else None,
        "passing_provider": f"{pr['provider']}/{pr['model'] or '?'}" if pr else None,
        "provider": pr["provider"] if pr else (fr["provider"] if fr else None),
        "ref_a": ref_a, "ref_b": ref_b, "base_commit": t["base_commit"], "relevant_files": [str(x) for x in _jl(t["relevant_files"])],
        "passing_at": p_at, "updated_at": t["finished_at"] or t["updated_at"],
    }


def _diff_info(ev: dict[str, Any]) -> tuple[str, list[str], str]:
    """(basis, files, diffstat). Best effort via git; falls back to the task's recorded relevant_files."""
    path = ev.get("project_path")
    a, b, base = ev.get("ref_a"), ev.get("ref_b"), ev.get("base_commit")
    pairs = []
    if a and b and a != b:
        pairs.append(("git diff between checkpoints around the failure", a, b))
    elif b and base and b != base:
        pairs.append(("git diff task base..head (no checkpoint before the failure)", base, b))
    for basis, x, y in pairs:
        if not path or x.startswith("-") or y.startswith("-"):
            continue
        try:
            stat = gitops.run_git(["diff", "--no-color", "--no-ext-diff", "--stat", x, y, "--"], path, timeout=20).stdout
            names = gitops.run_git(["diff", "--no-color", "--no-ext-diff", "--name-only", x, y, "--"], path, timeout=20).stdout
            return basis, [n for n in names.splitlines() if n][:30], truncate(stat.strip(), 800, keep="head")
        except Exception:
            continue
    return "task relevant_files (git diff unavailable)", ev.get("relevant_files", [])[:30], ""


def error_unit(ev: dict[str, Any]) -> Unit:
    sig = src.normalise_error(redact(ev["signature_source"]))
    basis, files, stat = _diff_info(ev)
    body = "\n".join([
        f"Error signature: {sig}",
        f"Category: {ev['category'] or 'unknown'}",
        f"What failed: {truncate(redact(ev['what_failed']), 600, keep='head')}",
        f"Provider: failing {ev['failing_provider'] or '?'} -> passing {ev['passing_provider'] or '?'}",
        f"What changed ({basis}): " + (", ".join(files) if files else "(unknown)"),
        *( [f"Diff stat:\n{stat}"] if stat else [] ),
        f"Task: {ev['title']} ({ev['task_id']})",
    ])
    title = f"Error resolution: {sig[:100]}"
    return Unit(ev["project_id"], f"error:{ev['task_id']}", sha(body, CHUNK_VERSION), lambda: [("error_resolution", title, body)],
                updated_at=ev["updated_at"])


def skill_units(tx: Tx) -> list[Unit]:
    units = []
    for s in tx.all("SELECT * FROM skills ORDER BY id"):
        body = "\n".join([f"Skill: {s['name']}", f"Kind: {s['kind']}", f"Category: {s['category'] or '-'}", f"State: {s['state']}",
                          f"Description: {s['description']}", f"Source: {s['source_url'] or '-'}"])
        units.append(Unit(None, f"skill:{s['id']}", sha(body, CHUNK_VERSION), lambda s=s, b=body: [("skill", s["name"], b)], updated_at=s["updated_at"]))
    return units


def decision_units(tx: Tx, project_id: Optional[str]) -> list[Unit]:
    sql, params = "SELECT * FROM decisions WHERE project_id IS NULL", []
    if project_id:
        sql, params = "SELECT * FROM decisions WHERE project_id = ?", [project_id]
    units = []
    for d in tx.all(sql + " ORDER BY created_at", params):
        body = "\n".join([f"Decision: {truncate(d['decision'], 800, keep='head')}", f"Context: {truncate(d['context'], 800, keep='head')}",
                          f"Rationale: {truncate(d['rationale'], 800, keep='head')}"])
        units.append(Unit(d["project_id"], f"decision:{d['id']}", sha(d["title"], body, CHUNK_VERSION),
                          lambda d=d, b=body: [("adr", d["title"], b)], updated_at=d["created_at"]))
    return units


def model_units(tx: Tx) -> list[Unit]:
    units = []
    for (prov, model, ttype), s in sorted(telemetry.history_stats(tx).items()):
        n, k = s["verified_runs"], s["successes"]
        body = f"Model history: {prov}/{model} on task_type={ttype}: {k} of {n} verified runs succeeded ({k / n:.0%}). Definition: db.telemetry.history_stats."
        units.append(Unit(None, f"model:{prov}/{model}/{ttype}", sha(body, CHUNK_VERSION),
                          lambda p=prov, m=model, b=body: [("model_history", f"{p}/{m}", b)]))
    return units


# ---- settings -------------------------------------------------------------------------


def knowledge_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Effective knowledge config from `settings`: key "knowledge" ({include_globs, max_file_bytes, auto_index,
    vectors: {kinds: [...]}}) and/or the dotted key "knowledge.vectors.kinds"."""
    k = settings.get("knowledge") if isinstance(settings.get("knowledge"), dict) else {}
    vec = k.get("vectors") if isinstance(k.get("vectors"), dict) else {}
    kinds = settings.get("knowledge.vectors.kinds", vec.get("kinds", []))
    kinds = [x for x in (kinds if isinstance(kinds, list) else []) if x in ("requirement", "adr", "doc", "task", "error_resolution", "code_meta", "skill", "model_history")]
    globs = k.get("include_globs") if isinstance(k.get("include_globs"), list) and k.get("include_globs") else list(src.DEFAULT_INCLUDE_GLOBS)
    max_bytes = k.get("max_file_bytes")
    return {
        "include_globs": [str(g) for g in globs],
        "max_file_bytes": int(max_bytes) if isinstance(max_bytes, int) and 1000 <= max_bytes <= 2_000_000 else src.MAX_DOC_BYTES,
        "auto_index": bool(k.get("auto_index", True)),
        "vector_kinds": kinds,
    }


# ---- orchestration ----------------------------------------------------------------------


class Indexer:
    def __init__(self, db: Database):
        self.db = db

    async def run(self, *, project_id: Optional[str] = None, sources: Optional[Iterable[str]] = None, full: bool = False,
                  globs: Optional[list[str]] = None) -> Report:
        from orchestrator.db import records  # local: avoids an import cycle at package import

        want = [s for s in (sources or ALL_SOURCES) if s in ALL_SOURCES]
        report = Report()
        settings = await self.db.read(records.settings_all)
        cfg = knowledge_settings(settings)
        use_globs = src.validate_globs(globs) if globs else cfg["include_globs"]
        projects = await self.db.read(lambda tx: [dict(r) for r in tx.all(
            "SELECT id, path FROM projects" + (" WHERE id = ?" if project_id else "") + " ORDER BY created_at", (project_id,) if project_id else ())])
        if project_id and not projects:
            report.errors.append(f"unknown project {project_id}")
            return report

        for p in projects:
            root: Optional[Path] = None
            if {"docs", "code"} & set(want):
                try:
                    root = validate_root(p["path"])
                except PathPolicyError as exc:
                    report.errors.append(f"project {p['id']}: path refused: {exc}")
            if "docs" in want and root is not None:
                units = await _thread(doc_units, root, p["id"], use_globs, cfg["max_file_bytes"], report)
                await self.db.write(lambda tx, u=units: apply_units(tx, FAMILIES["docs"], p["id"], u, report, full=full))
            if "code" in want and root is not None:
                units = await _thread(code_units, root, p["id"], report)
                await self.db.write(lambda tx, u=units: apply_units(tx, FAMILIES["code"], p["id"], u, report, full=full))
            if "tasks" in want:
                await self.db.write(lambda tx: apply_units(
                    tx, FAMILIES["tasks"], p["id"], [task_unit(tx, t) for t in finished_tasks(tx, p["id"])], report, full=full))
            if "errors" in want:
                await self._errors(p["id"], None, report, full)
            if "decisions" in want:
                await self.db.write(lambda tx: apply_units(tx, FAMILIES["decisions"], p["id"], decision_units(tx, p["id"]), report, full=full))
        if "skills" in want:
            await self.db.write(lambda tx: apply_units(tx, FAMILIES["skills"], None, skill_units(tx), report, full=full))
        if "models" in want and project_id is None:
            await self.db.write(lambda tx: apply_units(tx, FAMILIES["models"], None, model_units(tx), report, full=full))
        if project_id is None:  # project-less decisions + knowledge of deleted projects
            if "decisions" in want:
                await self.db.write(lambda tx: apply_units(tx, FAMILIES["decisions"], None, decision_units(tx, None), report, full=full))
            await self.db.write(self._drop_orphans(report))
        return report

    @staticmethod
    def _drop_orphans(report: Report) -> Callable[[Tx], None]:
        def fn(tx: Tx) -> None:
            n = tx.execute("DELETE FROM knowledge_docs WHERE project_id IS NOT NULL AND project_id NOT IN (SELECT id FROM projects)").rowcount
            report.removed += max(n, 0)
        return fn

    async def _errors(self, project_id: str, task_id: Optional[str], report: Report, full: bool) -> None:
        evs = await self.db.read(lambda tx: [e for t in finished_tasks(tx, project_id, task_id)
                                             if (e := collect_error_evidence(tx, t)) is not None])
        units = [await _thread(error_unit, e) for e in evs]
        # a single-task refresh must not delete the other tasks' error docs
        await self.db.write(lambda tx: apply_units(tx, FAMILIES["errors"], project_id, units, report, full=full, complete=task_id is None))

    async def index_task(self, task_id: str) -> Report:
        """Event-driven path: refresh the summary (+ error resolution) of one finished task."""
        report = Report()
        row = await self.db.one("SELECT project_id FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            report.errors.append(f"unknown task {task_id}")
            return report
        pid = row["project_id"]
        await self.db.write(lambda tx: apply_units(
            tx, FAMILIES["tasks"], pid, [task_unit(tx, t) for t in finished_tasks(tx, pid, task_id)], report, complete=False))
        await self._errors(pid, task_id, report, False)
        return report


async def _thread(fn: Callable[..., Any], *args: Any) -> Any:
    import asyncio

    return await asyncio.to_thread(fn, *args)


# ---- vectors (optional) -------------------------------------------------------------------------


def sync_vectors(tx: Tx, embedder: Any, kinds: list[str], *, cap: int = 5000) -> dict[str, int]:
    """Embed docs of the explicitly enabled kinds that lack a vector for `embedder.name`; drop vectors of other
    models / disabled kinds. With no kinds enabled this deletes every vector (OFF by default)."""
    from orchestrator.knowledge.embedding import pack

    removed = tx.execute(
        "DELETE FROM knowledge_vectors WHERE model <> ? OR doc_id NOT IN (SELECT id FROM knowledge_docs WHERE kind IN (%s))"
        % ",".join("?" * len(kinds)) if kinds else "DELETE FROM knowledge_vectors",
        ([embedder.name, *kinds] if kinds else []),
    ).rowcount
    added = 0
    if kinds:
        rows = tx.all(
            "SELECT id, title, body FROM knowledge_docs WHERE kind IN (%s) AND id NOT IN "
            "(SELECT doc_id FROM knowledge_vectors WHERE model = ?) LIMIT ?" % ",".join("?" * len(kinds)),
            [*kinds, embedder.name, cap],
        )
        for r in rows:
            tx.execute("INSERT OR REPLACE INTO knowledge_vectors (doc_id, chunk, vector, model) VALUES (?,0,?,?)",
                       (r["id"], pack(embedder.embed(f"{r['title']}\n{r['body']}")), embedder.name))
            added += 1
    return {"added": added, "removed": max(removed, 0)}
