"""Native data-flow denial against live IPv4/IPv6 TCP/UDP loopback endpoints.

The host reaches the same listener before and after the sandbox probe. Socket
timeouts alone, or tests against unreachable Internet hosts, are not evidence.
"""

import json
import socket
import sys

import pytest

from orchestrator.security import appcontainer
from security.test_n2_verification_sandbox import Env

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows AppContainer")


@pytest.fixture
def env(tmp_path):
    instance = Env(tmp_path)
    try:
        yield instance
    finally:
        instance.close()


@pytest.mark.parametrize("family,address", [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")])
@pytest.mark.parametrize("kind", [socket.SOCK_STREAM, socket.SOCK_DGRAM], ids=["tcp", "udp"])
def test_sandbox_cannot_deliver_loopback_data(env, family, address, kind):
    with socket.socket(family, kind) as server:
        server.bind((address, 0))
        if kind == socket.SOCK_STREAM:
            server.listen(4)
        server.settimeout(2)
        port = server.getsockname()[1]

        def host_baseline():
            with socket.socket(family, kind) as client:
                client.settimeout(2)
                if kind == socket.SOCK_STREAM:
                    client.connect((address, port))
                    client.sendall(b"host-baseline")
                    peer, _ = server.accept()
                    with peer:
                        assert peer.recv(32) == b"host-baseline"
                else:
                    client.sendto(b"host-baseline", (address, port))
                    assert server.recvfrom(32)[0] == b"host-baseline"

        host_baseline()
        spec = json.dumps({"family": int(family), "kind": int(kind), "address": address, "port": port})
        script = env.wt / "transport-probe.py"
        script.write_text('''
import json, socket, sys
spec = json.loads(sys.argv[1])
error = 0
with socket.socket(spec["family"], spec["kind"]) as client:
    client.settimeout(1)
    try:
        if spec["kind"] == socket.SOCK_STREAM:
            client.connect((spec["address"], spec["port"]))
            client.sendall(b"sandbox-probe")
        else:
            client.sendto(b"sandbox-probe", (spec["address"], spec["port"]))
    except OSError as exc:
        error = getattr(exc, "winerror", None) or exc.errno
print("TRANSPORT=" + json.dumps({"error": error}))
''', encoding="utf-8")
        status, checks = env.run([{"cmd": [sys.executable, str(script), spec], "timeout": 20}])
        assert status == "passed", (checks[0].error, checks[0].output)
        result = json.loads(checks[0].output.split("TRANSPORT=", 1)[1].splitlines()[0])
        if kind == socket.SOCK_STREAM:
            # Python raises TimeoutError with no WinError for a silently dropped
            # connect; errno may be None. A successful connect is never accepted.
            assert result["error"] in (10013, 10035, None), result
        else:
            # UDP send can report success before WFP drops the datagram. The
            # decisive assertion is that the live recipient receives no data.
            assert result["error"] in (0, 10013, 10035, None), result
        assert appcontainer.task_sid(env.wt) not in appcontainer.loopback_exemptions()
        server.settimeout(0.2)
        with pytest.raises(TimeoutError):
            server.accept() if kind == socket.SOCK_STREAM else server.recvfrom(32)
        server.settimeout(2)
        host_baseline()
