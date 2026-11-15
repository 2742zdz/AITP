#!/usr/bin/env python3
"""Tests check_asset.py and github_release.py on synthetic inputs."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_asset = load("check_asset")
check_release = load("check_release")
release_manifest = load("release_manifest")
sign_manifest = load("sign_manifest")
FIXTURES = SCRIPTS.parent / "tests" / "fixtures" / "minisign"
github_release = load("github_release")


def elf(machine: int = 62, interpreter: bool = False, needed: bool = False, extra: bytes = b"") -> bytes:
    """A minimal ELF64 executable: one executable PT_LOAD, optionally a
    PT_INTERP and a PT_DYNAMIC with DT_NEEDED."""
    headers = [(1, 5, 0, 0x400000, 0x1000, 0x1000)]
    dynamic = struct.pack("<qQ", 1 if needed else 0x6FFFFFFB, 0) + struct.pack("<qQ", 0, 0)
    if interpreter:
        headers.append((3, 4, 0x200, 0x400200, 16, 16))
    headers.append((2, 6, 0x300, 0x400300, len(dynamic), len(dynamic)))
    ident = b"\x7fELF" + bytes([2, 1, 1]) + bytes(9)
    data = bytearray(0x1000)
    data[0:64] = struct.pack("<16sHHIQQQIHHHHHH", ident, 2, machine, 1, 0x400100, 64, 0, 0, 64, 56,
                             len(headers), 64, 0, 0)
    for index, (kind, flags, offset, address, file_size, memory_size) in enumerate(headers):
        data[64 + index * 56:64 + (index + 1) * 56] = struct.pack("<IIQQQQQQ", kind, flags, offset, address,
                                                                    address, file_size, memory_size, 0x1000)
    data[0x300:0x300 + len(dynamic)] = dynamic
    return bytes(data) + extra


def macho(cpu: int = 0x0100000C, file_type: int = 2) -> bytes:
    command = struct.pack("<II", 0x19, 72) + bytes(64)  # one LC_SEGMENT_64-sized command
    return struct.pack("<IiiIIIII", 0xFEEDFACF, cpu, 0, file_type, 1, len(command), 0, 0) + command


def pe(machine: int = 0x8664, repro: bool = True, pdb: bytes = b"aitp.pdb", debug: bool = True) -> bytes:
    """A minimal PE32+ image with one section holding the debug directory."""
    data = bytearray(0x800)
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HH", data, 0x84, machine, 1)
    struct.pack_into("<H", data, 0x80 + 20, 240)
    optional = 0x80 + 24
    struct.pack_into("<H", data, optional, 0x20B)
    entries = []
    if pdb is not None:
        entries.append((2, 24 + len(pdb) + 1, 0x600))
    if repro:
        entries.append((16, 0, 0))
    if debug and entries:
        struct.pack_into("<II", data, optional + 160, 0x1000, 28 * len(entries))
    section = optional + 240
    struct.pack_into("<IIII", data, section + 8, 0x200, 0x1000, 0x200, 0x400)
    for index, (kind, size, pointer) in enumerate(entries):
        struct.pack_into("<IIHHIIII", data, 0x400 + index * 28, 0, 0, 0, 0, kind, size, 0, pointer)
    if pdb is not None:
        data[0x600:0x604] = b"RSDS"
        data[0x618:0x618 + len(pdb)] = pdb
    return bytes(data)


class CheckAssetTest(unittest.TestCase):
    def check(self, name: str, data: bytes) -> str:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / name
            path.write_bytes(data)
            return check_asset.check(path)

    def refuses(self, name: str, data: bytes, message: str) -> None:
        with self.assertRaisesRegex(check_asset.CheckError, message):
            self.check(name, data)

    def test_static_linux(self) -> None:
        self.assertEqual(self.check("aitp-linux-x86_64", elf()), "static ELF")
        self.assertEqual(self.check("aitp-linux-aarch64", elf(183)), "static ELF")
        self.refuses("aitp-linux-aarch64", elf(62), "machine")
        self.refuses("aitp-linux-x86_64", elf(interpreter=True), "dynamic loader")
        self.refuses("aitp-linux-x86_64", elf(needed=True), "shared library")
        self.refuses("aitp-linux-x86_64", elf(extra=b"memcpy@GLIBC_2.14"), "GNU C library")
        self.refuses("aitp-linux-x86_64", macho(), "not an ELF")
        self.refuses("aitp-linux-x86_64", elf()[:0x320], "loadable segment")

    def test_macos(self) -> None:
        self.assertEqual(self.check("aitp-macos-aarch64", macho()), "Mach-O executable")
        self.assertEqual(self.check("aitp-macos-x86_64", macho(0x01000007)), "Mach-O executable")
        self.refuses("aitp-macos-x86_64", macho(), "CPU type")
        self.refuses("aitp-macos-aarch64", macho(file_type=6), "not a Mach-O executable")
        self.refuses("aitp-macos-aarch64", elf(), "Mach-O")
        self.refuses("aitp-macos-aarch64", macho()[:28], "truncated|Mach-O")
        self.refuses("aitp-macos-aarch64", macho()[:60], "load commands")

    def test_windows(self) -> None:
        self.assertIn("/Brepro", self.check("aitp-windows-x86_64.exe", pe()))
        self.assertIn("/Brepro", self.check("aitp-windows-aarch64.exe", pe(0xAA64, pdb=None)))
        self.refuses("aitp-windows-aarch64.exe", pe(), "machine")
        self.refuses("aitp-windows-x86_64.exe", pe(repro=False), "Brepro")
        self.refuses("aitp-windows-x86_64.exe", pe(pdb=None, repro=False), "Brepro")
        self.refuses("aitp-windows-x86_64.exe", pe(pdb=b"D:\\a\\b\\aitp.pdb"), "PDB by a path")
        self.refuses("aitp-windows-x86_64.exe", pe(pdb=b"/x/aitp.pdb"), "PDB by a path")

    def test_installers(self) -> None:
        self.assertEqual(self.check("setup.sh", b"#!/usr/bin/env bash\necho\tok\n"), "ASCII script with LF line ends")
        self.assertEqual(self.check("setup.ps1", b"param()\n"), "ASCII script with LF line ends")
        self.refuses("setup.sh", b"#!/bin/sh\n", "does not start with")
        self.refuses("setup.ps1", b"param()\r\n", "CR line ends")
        self.refuses("setup.ps1", b"\xef\xbb\xbfparam()\n", "not ASCII")
        self.refuses("setup.ps1", "# caf\u00e9\n".encode(), "not ASCII")
        self.refuses("setup.ps1", b"param()", "line break")
        self.refuses("setup.ps1", b"a\x00\n", "control character")
        self.refuses("setup.ps1", b"", "not between")
        self.refuses("setup.ps1", b"#" * (1024 * 1024) + b"\n", "not between")
        # The installers of this checkout pass.
        for name in ("setup.sh", "setup.ps1"):
            self.assertEqual(check_asset.check(SCRIPTS / name), "ASCII script with LF line ends")

    def test_names(self) -> None:
        self.refuses("aitp-linux-riscv64", elf(), "not a release asset name")
        self.refuses("aitp-windows-x86_64", pe(), "not a release asset name")


class CheckReleaseTest(unittest.TestCase):
    def test_installers_name_no_other_distribution(self) -> None:
        environ = {"AITP_TEST_FOREIGN_NAMES": "otherdist"}
        with tempfile.TemporaryDirectory() as directory:
            build = Path(directory) / "build" / "dir"
            for name, text, expected in [
                ("setup.sh", "#!/usr/bin/env bash\n# KTest\n", "ktest"),
                ("setup.ps1", "# OtherDist installer\n", "otherdist"),
            ]:
                path = Path(directory) / name
                path.write_text(text)
                with self.assertRaisesRegex(check_release.CheckError, f"names another distribution.*{expected}"):
                    with contextlib.redirect_stdout(io.StringIO()):
                        check_release.check([path], [build], environ)
            # The installers of this checkout pass (no build path or keys needed).
            with contextlib.redirect_stdout(io.StringIO()) as output:
                check_release.check([SCRIPTS / "setup.sh", SCRIPTS / "setup.ps1"], [build], environ)
            self.assertIn("2 installer(s)", output.getvalue())

    def test_build_paths_in_utf8_and_utf16(self) -> None:
        path = "D:\\a\\AITP\\AITP"
        for encoding in ("utf-8", "utf-16-le"):
            data = b"x" + "d:/a/aitp/AITP\\src\\foo.c".encode(encoding) + b"y"
            self.assertTrue(check_release.carries_path(check_release.path_view(data), path), encoding)
        neutral = b"/aitp/src/foo.c\0" + "/aitp/src/foo.c".encode("utf-16-le") + b"\0\0"
        for path in ("/home/runner/work/AITP/AITP", "/src"):
            self.assertFalse(check_release.carries_path(check_release.path_view(neutral), path), path)
        # A short build path outside a neutral prefix is still found.
        for encoding in ("utf-8", "utf-16-le"):
            data = "/src/foo.c".encode(encoding)
            self.assertTrue(check_release.carries_path(check_release.path_view(data), "/src"), encoding)


class GithubReleaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.dist = Path(self.directory.name)
        self.files = {"aitp-manifest.json": b"{}\n", "aitp-linux-x86_64": b"x" * 100}
        for name, data in self.files.items():
            (self.dist / name).write_bytes(data)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def release(self, **changes) -> dict:
        release = {
            "tag_name": "v1.2.3",
            "draft": True,
            "prerelease": False,
            "immutable": False,
            "assets": [
                {"id": index, "name": name, "state": "uploaded", "size": len(data),
                 "digest": "sha256:" + hashlib.sha256(data).hexdigest()}
                for index, (name, data) in enumerate(self.files.items(), 1)
            ],
        }
        release.update(changes)
        return release

    def verify(self, release: dict, state: str = "draft", names=None) -> None:
        github_release.verify(release, self.dist, "v1.2.3", state, list(names or self.files))

    def test_draft_and_published(self) -> None:
        self.verify(self.release())
        self.verify(self.release(draft=False, immutable=True), "published")
        with self.assertRaisesRegex(github_release.CheckError, "not immutable"):
            self.verify(self.release(draft=False, immutable=False), "published")
        with self.assertRaisesRegex(github_release.CheckError, "still a draft"):
            self.verify(self.release(), "published")
        with self.assertRaisesRegex(github_release.CheckError, "already immutable"):
            self.verify(self.release(immutable=True))
        with self.assertRaisesRegex(github_release.CheckError, "prerelease"):
            self.verify(self.release(prerelease=True))
        with self.assertRaisesRegex(github_release.CheckError, "has tag"):
            self.verify(self.release(tag_name="v1.2.4"))

    def test_assets_must_match_exactly(self) -> None:
        release = self.release()
        release["assets"][1]["digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(github_release.CheckError, "digest"):
            self.verify(release)
        release = self.release()
        release["assets"][1]["size"] = 99
        with self.assertRaisesRegex(github_release.CheckError, "bytes"):
            self.verify(release)
        release = self.release()
        release["assets"][1]["state"] = "starter"
        with self.assertRaisesRegex(github_release.CheckError, "not uploaded"):
            self.verify(release)
        release = self.release()
        release["assets"].append(dict(release["assets"][1]))
        with self.assertRaisesRegex(github_release.CheckError, "repeats"):
            self.verify(release)
        with self.assertRaisesRegex(github_release.CheckError, "expected"):
            self.verify(self.release(), names=["aitp-manifest.json"])

    def test_make_latest_only_for_a_higher_version(self) -> None:
        make_latest = github_release.make_latest
        self.assertTrue(make_latest("v0.1.0", ""))
        self.assertTrue(make_latest("v0.2.0", "v0.1.9"))
        self.assertTrue(make_latest("v0.10.0", "v0.9.0"))  # numeric, not text order
        self.assertTrue(make_latest("v1.0.0", "v0.99.99"))
        self.assertFalse(make_latest("v0.1.1", "v0.2.0"))
        self.assertFalse(make_latest("v0.9.0", "v0.10.0"))
        for tag, latest in (("v1.2.3", "v1.2.3"), ("v1.2", "v1.0.0"), ("v1.2.3", "1.0.0"), ("v1.2.3", "v01.0.0")):
            with self.assertRaises(github_release.CheckError, msg=f"{tag} {latest}"):
                make_latest(tag, latest)

    def test_tag_run(self) -> None:
        sha = "a" * 40
        good = {"id": 5, "name": "Release", "path": ".github/workflows/release.yml", "event": "push",
                "head_branch": "v1.2.3", "head_sha": sha, "status": "completed", "conclusion": "success"}
        newer = dict(good, id=7)
        self.assertEqual(github_release.tag_run([good, newer], "v1.2.3", sha)["id"], 7)
        for change in ({"event": "workflow_dispatch"}, {"head_branch": "main"}, {"head_sha": "b" * 40},
                       {"conclusion": "failure"}, {"path": ".github/workflows/ci.yml"}, {"id": "5"}):
            with self.assertRaises(github_release.CheckError, msg=json.dumps(change)):
                github_release.tag_run([dict(good, **change)], "v1.2.3", sha)


def throwaway_key() -> tuple[bytes, bytes, str]:
    """(key id, seed, minisign public key line) of a key made for one test."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    seed = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                 serialization.NoEncryption())
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key_id = os.urandom(8)
    return key_id, seed, base64.b64encode(b"Ed" + key_id + public).decode()


