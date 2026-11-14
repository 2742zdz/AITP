#!/usr/bin/env python3
"""Tests check_asset.py and github_release.py on synthetic inputs."""

from __future__ import annotations

import hashlib
import importlib.util
import json
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

    def test_names(self) -> None:
        self.refuses("aitp-linux-riscv64", elf(), "not a release asset name")
        self.refuses("aitp-windows-x86_64", pe(), "not a release asset name")


class CheckReleaseTest(unittest.TestCase):
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


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    unittest.main()
