"""Backend for the Playwright e2e run: the REAL app (create_app) with the MOCK provider only.

The stock `orchestrator serve` cannot script the mock provider, and the e2e needs tasks that
hang, run slowly or write files. The scripts below are keyed on markers in the task text
(title + description end up in the provider prompt via the handoff):

    MOCK:HANG   works forever until the task is cancelled
    MOCK:FIXLATER  first run misses fix.txt (verification fails), the repair run writes it
    MOCK:SLOW   emits output over ~3 s, writes result.txt, then succeeds
    (default)   writes result.txt and succeeds immediately

No real CLI is ever started: only the mock provider is registered.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

os.environ.setdefault("ORCHESTRATOR_ENABLE_MOCK", "1")
os.environ.setdefault("ORCHESTRATOR_DISABLE_REAL_PROVIDERS", "1")

import uvicorn  # noqa: E402
from fastapi import APIRouter  # noqa: E402

from orchestrator.api.app import create_app  # noqa: E402
from orchestrator import skills  # noqa: E402
from orchestrator.api.routes import skills as skills_routes  # noqa: E402
from orchestrator.providers.mock import MockProvider, MockScript, MockStep  # noqa: E402
from orchestrator.services.providers import merge_models_cfg, mock_models_cfg  # noqa: E402
from orchestrator.skills.errors import SourceNotAllowed  # noqa: E402
from orchestrator.skills.sources import SourcePolicy, SourceSpec, check_subpath  # noqa: E402

PROVIDER_CALLS = Counter()
test_router = APIRouter()


@test_router.get("/api/_e2e/provider-calls")
def provider_calls():
    """Test-only instrumentation; this router is never included in production."""
    return {"total": sum(PROVIDER_CALLS.values()), "by_provider": dict(PROVIDER_CALLS)}


class LocalPolicy(SourcePolicy):
    """`local:<name>[::sub/path]` -> a git repository on disk created by the e2e global setup. Nothing else is allowed,
    so the e2e never touches the network."""

    def __init__(self, repos: dict[str, str]):
        self.repos = repos

    def parse(self, url: str) -> SourceSpec:
        if not url.startswith("local:"):
            raise SourceNotAllowed("only local e2e sources are allowed here")
        name, _, sub = url[len("local:"):].partition("::")
        if name not in self.repos:
            raise SourceNotAllowed(f"unknown local source {name}")
        return SourceSpec(clone_url=self.repos[name], subpath=check_subpath(sub), local=True)


def make_scripter(name: str):
    """One scripter per fake provider. zeta/zulu exist only so a leader switch has a second eligible model:
    zeta validates the handoff, zulu always fails it (-> rollback)."""

    def scripter(request, index):  # noqa: ANN001
        PROVIDER_CALLS[name] += 1
        if "taking over as the technical leader" in request.prompt:  # leader switch validation (leader/switch.py)
            if name == "zulu":
                return MockScript.success("I cannot restate the tasks")
            m = re.search(r'^(\{"tasks":.*)$', request.prompt, re.M)
            data = json.loads(m.group(1)) if m else {"tasks": []}
            out = {"tasks": [{"id": t["id"], "next_action": "continue"} for t in data["tasks"]]}
            return MockScript.success(json.dumps(out), structured_output=out)
        return base_script(request, index)

    return scripter


def base_script(request, index):  # noqa: ANN001
    prompt = request.prompt
    if "independent technical advisor" in prompt:  # advisor consult (leader/advisor.py)
        return MockScript.success(
            "advice",
            structured_output={
                "assessment": "Both options work; the second keeps the data local.",
                "recommended_option": "Keep it local",
                "risks": ["more code to maintain"],
                "disagreements": [],
                "confidence": 0.7,
                "evidence_requested": ["a benchmark of both options"],
            },
        )
    if "decomposing a user objective" in prompt:  # objective decomposition (leader/objectives.py)
        if "MOCK:BADPLAN" in prompt:
            return MockScript.success("this is not json")
        task = lambda key, title, deps: {  # noqa: E731
            "key": key, "title": title, "description": f"{title} (e2e)", "task_type": "coding", "role": "backend", "priority": 50,
            "depends_on": deps, "acceptance_criteria": [f"{title} works"], "verify_commands": [],
        }
        return MockScript.success(
            "plan",
            structured_output={
                "deliverables": ["A working exporter"],
                "prerequisites": ["Node installed"],
                "acceptance_criteria": ["The exporter writes a file"],
                "tasks": [task("schema", "Define the schema", []), task("impl", "Implement the exporter", ["schema"]), task("docs", "Document the exporter", ["impl"])],
            },
        )
    if "MOCK:HANG" in prompt:
        return MockScript.hang()
    if "MOCK:SLOW" in prompt:
        return MockScript(
            steps=[
                MockStep(action="text", text="slow task: reading the project"),
                MockStep(action="sleep", seconds=1.2),
                MockStep(action="tool_use", text="inspecting README.md", data={"tool": "read_file"}),
                MockStep(action="text", text="slow task: writing result.txt"),
                MockStep(action="sleep", seconds=1.2),
                MockStep(action="write_file", path="result.txt", content="hello from the mock provider\n"),
                MockStep(action="usage", data={"input_tokens": 120, "output_tokens": 40}),
                MockStep(action="sleep", seconds=0.6),
                MockStep(action="succeed", text="slow task finished"),
            ]
        )
    if "MOCK:FIXLATER" in prompt:  # first attempt leaves the project broken; the REPAIR run fixes it
        if "This is a REPAIR run" in prompt:
            return MockScript.success("fixed", files={"result.txt": "ok", "fix.txt": "fixed"})
        return MockScript.success("first attempt", files={"result.txt": "ok"})
    return MockScript.success("mock work finished", files={"result.txt": "hello from the mock provider\n"})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--skills-config", default=None, help="JSON {catalog: path, repos: {name: path}} for the fake skill sources")
    args = ap.parse_args()
    extra_routers: list = [test_router]
    on_startup: list = []
    if args.skills_config:
        cfg = json.loads(Path(args.skills_config).read_text(encoding="utf-8"))
        extra_routers.append(skills_routes.router)
        on_startup.append(
            skills.make_startup(
                catalog_path=Path(cfg["catalog"]),
                source_policy=LocalPolicy(cfg["repos"]),
                which=lambda name: f"/fake/{name}",
            )
        )
    app = create_app(
        data_dir=args.data_dir,
        providers={
            "mock": MockProvider(scripter=make_scripter("mock")),
            "zeta": MockProvider(name="zeta", model="zeta-model", scripter=make_scripter("zeta")),
            "zulu": MockProvider(name="zulu", model="zulu-model", scripter=make_scripter("zulu")),
        },
        extra_models_cfg=merge_models_cfg(mock_models_cfg("zeta", "zeta-model", "frontier"), mock_models_cfg("zulu", "zulu-model", "frontier")),
        poll_interval=0.2,
        celebrate_seconds=0.5,
        use_routing=True,  # the real RoutingService: decisions are recorded and explainable
        extra_routers=extra_routers,
        on_startup=on_startup,
    )
    # loop="asyncio" selects the Proactor loop on Windows (needed for subprocesses).
    uvicorn.run(app, host="127.0.0.1", port=args.port, loop="asyncio", log_level="warning",
                proxy_headers=False, forwarded_allow_ips="")  # see orchestrator/api/security.py (N1)


if __name__ == "__main__":
    sys.exit(main())