class SignManifestTest(unittest.TestCase):
    manifest = (FIXTURES / "manifest.json").read_bytes()  # distribution ktest, version 9.9.9

    def sign(self, key_id: bytes, seed: bytes, trusted: list[str]) -> bytes:
        return sign_manifest.sign(self.manifest, key_id, seed, trusted, release_manifest, distribution="ktest")

    def test_the_signature_verifies_like_minisign(self) -> None:
        key_id, seed, public = throwaway_key()
        other = throwaway_key()[2]
        signed = self.sign(key_id, seed, [other, public])
        self.assertEqual(release_manifest.verify_signature(self.manifest, signed, [public]),
                         release_manifest.key_id_text(key_id))
        lines = signed.decode().splitlines()
        digest = hashlib.sha256(self.manifest).hexdigest()
        self.assertEqual(lines[2], f"trusted comment: aitp-manifest 9.9.9 sha256:{digest}")
        self.assertTrue(lines[0].startswith("untrusted comment: "))
        # Ed25519 is deterministic: the same key signs the same bytes.
        self.assertEqual(signed, self.sign(key_id, seed, [public]))
        with self.assertRaisesRegex(release_manifest.CheckError, "does not verify"):
            release_manifest.verify_signature(self.manifest + b" ", signed, [public])
        with self.assertRaisesRegex(release_manifest.CheckError, "not trusted"):
            release_manifest.verify_signature(self.manifest, signed, [other])

    def test_the_fixture_signed_by_the_tool_verifies(self) -> None:
        # The Rust client verifies the same fixture (update_signature_tests.rs).
        public = (FIXTURES / "workflow.pub").read_text().splitlines()[1]
        signed = (FIXTURES / "manifest.workflow.minisig").read_bytes()
        release_manifest.verify_signature(self.manifest, signed, [public])

    def test_only_a_trusted_key_signs(self) -> None:
        key_id, seed, public = throwaway_key()
        other_id, other_seed, other = throwaway_key()
        with self.assertRaisesRegex(sign_manifest.SignError, "not one the AITP program trusts"):
            self.sign(key_id, seed, [other])
        # The right key id with another key's seed.
        with self.assertRaisesRegex(sign_manifest.SignError, "not one the AITP program trusts"):
            self.sign(key_id, other_seed, [public])

    def run_main(self, value: str | None, output: Path, manifest: Path) -> tuple[int, str]:
        environ = {} if value is None else {"AITP_SIGNING_KEY": value}
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), contextlib.redirect_stdout(io.StringIO()):
            status = sign_manifest.main(["--manifest", str(manifest), "--output", str(output)], environ)
        self.assertNotIn("AITP_SIGNING_KEY", environ, "the key variable is removed once read")
        return status, errors.getvalue()

    def test_bad_keys_fail_without_showing_them(self) -> None:
        key_id, seed, _ = throwaway_key()
        good = base64.b64encode(key_id + seed).decode()
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "aitp-manifest.json"
            manifest.write_bytes(self.manifest)
            output = Path(directory) / "aitp-manifest.json.minisig"
            for value, message in (
                (None, "not set or empty"),
                ("", "not set or empty"),
                ("not base64 at all!" + good[:20], "not base64"),
                (base64.b64encode(key_id + seed[:31]).decode(), "not 40 bytes"),
                (good.rstrip("="), "not base64"),
                # 40 bytes end in "X==" where X carries 4 unused bits: set one.
                (good[:-3] + chr(ord(good[-3]) + 1) + "==", "not canonical base64"),
                # A real key the brand does not trust.
                (good, "not one the AITP program trusts"),
            ):
                status, errors = self.run_main(value, output, manifest)
                self.assertEqual(status, 1, value)
                self.assertIn(message, errors)
                self.assertFalse(output.exists())
                for secret in filter(None, (value, good, seed.hex(), base64.b64encode(seed).decode())):
                    self.assertNotIn(secret, errors)

    def test_an_existing_signature_is_not_overwritten(self) -> None:
        key_id, seed, _ = throwaway_key()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "aitp-manifest.json.minisig"
            output.write_bytes(b"kept")
            status, errors = self.run_main(base64.b64encode(key_id + seed).decode(), output,
                                           FIXTURES / "manifest.json")
            self.assertEqual(status, 1)
            self.assertIn("already exists", errors)
            self.assertEqual(output.read_bytes(), b"kept")


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    unittest.main()
