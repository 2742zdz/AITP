#!/usr/bin/env python3
"""End-to-end tests of setup.sh and setup.ps1 against a fake GitHub.

A local HTTP server lays out releases like GitHub (AITP_SETUP_RELEASES_URL
points the installers at it): /releases/download/v<version>/<file> holds
aitp-manifest.json, a stub program for every platform, setup.sh and
setup.ps1, and /releases/latest/download/<file> redirects (302) to the
latest release's file. The stub answers `--version` with "aitp <version>" and
records every other invocation (arguments and the AITP_* variables the
installer sets) in AITP_STUB_LOG.

  python3 scripts/test_setup.py              # bash, and pwsh/powershell if found

setup.sh runs under SETUP_TEST_BASH (default: bash; on macOS CI /bin/bash,
bash 3.2). setup.ps1 runs under every PowerShell found (pwsh, and Windows
PowerShell on Windows), or only under SETUP_TEST_POWERSHELL when set (a path,
or "none"). On Windows the stub is a small console program compiled with
Add-Type; elsewhere it is a shell script.
"""

from __future__ import annotations

import functools
import hashlib
import http.server
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
WINDOWS = os.name == "nt"
VERSION = "1.2.3"
PROGRAMS = (
    "aitp-linux-x86_64",
    "aitp-linux-aarch64",
    "aitp-macos-x86_64",
    "aitp-macos-aarch64",
    "aitp-windows-x86_64.exe",
    "aitp-windows-aarch64.exe",
)
# Shell metacharacters, quotes, spaces, a backslash before a quote and
# non-ASCII: the key must reach `aitp init` unchanged and never be printed.
KEY = "sk-te st'k\"ey\\\" $(touch pwned) `id` ;&|<>*?%PATH%!é"
PADDING = 1024 * 1024 + 64
BASE_URL = "https://gateway.example.com"

# The stub keeps which proxies run as files active-<environment> in
# AITP_STUB_STATE: `status` reports them, `stop`/`start` change them, `init`
# (claude) and `init-codex` (codex) start theirs unless AITP_SKIP_START=1.
# AITP_STUB_FAIL=<command> makes that command fail. Calls other than
# `status` are logged.
STUB_SH = """#!/bin/sh
# aitp test stub {version} {variant}
if [ "$1" = --version ]; then echo "aitp {version}"; exit 0; fi
environment=claude
command="$1"
if [ "$1" = --environment ]; then environment="$2"; command="$3"; fi
[ "$command" != init-codex ] || environment=codex
state="${{AITP_STUB_STATE:-}}"
if [ "$command" = status ]; then
  if [ -n "$state" ] && [ -e "$state/active-$environment" ]; then echo "service active: yes"; else echo "service active: no"; fi
  exit 0
fi
{{
  echo "--- call"
  echo "AITP_BINARY_UPDATED=${{AITP_BINARY_UPDATED-unset}}"
  echo "AITP_SKIP_START=${{AITP_SKIP_START-unset}}"
  for argument in "$@"; do printf 'arg=%s\\n' "$argument"; done
}} >> "$AITP_STUB_LOG"
[ "${{AITP_STUB_FAIL:-}}" != "$command" ] || exit 3
if [ -n "$state" ]; then
  case "$command" in
    stop) rm -f "$state/active-$environment" ;;
    start) : > "$state/active-$environment" ;;
    init|init-codex) [ "${{AITP_SKIP_START:-}}" = 1 ] || : > "$state/active-$environment" ;;
  esac
fi
exit 0
"""

