"""Owning PID of a local TCP connection (SECURITY_REVIEW #1, worker-origin guard).

Given the client endpoint the ASGI server reports and the server endpoint, find the process that owns the
CLIENT side of that connection. Windows uses `GetExtendedTcpTable(TCP_TABLE_OWNER_PID_CONNECTIONS)` for
IPv4 and IPv6 (measured ~0.5 ms with ~180 rows on the dev machine; `psutil.net_connections` was ~2.3 ms
because it decodes every row of every family). Other platforms use `psutil.net_connections("tcp")`.

The functions here block (a syscall plus a table scan); call them from a worker thread.
"""

from __future__ import annotations

import ipaddress
import socket
import struct
import sys
from typing import Optional

import psutil

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes as wt

    _iphlp = ctypes.WinDLL("iphlpapi")
    _gett = _iphlp.GetExtendedTcpTable
    _gett.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.DWORD), wt.BOOL, wt.ULONG, ctypes.c_int, wt.ULONG]
    _gett.restype = wt.DWORD
    TCP_TABLE_OWNER_PID_CONNECTIONS = 4
    ERROR_INSUFFICIENT_BUFFER = 122
    _ROW4 = struct.Struct("<6I")  # state, local addr, local port, remote addr, remote port, pid
    _ROW6 = struct.Struct("<16sII16sIIII")  # local addr, scope, port, remote addr, scope, port, state, pid

    def _table(af: int) -> bytes:
        size = wt.DWORD(0)
        for _ in range(4):  # the table can grow between the size probe and the read
            buf = ctypes.create_string_buffer(max(size.value, 4) + 8192)
            size = wt.DWORD(len(buf))
            rc = _gett(buf, ctypes.byref(size), False, af, TCP_TABLE_OWNER_PID_CONNECTIONS, 0)
            if rc == 0:
                return buf.raw[: size.value]
            if rc != ERROR_INSUFFICIENT_BUFFER:
                raise OSError(rc, "GetExtendedTcpTable failed")
        raise OSError(ERROR_INSUFFICIENT_BUFFER, "GetExtendedTcpTable: table kept growing")

    def _port(raw: int) -> int:
        return socket.ntohs(raw & 0xFFFF)

    def _find_windows(client: ipaddress._BaseAddress, cport: int, server: Optional[ipaddress._BaseAddress], sport: Optional[int]) -> Optional[int]:
        if client.version == 4:
            raw = _table(socket.AF_INET)
            want = client.packed
            (count,) = struct.unpack_from("<I", raw, 0)
            for i in range(count):
                _st, la, lp, ra, rp, pid = _ROW4.unpack_from(raw, 4 + i * _ROW4.size)
                if _port(lp) != cport or struct.pack("<I", la) != want:
                    continue
                if sport is not None and _port(rp) != sport:
                    continue
                if server is not None and server.version == 4 and struct.pack("<I", ra) != server.packed:
                    continue
                if pid:
                    return pid
            return None
        raw = _table(socket.AF_INET6)
        want = client.packed
        (count,) = struct.unpack_from("<I", raw, 0)
        for i in range(count):
            la, _ls, lp, ra, _rs, rp, _st, pid = _ROW6.unpack_from(raw, 4 + i * _ROW6.size)
            if _port(lp) != cport or la != want:
                continue
            if sport is not None and _port(rp) != sport:
                continue
            if server is not None and server.version == 6 and ra != server.packed:
                continue
            if pid:
                return pid
        return None


def _find_psutil(client: ipaddress._BaseAddress, cport: int, server: Optional[ipaddress._BaseAddress], sport: Optional[int]) -> Optional[int]:
    for c in psutil.net_connections(kind="tcp"):
        if not c.laddr or not c.raddr or c.pid is None:
            continue
        if c.laddr.port != cport or _ip(c.laddr.ip) != client:
            continue
        if sport is not None and c.raddr.port != sport:
            continue
        if server is not None and _ip(c.raddr.ip) not in (server, None):
            continue
        return c.pid
    return None


def _ip(text: str) -> Optional[ipaddress._BaseAddress]:
    try:
        addr = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def parse_ip(text: str) -> Optional[ipaddress._BaseAddress]:
    """The IP literal in `text` (IPv4-mapped IPv6 unwrapped), or None when it is not an IP (e.g. 'testclient')."""
    if not isinstance(text, str):
        return None
    return _ip(text.strip("[]"))


def owner_pid(client_host: str, client_port: int, server_host: Optional[str] = None, server_port: Optional[int] = None) -> Optional[int]:
    """PID owning the client side of the connection client -> server, or None when it is not in the table."""
    client = parse_ip(client_host)
    if client is None:
        return None
    server = parse_ip(server_host) if server_host else None
    if IS_WINDOWS:
        # An IPv4-mapped client of a dual-stack listener lives in the IPv6 table with the mapped address.
        pid = _find_windows(client, client_port, server, server_port)
        if pid is None and client.version == 4 and client_host.lower().startswith("::ffff:"):
            mapped = ipaddress.IPv6Address("::ffff:" + str(client))
            srv6 = ipaddress.IPv6Address("::ffff:" + str(server)) if server is not None and server.version == 4 else None
            pid = _find_windows(mapped, client_port, srv6, server_port)
        return pid
    return _find_psutil(client, client_port, server, server_port)


_local_addrs: Optional[set[ipaddress._BaseAddress]] = None


def is_local_address(addr: ipaddress._BaseAddress) -> bool:
    """Loopback or an address of a local interface (a connection from it originates on this machine)."""
    global _local_addrs
    if addr.is_loopback:
        return True
    if _local_addrs is None:
        found: set[ipaddress._BaseAddress] = set()
        try:
            for entries in psutil.net_if_addrs().values():
                for e in entries:
                    ip = _ip(e.address) if e.family in (socket.AF_INET, socket.AF_INET6) else None
                    if ip is not None:
                        found.add(ip)
        except (OSError, psutil.Error):
            pass
        _local_addrs = found
    return addr in _local_addrs


__all__ = ["owner_pid", "parse_ip", "is_local_address"]
