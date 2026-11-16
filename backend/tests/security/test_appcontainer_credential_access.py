"""Open-only probes: no real credential bytes are read, logged or copied."""

import json
import sys
from pathlib import Path

import pytest

from security.test_n2_verification_sandbox import Env

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows AppContainer")


def test_existing_credentials_cannot_even_be_opened(tmp_path):
    # This explicitly tests access control on the user's real paths without
    # reading any file content, even if an access-control regression is present.
    candidates = {
        "codex-auth": Path.home() / ".codex" / "auth.json",
        "claude-auth": Path.home() / ".claude" / ".credentials.json",
        "ssh-key": Path.home() / ".ssh" / "id_rsa",
    }
    existing = {name: str(path) for name, path in candidates.items() if path.is_file()}
    if not existing:
        pytest.skip("No credential files at the known paths; fake credential denial is tested separately")
    instance = Env(tmp_path)
    try:
        script = instance.wt / "credential-open-probe.py"
        script.write_text('''
import json, os, sys
results = {}
for name, path in json.loads(sys.argv[1]).items():
    try:
        fd = os.open(path, os.O_RDONLY)
    except PermissionError:
        results[name] = "DENIED"
    else:
        os.close(fd)  # never read the descriptor, even when isolation failed
        results[name] = "OPENED"
print("CREDENTIAL-ACCESS=" + json.dumps(results))
''', encoding="utf-8")
        status, checks = instance.run([[sys.executable, str(script), json.dumps(existing)]])
        assert status == "passed", (checks[0].error, checks[0].output)
        results = json.loads(checks[0].output.split("CREDENTIAL-ACCESS=", 1)[1].splitlines()[0])
        assert results and set(results.values()) == {"DENIED"}, results
    finally:
        instance.close()