STUB_CS = """
using System;
using System.IO;
using System.Text;
public static class Stub {{
    public static int Main(string[] args) {{
        if (args.Length == 1 && args[0] == "--version") {{ Console.WriteLine("aitp {version}"); return 0; }}
        string environment = "claude", command = args.Length > 0 ? args[0] : "";
        if (args.Length >= 3 && args[0] == "--environment") {{ environment = args[1]; command = args[2]; }}
        if (command == "init-codex") environment = "codex";
        string state = Environment.GetEnvironmentVariable("AITP_STUB_STATE") ?? "";
        string active = state.Length > 0 ? Path.Combine(state, "active-" + environment) : "";
        if (command == "status") {{
            Console.WriteLine(active.Length > 0 && File.Exists(active) ? "service active: yes" : "service active: no");
            return 0;
        }}
        var log = new StringBuilder();
        log.Append("--- call\\n");
        log.Append("AITP_BINARY_UPDATED=" + (Environment.GetEnvironmentVariable("AITP_BINARY_UPDATED") ?? "unset") + "\\n");
        log.Append("AITP_SKIP_START=" + (Environment.GetEnvironmentVariable("AITP_SKIP_START") ?? "unset") + "\\n");
        foreach (var argument in args) log.Append("arg=" + argument + "\\n");
        File.AppendAllText(Environment.GetEnvironmentVariable("AITP_STUB_LOG"), log.ToString(), new UTF8Encoding(false));
        if (Environment.GetEnvironmentVariable("AITP_STUB_FAIL") == command) return 3;
        if (active.Length > 0) {{
            if (command == "stop") File.Delete(active);
            else if (command == "start") File.WriteAllText(active, "");
            else if ((command == "init" || command == "init-codex") && Environment.GetEnvironmentVariable("AITP_SKIP_START") != "1") File.WriteAllText(active, "");
        }}
        return 0; // {variant}
    }}
}}
"""


def stub(directory: Path, version: str, variant: str = "") -> bytes:
    """A stub program of at least 1 MiB reporting `version`."""
    if WINDOWS:
        source = directory / f"stub-{version}{variant}.cs"
        output = directory / f"stub-{version}{variant}.exe"
        source.write_text(STUB_CS.format(version=version, variant=variant), encoding="utf-8")
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Add-Type -OutputType ConsoleApplication -OutputAssembly $env:STUB_OUTPUT "
             "-TypeDefinition ([IO.File]::ReadAllText($env:STUB_SOURCE))"],
            env=dict(os.environ, STUB_SOURCE=str(source), STUB_OUTPUT=str(output)), check=True,
        )
        data = output.read_bytes()
        return data + b"\0" * max(0, PADDING - len(data))
    text = STUB_SH.format(version=version, variant=variant)
    return text.encode() + b"# " + b"x" * PADDING + b"\n"


