#!/usr/bin/env python3
"""Runs a Linux release program with the getrandom system call failing (ENOSYS).

Old kernels (before 3.17) have no getrandom. The release build exposes the
kernel headers to AWS-LC (build-release.sh) so that it calls getrandom itself
and falls back to /dev/urandom; built without them, it calls musl's
getentropy, which has no fallback, and the program aborts on such a kernel.

Under a seccomp filter that makes getrandom fail with ENOSYS (installed in
each child of this script before it runs the program, and inherited by the
program's own children; this script's TLS client stays outside it), the
program

* initializes in an empty home: it generates its local CA key, and
* runs its local proxy, which completes a TLS handshake for the Claude API
  host with a leaf certificate it issues from that CA.

Nothing leaves the machine: the Gateway named at initialization is a closed
local port and the handshake ends before any request. No host setting is
changed (the filter applies to the program's processes only).

Usage: test_linux_entropy.py --binary PROGRAM
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
import platform
import resource
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TIMEOUT = 60.0
TLS_HOST = "api.anthropic.com"


class SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte), ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32)]


class SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(SockFilter))]


ARCHITECTURES = {"x86_64": (318, 0xC000003E), "aarch64": (278, 0xC00000B7)}


def deny_getrandom() -> None:
    """Installs the filter in the calling process (a child before exec)."""
    # Native system call number and AUDIT_ARCH of each release architecture.
    number, audit_arch = ARCHITECTURES[platform.machine()]
    libc = ctypes.CDLL(None, use_errno=True)
    # BPF: check the architecture; load the number; getrandom -> ENOSYS; allow the rest.
    program = (SockFilter * 7)(
        SockFilter(0x20, 0, 0, 4),
        SockFilter(0x15, 1, 0, audit_arch),
        SockFilter(0x06, 0, 0, 0x80000000),
        SockFilter(0x20, 0, 0, 0),
        SockFilter(0x15, 0, 1, number),
        SockFilter(0x06, 0, 0, 0x00050000 | errno.ENOSYS),
        SockFilter(0x06, 0, 0, 0x7FFF0000),
    )
    filter_program = SockFprog(len(program), program)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS failed")
    if libc.prctl(22, 2, ctypes.byref(filter_program), 0, 0) != 0:  # PR_SET_SECCOMP, SECCOMP_MODE_FILTER
        raise OSError(ctypes.get_errno(), "installing the seccomp filter failed")


def closed_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def environment(home: Path) -> dict[str, str]:
    keep = ("PATH", "LANG", "LC_ALL", "TMPDIR")
    env = {name: os.environ[name] for name in keep if name in os.environ}
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        AITP_SKIP_START="1",
    )
    return env


def run(binary: Path, args: list[str], env: dict[str, str]) -> str:
    result = subprocess.run(
        [str(binary), *args], env=env, capture_output=True, text=True, timeout=TIMEOUT, check=False,
        preexec_fn=deny_getrandom,
    )
    if result.returncode != 0:
        raise AssertionError(f"aitp {args[0]} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
    return result.stdout


def handshake(port: int, ca: Path) -> str:
    deadline = time.monotonic() + TIMEOUT
    while True:
        try:
            stream = socket.create_connection(("127.0.0.1", port), timeout=5)
            break
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)
    with stream:
        stream.sendall(f"CONNECT {TLS_HOST}:443 HTTP/1.1\r\nHost: {TLS_HOST}:443\r\n\r\n".encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = stream.recv(4096)
            if not chunk:
                raise AssertionError(f"the local proxy closed the CONNECT: {response!r}")
            response += chunk
        status = response.split(b"\r\n", 1)[0]
        if b" 200" not in status:
            raise AssertionError(f"the local proxy refused the CONNECT: {status!r}")
        context = ssl.create_default_context(cafile=str(ca))
        with context.wrap_socket(stream, server_hostname=TLS_HOST) as tls:
            return f"{tls.version()} {tls.cipher()[0]}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--binary", required=True, type=Path)
    binary = parser.parse_args().binary.resolve(strict=True)
    if platform.system() != "Linux" or platform.machine() not in ARCHITECTURES:
        raise SystemExit("the entropy check needs native Linux x86_64 or aarch64")
    # The filter works: in a child (this process keeps its own entropy),
    # getrandom fails with ENOSYS.
    number = ARCHITECTURES[platform.machine()][0]
    probe = subprocess.run(
        [
            sys.executable, "-I", "-c",
            "import ctypes, errno, sys\n"
            "libc = ctypes.CDLL(None, use_errno=True)\n"
            "libc.syscall.restype = ctypes.c_long\n"
            f"result = libc.syscall({number}, ctypes.create_string_buffer(1), 1, 0)\n"
            "sys.exit(0 if result == -1 and ctypes.get_errno() == errno.ENOSYS else 1)\n",
        ],
        preexec_fn=deny_getrandom, capture_output=True, text=True, timeout=TIMEOUT, check=False,
    )
    if probe.returncode != 0:
        raise AssertionError(f"getrandom does not fail with ENOSYS under the filter: {probe.stderr}")
    with tempfile.TemporaryDirectory(prefix="aitp-entropy-") as directory:
        home = Path(directory) / "home"
        home.mkdir()
        env = environment(home)
        version = run(binary, ["--version"], env).strip()
        run(binary, ["init", "--base-url", f"http://127.0.0.1:{closed_port()}", "--api-key", "TEST_ONLY_ENTROPY_KEY"], env)
        config = json.loads((home / ".aitp" / "config.json").read_text(encoding="utf-8"))
        ca = Path(config["ca_cert_path"])
        port = int(config["listen"].rsplit(":", 1)[1])
        log = (Path(directory) / "proxy.log").open("w")
        proxy = subprocess.Popen(
            [str(binary)], env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            preexec_fn=deny_getrandom,
        )
        try:
            session = handshake(port, ca)
            if proxy.poll() is not None:
                raise AssertionError(f"the local proxy exited ({proxy.returncode})")
        except BaseException:
            proxy.kill()
            proxy.wait(timeout=10)
            log.close()
            print((Path(directory) / "proxy.log").read_text(errors="replace"))
            raise
        proxy.kill()
        proxy.wait(timeout=10)
        log.close()
    print(f"{version}: initialization and a local TLS handshake ({session}) work with getrandom failing (ENOSYS)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
