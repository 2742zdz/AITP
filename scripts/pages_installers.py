#!/usr/bin/env python3
"""Copies the installers of a published release into the Pages site.

  pages_installers.py --release DIR --output DIR [--tag vX.Y.Z]

DIR holds aitp-manifest.json, aitp-manifest.json.minisig, setup.sh and
setup.ps1 as downloaded from one published release (the Pages workflow
downloads them; this script downloads nothing). The manifest's signature
must verify with a key built into the program (src/bin/aitp/brand.rs, as
the release workflow checks it, never a placeholder); the manifest must name
this distribution (and, with --tag, the tag's version) and list both
installers; each installer must match its size and SHA-256 there. Only then
are both written to the output directory, as the bytes that were checked.
A failed check exits 1 before anything is written.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("release_manifest", HERE / "release_manifest.py")
rm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rm)

CheckError = rm.CheckError
require = rm.require


def read_installer(path: Path) -> bytes:
    require(path.is_file() and not path.is_symlink(), f"installer is missing or not a regular file: {path}")
    return rm.read_bounded(path, rm.SCRIPT_MAX, "installer")


def checked_installers(release: Path, tag: str | None = None, keys: list[str] | None = None,
                       distribution: str | None = None) -> tuple[str, str, dict[str, bytes]]:
    """(signing key id, version, {installer name: its checked bytes})."""
    keys = rm.brand_keys() if keys is None else keys
    distribution = rm.brand_distribution() if distribution is None else distribution
    rm.require_real_keys(keys)
    manifest_path = release / rm.MANIFEST_NAME
    signature_path = release / (rm.MANIFEST_NAME + rm.SIGNATURE_SUFFIX)
    for path in (manifest_path, signature_path):
        require(not path.is_symlink(), f"not a regular file: {path}")
    manifest_bytes = rm.read_bounded(manifest_path, rm.MANIFEST_LIMIT, "manifest")
    signature_bytes = rm.read_bounded(signature_path, rm.SIGNATURE_LIMIT, "signature")
    key_id = rm.verify_signature(manifest_bytes, signature_bytes, keys)
    manifest = rm.parse_manifest(manifest_bytes, distribution)
    version = manifest["version"]
    if tag is not None:
        require(tag == f"v{version}", f"the manifest names version {version}, the release tag is {tag}")
    installers = {}
    for name in rm.SCRIPTS:
        expected = manifest["assets"].get(name)
        require(expected is not None, f"the manifest of {version} does not list {name}")
        data = read_installer(release / name)
        actual = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        require(actual == expected, f"{name} does not match the manifest of {version}")
        installers[name] = data
    return key_id, version, installers


def write_file(directory: Path, name: str, data: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.chmod(temporary, 0o644)
        os.replace(temporary, directory / name)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def publish_installers(release: Path, output: Path, tag: str | None = None, keys: list[str] | None = None,
                       distribution: str | None = None) -> tuple[str, str]:
    """Checks the release files and writes the installers to `output`;
    returns (signing key id, version)."""
    require(output.is_dir() and not output.is_symlink(), f"the output directory is missing: {output}")
    key_id, version, installers = checked_installers(release, tag, keys, distribution)
    for name, data in installers.items():
        write_file(output, name, data)
    return key_id, version


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release", required=True, type=Path, help="the downloaded release files")
    parser.add_argument("--output", required=True, type=Path, help="the site directory")
    parser.add_argument("--tag", help="the release tag; the manifest must name its version")
    args = parser.parse_args(argv)
    try:
        key_id, version = publish_installers(args.release, args.output, args.tag)
    except CheckError as error:
        print(f"pages_installers.py: {error}", file=sys.stderr)
        return 1
    print(f"installers of {version} (manifest signed by trusted key {key_id}) written to {args.output}: "
          f"{', '.join(rm.SCRIPTS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