def canonical(assets: dict, version: str = VERSION, **override) -> bytes:
    manifest = {"assets": assets, "distribution": "aitp", "manifest_schema": 1, "version": version}
    manifest.update(override)
    return (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()


def entry(data: bytes) -> dict:
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


class Mirror:
    """Releases laid out like GitHub's on a local HTTP server; records
    requests. `latest` is the tag that /releases/latest/download/ redirects to."""

    def __init__(self, root: Path):
        self.root = root
        self.latest = f"v{VERSION}"
        # When set, `latest` becomes this tag once a manifest was served.
        self.move_to: str | None = None
        self.requests: list[str] = []
        mirror = self

        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                mirror.requests.append(self.path)
                prefix = "/releases/latest/download/"
                if self.path.startswith(prefix):
                    self.send_response(302)
                    self.send_header("Location", f"/releases/download/{mirror.latest}/{self.path[len(prefix):]}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                super().do_GET()
                if mirror.move_to and self.path.endswith("/aitp-manifest.json"):
                    mirror.latest = mirror.move_to

        handler = functools.partial(Handler, directory=str(root))
        class Server(http.server.ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                pass  # a client that stops reading (curl --max-filesize)

        self.server = Server(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.releases = f"{self.url}/releases"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def directory(self, tag: str | None = None) -> Path:
        return self.root / "releases" / "download" / (tag or self.latest)

    def publish(self, files: dict[str, bytes], manifest: bytes | None = None, tag: str | None = None):
        directory = self.directory(tag)
        directory.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (directory / name).write_bytes(data)
        if manifest is None:
            manifest = canonical({name: entry(data) for name, data in files.items()})
        (directory / "aitp-manifest.json").write_bytes(manifest)
        for name in ("setup.sh", "setup.ps1"):
            shutil.copyfile(HERE / name, directory / name)


class SetupTests:
    """Shared cases; subclasses run one installer."""

    asset: str
    program: str

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.top = Path(self.temporary.name)
        self.home = self.top / "home"
        self.home.mkdir()
        self.install = self.top / "install dir"
        self.log = self.top / "stub.log"
        self.state = self.top / "state"
        self.state.mkdir()
        self.mirror = Mirror(self.top / "mirror")
        self.current = stub(self.top, VERSION)
        self.files = {name: self.current for name in PROGRAMS}
        self.mirror.publish(self.files)

    def tearDown(self):
        self.mirror.close()
        self.temporary.cleanup()

    # -- helpers --

    def environment(self) -> dict:
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("AITP_") and key.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
        environment.update(HOME=str(self.home), AITP_STUB_LOG=str(self.log), AITP_STUB_STATE=str(self.state),
                           NO_PROXY="*", no_proxy="*",
                           AITP_SETUP_RELEASES_URL=self.mirror.releases)
        return environment

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        calls = []
        for line in self.log.read_text(encoding="utf-8").splitlines():
            if line == "--- call":
                calls.append([])
            else:
                calls[-1].append(line)
        return calls

    def installed(self) -> Path:
        return self.install / self.program

    def leftovers(self) -> list[str]:
        if not self.install.exists():
            return []
        return sorted(path.name for path in self.install.iterdir() if path.name.startswith(".aitp."))

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(KEY, result.stdout + result.stderr)

    def assert_failed(self, result, message: str):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(message, result.stdout + result.stderr)
        self.assertNotIn(KEY, result.stdout + result.stderr)
        self.assertFalse(self.installed().exists(), "nothing may be installed")
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(self.calls(), [])

    def init_call(self, updated: str, skip: str, base: str | None = None) -> list[str]:
        return [f"AITP_BINARY_UPDATED={updated}", f"AITP_SKIP_START={skip}",
                "arg=init", "arg=--base-url", f"arg={base or BASE_URL}", "arg=--api-key", f"arg={KEY}"]

    # -- cases --

    def test_happy_path_then_up_to_date(self):
        self.assert_ok(self.run_setup())
        self.assertEqual(self.installed().read_bytes(), self.current)
        self.assertEqual(self.calls(), [self.init_call("1", "")])
        # The manifest of the latest release, the program of that version.
        self.assertEqual(self.mirror.requests, [
            "/releases/latest/download/aitp-manifest.json",
            f"/releases/download/v{VERSION}/aitp-manifest.json",
            f"/releases/download/v{VERSION}/{self.asset}",
        ])
        self.assertEqual(self.leftovers(), [])
        # Again: the same program is not downloaded again.
        self.mirror.requests.clear()
        result = self.run_setup()
        self.assert_ok(result)
        self.assertIn("download skipped", result.stderr)
        self.assertEqual(self.mirror.requests, ["/releases/latest/download/aitp-manifest.json",
                                                f"/releases/download/v{VERSION}/aitp-manifest.json"])
        self.assertEqual(self.calls()[-1], self.init_call("0", ""))

    def test_one_line_contract(self):
        result = self.run_contract()
        self.assert_ok(result)
        self.assertEqual(self.installed().read_bytes(), self.current)
        self.assertEqual(self.calls(), [self.init_call("1", "")])

    def test_no_start_and_codex(self):
        result = self.run_setup(no_start=True, codex=True)
        self.assert_ok(result)
        codex = [f"AITP_BINARY_UPDATED=0", "AITP_SKIP_START=1", "arg=init-codex", "arg=--base-url",
                 f"arg={BASE_URL}", "arg=--api-key", f"arg={KEY}"]
        self.assertEqual(self.calls(), [self.init_call("1", "1"), codex])
        self.assertRegex(result.stdout + result.stderr, r"next: start the local proxy with: .*aitp(\.exe)? start\n")

    def test_no_start_names_the_claude_environment(self):
        # A bare `start` would also refresh a separately selected Codex proxy.
        result = self.run_setup(no_start=True)
        self.assert_ok(result)
        self.assertRegex(result.stdout + result.stderr,
                         r"next: start the local proxy with: .*aitp(\.exe)? --environment claude start\n")

    def test_install_only(self):
        result = self.run_setup(base=None, key=None, install_only=True,
                                download_base=f"{self.mirror.releases}/download/v{VERSION}/")
        self.assert_ok(result)
        self.assertEqual(self.mirror.requests, [f"/releases/download/v{VERSION}/aitp-manifest.json",
                                                f"/releases/download/v{VERSION}/{self.asset}"])
        self.assertEqual(self.installed().read_bytes(), self.current)
        self.assertEqual(self.calls(), [])

    def test_the_base_url_goes_to_init_as_given(self):
        # Any form (`aitp init` normalizes it); the download does not use it.
        for base in ("https://gateway.example.com/v1/", "http://10.0.0.5:8080"):
            self.assert_ok(self.run_setup(base=base))
            self.assertEqual(self.calls()[-1][2:], self.init_call("1", "", base)[2:])

    def test_the_program_comes_from_the_manifests_release(self):
        # The latest release moved on after its manifest was read: the
        # program still comes from the release the manifest names.
        newer = stub(self.top, "1.2.4", "newer")
        self.mirror.publish({name: newer for name in PROGRAMS},
                            canonical({name: entry(newer) for name in PROGRAMS}, version="1.2.4"), tag="v1.2.4")
        self.mirror.move_to = "v1.2.4"
        self.assert_ok(self.run_setup())
        self.assertEqual(self.installed().read_bytes(), self.current)
        self.assertIn(f"/releases/download/v{VERSION}/{self.asset}", self.mirror.requests)
        self.assertFalse(any("latest/download/aitp-" in path and "manifest" not in path
                             for path in self.mirror.requests))

    def test_github_source(self):
        environment = self.environment()
        # No network in the test: the download fails, after naming the source.
        environment.update(https_proxy="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:9", NO_PROXY="", no_proxy="")
        del environment["AITP_SETUP_RELEASES_URL"]
        result = self.run_setup(environment=environment)
        self.assert_failed(result, "release manifest: https://github.com/2742zdz/AITP/releases/latest/download/aitp-manifest.json")
        self.assertEqual(self.mirror.requests, [])

    def test_wrong_sha256(self):
        bad = dict(entry(self.current), sha256="0" * 64)
        self.mirror.publish(self.files, canonical({name: bad for name in PROGRAMS}))
        self.assert_failed(self.run_setup(), "SHA-256")

    def test_wrong_size(self):
        for size in (len(self.current) - 1, len(self.current) + 1):
            bad = dict(entry(self.current), size=size)
            self.mirror.publish(self.files, canonical({name: bad for name in PROGRAMS}))
            self.assert_failed(self.run_setup(), self.asset)

    def test_missing_platform_asset(self):
        others = {name: entry(self.current) for name in PROGRAMS if name != self.asset}
        self.mirror.publish(self.files, canonical(others))
        self.assert_failed(self.run_setup(), f"lists {self.asset} 0 times")

    def test_malformed_manifests(self):
        good = {name: entry(self.current) for name in PROGRAMS}
        small = {name: dict(entry(self.current), size=1000) for name in PROGRAMS}
        for manifest in [
            canonical(good, distribution="other"),
            canonical(good, manifest_schema=2),
            canonical(good, channel="beta"),
            canonical(good, version="v1.2.3"),
            canonical(good)[:-1],
            canonical(good).replace(b"\n", b"\r\n"),
            json.dumps({"manifest_schema": 1, "distribution": "aitp", "version": VERSION, "assets": good}).encode() + b"\n",
            canonical(good).replace(b'"assets":{', b'"assets":{"' + self.asset.encode() + b'":' + json.dumps(entry(b"x")).replace(" ", "").encode() + b","),
        ]:
            self.mirror.publish(self.files, manifest)
            self.assert_failed(self.run_setup(), "aitp-setup:")
        self.mirror.publish(self.files, canonical(small))
        self.assert_failed(self.run_setup(), "not between")
        # Another asset listed twice (the program's own asset only once).
        installer = json.dumps(entry(b"x")).replace(" ", "").encode()
        twice = canonical(good).replace(b'"assets":{', b'"assets":{"setup.sh":' + installer + b',"setup.sh":' + installer + b",")
        self.mirror.publish(self.files, twice)
        self.assert_failed(self.run_setup(), "twice")

    def test_the_previous_program_link_is_replaced_not_followed(self):
        old = stub(self.top, "1.2.2", "old")
        self.install.mkdir()
        self.installed().write_bytes(old)
        self.installed().chmod(0o755)
        outside = self.top / "outside"
        outside.write_bytes(b"keep me")
        try:
            (self.install / self.previous).symlink_to(outside)
        except OSError as error:
            self.skipTest(f"cannot create a symbolic link here: {error}")
        self.assert_ok(self.run_setup())
        self.assertEqual(outside.read_bytes(), b"keep me")
        self.assertFalse((self.install / self.previous).is_symlink())
        self.assertEqual((self.install / self.previous).read_bytes(), old)
        self.assertEqual(self.installed().read_bytes(), self.current)

    def test_a_broken_program_is_replaced(self):
        self.install.mkdir()
        self.installed().write_bytes(b"\0" * 4096)
        self.installed().chmod(0o755)
        self.assert_ok(self.run_setup())
        self.assertEqual(self.installed().read_bytes(), self.current)
        self.assertEqual(self.calls()[-1], self.init_call("1", ""))

    def test_an_older_program_is_replaced_and_kept_as_previous(self):
        old = stub(self.top, "1.2.2", "old")
        self.install.mkdir()
        self.installed().write_bytes(old)
        self.installed().chmod(0o755)
        result = self.run_setup()
        self.assert_ok(result)
        self.assertIn("from aitp 1.2.2 to 1.2.3", result.stderr)
        self.assertEqual(self.installed().read_bytes(), self.current)
        self.assertEqual((self.install / self.previous).read_bytes(), old)
        self.assertEqual(self.calls()[-1], self.init_call("1", ""))
        self.assertEqual(self.leftovers(), [])

    def test_a_newer_program_is_kept(self):
        newer = stub(self.top, "1.3.0", "new")
        self.install.mkdir()
        self.installed().write_bytes(newer)
        self.installed().chmod(0o755)
        result = self.run_setup()
        self.assert_ok(result)
        self.assertIn("newer than 1.2.3; keeping it", result.stderr)
        self.assertEqual(self.installed().read_bytes(), newer)
        self.assertNotIn(f"/aitp/{self.asset}", self.mirror.requests)
        self.assertEqual(self.calls(), [self.init_call("0", "")])

    def test_plain_http_elsewhere_is_refused(self):
        environment = self.environment()
        environment["AITP_SETUP_RELEASES_URL"] = "http://releases.invalid/releases"
        self.assert_failed(self.run_setup(environment=environment), "plain http")

    def test_a_program_reporting_another_version_is_refused(self):
        other = stub(self.top, "9.9.9", "other")
        self.mirror.publish({name: other for name in PROGRAMS},
                            canonical({name: entry(other) for name in PROGRAMS}))
        self.assert_failed(self.run_setup(), "does not report version")


class BashSetupTests(SetupTests, unittest.TestCase):
    program = "aitp"
    previous = "aitp-previous"

    @classmethod
    def setUpClass(cls):
        if WINDOWS:
            raise unittest.SkipTest("setup.sh is for Linux and macOS")
        cls.bash = os.environ.get("SETUP_TEST_BASH", "bash")
        system = {"Linux": "linux", "Darwin": "macos"}[platform.system()]
        machine = platform.machine().lower()
        architecture = "aarch64" if machine in ("arm64", "aarch64") else "x86_64"
        if system == "macos" and subprocess.run(["sysctl", "-n", "hw.optional.arm64"], capture_output=True,
                                                text=True).stdout.strip() == "1":
            architecture = "aarch64"
        cls.asset = f"aitp-{system}-{architecture}"

    def run_setup(self, base=BASE_URL, key=KEY, download_base=None, no_start=False,
                  codex=False, install_only=False, environment=None):
        arguments = [self.bash, str(HERE / "setup.sh"), "--install-dir", str(self.install)]
        if base is not None:
            arguments += ["--base-url", base]
        if key is not None:
            arguments += ["--api-key", key]
        if download_base:
            arguments += ["--download-base", download_base]
        arguments += ["--no-start"] * no_start + ["--codex"] * codex + ["--install-only"] * install_only
        return subprocess.run(arguments, env=environment or self.environment(), capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=120)

    def test_macos_root_is_refused_before_anything_is_installed(self):
        fake = self.top / "fake-macos-root"
        fake.mkdir()
        for name, output in (("uname", "Darwin"), ("id", "0")):
            (fake / name).write_text(f"#!/bin/sh\necho {output}\n", encoding="utf-8")
            (fake / name).chmod(0o755)
        environment = self.environment()
        environment["PATH"] = f"{fake}{os.pathsep}{environment['PATH']}"
        self.assert_failed(self.run_setup(environment=environment), "do not run setup as root")

    def run_contract(self):
        # Exactly the documented one-liner (plus the test install directory).
        command = (f'curl -fsSL {self.mirror.releases}/latest/download/setup.sh | {self.bash} -s -- '
                   f'--base-url {BASE_URL} --api-key "$KEY" --install-dir "$DIR"')
        environment = self.environment()
        environment.update(KEY=KEY, DIR=str(self.install))
        return subprocess.run([self.bash, "-c", command], env=environment, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=120)

    def test_the_key_is_asked_for_without_a_terminal_fails(self):
        result = self.run_setup(key=None)
        self.assert_failed(result, "--api-key is required")

    def test_a_current_program_without_execute_permission_is_fixed(self):
        self.install.mkdir()
        self.installed().write_bytes(self.current)
        self.installed().chmod(0o644)
        self.assert_ok(self.run_setup())
        self.assertTrue(os.access(self.installed(), os.X_OK))
        self.assertEqual(self.calls(), [self.init_call("0", "")])


def powershells() -> list[str]:
    chosen = os.environ.get("SETUP_TEST_POWERSHELL")
    if chosen == "none":
        return []
    if chosen:
        return [chosen]
    return [name for name in ("pwsh", "powershell") if shutil.which(name)
            and (WINDOWS or name == "pwsh")]


class PowerShellSetupTests(SetupTests):
    program = "aitp.exe"
    previous = "aitp-previous.exe"
    asset = "aitp-windows-x86_64.exe"
    shell = ""

    def environment(self) -> dict:
        environment = super().environment()
        if not WINDOWS:
            environment["PROCESSOR_ARCHITECTURE"] = "AMD64"
        return environment

    @staticmethod
    def quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    def run_setup(self, base=BASE_URL, key=KEY, download_base=None, no_start=False,
                  codex=False, install_only=False, environment=None):
        # Through -Command with every value quoted, so the key arrives intact
        # in both PowerShells (-File re-parses quotes in Windows PowerShell).
        parts = [f"& {self.quote(str(HERE / 'setup.ps1'))}", "-InstallDir", self.quote(str(self.install))]
        if base is not None:
            parts += ["-BaseUrl", self.quote(base)]
        if key is not None:
            parts += ["-ApiKey", "$env:TEST_KEY"]
        if download_base:
            parts += ["-DownloadBase", self.quote(download_base)]
        parts += ["-NoStart"] * no_start + ["-Codex"] * codex + ["-InstallOnly"] * install_only
        return self.powershell(" ".join(parts), environment)

    def run_contract(self):
        # Exactly the documented one-liner (plus the test install directory).
        return self.powershell(
            f"& ([scriptblock]::Create((irm {self.mirror.releases}/latest/download/setup.ps1))) "
            f"-BaseUrl '{BASE_URL}' -ApiKey $env:TEST_KEY -InstallDir {self.quote(str(self.install))}")

    def powershell(self, command: str, environment=None):
        environment = dict(environment or self.environment())
        environment["TEST_KEY"] = KEY
        # A failure must fail the process (a thrown error, never `exit`).
        script = f"$ErrorActionPreference = 'Stop'; try {{ {command} }} catch {{ [Console]::Error.WriteLine($_.Exception.Message); [Environment]::Exit(1) }}"
        return subprocess.run([self.shell, "-NoProfile", "-NonInteractive", "-Command", script], env=environment,
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              stdin=subprocess.DEVNULL, timeout=180)

    def test_the_key_is_asked_for_without_a_terminal_fails(self):
        result = self.run_setup(key=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.installed().exists())

    def test_github_source(self):
        # No network in the test: every request goes to a closed local port
        # (WebClient uses the default proxy on both .NET runtimes).
        environment = self.environment()
        del environment["AITP_SETUP_RELEASES_URL"]
        result = self.powershell(
            "[Net.WebRequest]::DefaultWebProxy = New-Object Net.WebProxy('http://127.0.0.1:9'); "
            f"& {self.quote(str(HERE / 'setup.ps1'))} -InstallDir {self.quote(str(self.install))} "
            f"-BaseUrl {self.quote(BASE_URL)} -ApiKey $env:TEST_KEY", environment)
        self.assert_failed(result, "release manifest: https://github.com/2742zdz/AITP/releases/latest/download/aitp-manifest.json")
        self.assertIn("Download failed: https://github.com/2742zdz/AITP/releases/latest/download/aitp-manifest.json",
                      result.stderr)
        self.assertEqual(self.mirror.requests, [])

    def install_old_with_running_proxies(self):
        self.old = stub(self.top, "1.2.2", "old")
        self.install.mkdir()
        self.installed().write_bytes(self.old)
        self.installed().chmod(0o755)
        for environment in ("claude", "codex"):
            (self.state / f"active-{environment}").write_bytes(b"")

    def actions(self) -> list[str]:
        return [" ".join(line[4:] for line in call if line.startswith("arg="))
                for call in self.calls()]

    def test_running_proxies_are_stopped_and_started_again(self):
        self.install_old_with_running_proxies()
        self.assert_ok(self.run_setup())
        self.assertEqual(self.installed().read_bytes(), self.current)
        self.assertEqual(self.actions()[:2], ["--environment claude stop", "--environment codex stop"])
        self.assertTrue(self.actions()[2].startswith("init "))
        # init starts the Claude proxy; the Codex one is started again.
        self.assertEqual(self.actions()[3:], ["--environment codex start"])
        self.assertEqual(sorted(path.name for path in self.state.iterdir()), ["active-claude", "active-codex"])

    def test_a_failed_setup_starts_the_stopped_proxies_again(self):
        self.install_old_with_running_proxies()
        environment = self.environment()
        environment["AITP_STUB_FAIL"] = "init"
        result = self.run_setup(environment=environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("aitp init failed", result.stderr)
        self.assertNotIn(KEY, result.stdout + result.stderr)
        self.assertEqual(self.actions()[-2:], ["--environment claude start", "--environment codex start"])
        self.assertEqual(sorted(path.name for path in self.state.iterdir()), ["active-claude", "active-codex"])

    def test_a_failed_replacement_puts_the_previous_program_back(self):
        # Move-Replace from the script itself, with a source that cannot be
        # moved: the old program is moved aside, the move into place fails.
        self.install.mkdir()
        self.installed().write_bytes(b"old program")
        script = HERE / "setup.ps1"
        result = self.powershell(
            "$errors = $null; $ast = [Management.Automation.Language.Parser]::ParseFile("
            f"{self.quote(str(script))}, [ref]$null, [ref]$errors); "
            "$function = $ast.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] "
            "-and $node.Name -eq 'Move-Replace' }, $true) | Select-Object -First 1; "
            ". ([scriptblock]::Create($function.Extent.Text)); "
            f"Move-Replace {self.quote(str(self.install / 'missing.exe'))} {self.quote(str(self.installed()))} -Attempts 2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("the previous program is still installed", result.stderr)
        self.assertEqual(self.installed().read_bytes(), b"old program")
        self.assertEqual(self.leftovers(), [])

    def test_parses(self):
        result = self.powershell(
            "$errors = $null; $null = [Management.Automation.Language.Parser]::ParseFile("
            f"{self.quote(str(HERE / 'setup.ps1'))}, [ref]$null, [ref]$errors); "
            "if ($errors.Count) { throw ($errors | Out-String) }")
        self.assertEqual(result.returncode, 0, result.stderr)


for _shell in powershells():
    _name = "PowerShellSetupTests_" + "".join(c if c.isalnum() else "_" for c in Path(_shell).stem)
    globals()[_name] = type(_name, (PowerShellSetupTests, unittest.TestCase), {"shell": _shell})


class ScriptFormatTests(unittest.TestCase):
    def test_scripts_are_ascii_with_lf_line_ends(self):
        for name in ("setup.sh", "setup.ps1"):
            data = (HERE / name).read_bytes()
            self.assertTrue(data.isascii(), name)
            self.assertNotIn(b"\r", data, name)
            self.assertTrue(data.endswith(b"\n"), name)


if __name__ == "__main__":
    unittest.main()
