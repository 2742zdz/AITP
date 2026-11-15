#!/usr/bin/env python3
"""Signs the release manifest with the release key held by the workflow.

Usage: sign_manifest.py --manifest FILE --output FILE [--key-variable NAME]

The key comes from the environment variable NAME (default AITP_SIGNING_KEY):
base64 of the key id (8 bytes, in minisign's order: the 8 bytes after "Ed"
in the public key) followed by the Ed25519 seed (32 bytes). It is never
printed or written anywhere; the variable is removed from this process's
environment once read.

The signature is minisign's legacy format (`minisign -S -l`), as the
program verifies it (src/update_signature.rs): plain Ed25519 over the
manifest's exact bytes, and the trusted comment
"aitp-manifest X.Y.Z sha256:<manifest sha256>" signed with it. The key must
be one the AITP program trusts (its public key and key id are in
src/bin/aitp/brand.rs), the manifest must be a valid AITP manifest, and the
written signature is verified with the trusted keys before this exits 0.
OUTPUT must not exist yet.

Exits 0 when signed, 1 otherwise; messages never include key material.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import importlib.util
import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
KEY_LENGTH = 8 + 32


class SignError(Exception):
    pass


def load_release_manifest():
    spec = importlib.util.spec_from_file_location("release_manifest", SCRIPTS / "release_manifest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_key(environ, name: str) -> tuple[bytes, bytes]:
    """(key id, seed) from the variable; the variable is removed."""
    value = environ.pop(name, None)
    if not value:
        raise SignError(f"the signing key variable {name} is not set or empty")
    try:
        raw = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise SignError(f"the signing key in {name} is not base64") from None
    finally:
        value = None
    if len(raw) != KEY_LENGTH:
        raise SignError(f"the signing key in {name} is not {KEY_LENGTH} bytes (key id and Ed25519 seed)")
    return raw[:8], raw[8:]


def sign(manifest: bytes, key_id: bytes, seed: bytes, trusted_keys: list[str], tool,
         distribution: str | None = None) -> bytes:
    # Imported here: the tests and the checks above need no third-party module.
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.from_private_bytes(seed)
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    trusted = {}
    for text in trusted_keys:
        trusted_id, trusted_key = tool.parse_public_key(text)
        trusted[trusted_id] = trusted_key
    if trusted.get(key_id) != public:
        raise SignError("the signing key is not one the AITP program trusts (key id and public key)")
    distribution = distribution or tool.brand_distribution()
    version = tool.parse_manifest(manifest, distribution)["version"]
    digest = hashlib.sha256(manifest).hexdigest()
    comment = f"aitp-manifest {version} sha256:{digest}".encode()
    signature = private.sign(manifest)
    global_signature = private.sign(signature + comment)
    text = b"".join([
        b"untrusted comment: signature from the AITP release workflow\n",
        base64.b64encode(b"Ed" + key_id + signature), b"\n",
        b"trusted comment: ", comment, b"\n",
        base64.b64encode(global_signature), b"\n",
    ])
    # The written file verifies with the program's trusted keys.
    tool.verify_signature(manifest, text, trusted_keys)
    return text


def main(argv=None, environ=os.environ) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--key-variable", default="AITP_SIGNING_KEY")
    args = parser.parse_args(argv)
    tool = load_release_manifest()
    try:
        key_id, seed = read_key(environ, args.key_variable)
        if args.output.exists() or args.output.is_symlink():
            raise SignError(f"{args.output} already exists")
        manifest = tool.read_bounded(args.manifest, tool.MANIFEST_LIMIT, "manifest")
        keys = tool.brand_keys()
        tool.require_real_keys(keys)
        text = sign(manifest, key_id, seed, keys, tool)
        seed = None
        with args.output.open("xb") as output:
            output.write(text)
    except (SignError, tool.CheckError, OSError, ValueError) as error:
        print(f"sign_manifest.py: {error}", file=sys.stderr)
        return 1
    print(f"signed {args.manifest} with key {tool.key_id_text(key_id)}: {args.output}")
    return 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
