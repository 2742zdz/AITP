# AITP

AITP is a local proxy client for Claude Code and Codex. It runs on your
machine, points the official clients at a local proxy, and forwards their
traffic to an API relay Gateway that you choose with a base URL and an API
key. AITP marks the client settings, certificates and background service
it installs, and `aitp uninstall` removes them again.

## Usage

Configure Claude Code (and, separately, Codex) for a Gateway:

```sh
aitp init       --base-url https://gateway.example.com --api-key <api_key>
aitp init-codex --base-url https://gateway.example.com --api-key <api_key> [--websockets]
```

Then run the local proxy as a user service and launch a client through it:

```sh
aitp start            # install and start the user service
aitp status           # show the service status
aitp claude           # launch Claude Code through the local proxy
aitp codex            # launch Codex through the local proxy
aitp doctor           # check the proxy and the client configuration
```

Other commands:

```sh
aitp update                         # update the program (signed releases, see below)
aitp upstream-proxy [system|direct|http://host:port]   # the proxy AITP itself uses
aitp uninstall [--dry-run]          # remove AITP services, settings, data and program
aitp --help                         # all commands and options
```

Initialization uses provider-specific CA files and leaves editor settings
alone unless you opt in (`--configure-vscode-editor`; on Windows also
`--trust-vscode-ca`, which adds the local CA to the CurrentUser Root store).

## Updates and their signatures

`aitp update` downloads `aitp-manifest.json` and the program for your
platform from this repository's GitHub Releases. The manifest lists every
release asset with its size and SHA-256 and is signed offline by the
maintainer with [minisign](https://jedisct1.github.io/minisign/) (legacy
Ed25519 signatures, `minisign -S -l`). The program accepts an update only if
the manifest's signature verifies with one of the two public keys built into
it, and only if the downloaded file matches the manifest.

| Key | Key id | Public key |
| --- | --- | --- |
| Release (primary) | `F7823EF82178E6E0` | `RWTg5ngh+D6C99dBN+56y4B/nIF4hSSNbCG86wuZr4Knpg+Lc4KxnAgr` |
| Recovery | `30D8EBD4CDEAE8F0` | `RWTw6OrN1OvYMNRnHzX9mhZugihDvcDXMqqpCGfKAIOxZwUnQU1sba/L` |

The private keys are kept offline by the maintainer and are never stored in
this repository or its CI: releases are built and uploaded unsigned, and the
maintainer adds the signature.

To check the keys yourself:

```sh
# the keys the program is built with (source of truth: src/bin/aitp/brand.rs)
python3 scripts/release_manifest.py keys --require-real

# a downloaded release, against one of the keys above
minisign -V -P RWTg5ngh+D6C99dBN+56y4B/nIF4hSSNbCG86wuZr4Knpg+Lc4KxnAgr \
    -m aitp-manifest.json -x aitp-manifest.json.minisig
sha256sum aitp-*        # compare with the manifest
```

A key id is the 8 bytes that follow the 2-byte `Ed` algorithm tag in the
Base64-decoded public key, read as a little-endian number and shown in
hexadecimal (as minisign prints it).

## Building

The toolchain is pinned in `rust-toolchain.toml`.

```sh
cargo build --locked --release --bin aitp
cargo test --locked
```

`scripts/build-release.sh` builds the release program with the build
machine's directories remapped to neutral `/aitp/...` prefixes,
`scripts/check_release.py` checks its contents and `scripts/check_asset.py`
its format. Release programs are static Linux (musl) programs for x86_64 and
aarch64, macOS programs for x86_64 and arm64, and Windows programs for x86_64
and arm64.

## Releasing

Releases are built by the `Release` workflow (`.github/workflows/release.yml`);
the signing keys never enter CI. A pull request, or a run by hand on a branch,
builds a preview of the six programs and the unsigned manifest.

1. Set the version in `Cargo.toml` (and `Cargo.lock`) on `main`, then push the
   tag `vX.Y.Z` for that commit. The workflow builds and checks the six
   programs, writes `aitp-manifest.json` and uploads them to a **draft**
   release; the run's summary lists the files' SHA-256 and the signing
   command.
2. On the offline signing machine, download `aitp-manifest.json` from the
   draft, compare its SHA-256 with the run's summary, and sign it:

   ```sh
   minisign -S -l -s aitp-release.key -p aitp-release.pub -m aitp-manifest.json \
       -x aitp-manifest.json.minisig -t "aitp-manifest X.Y.Z sha256:<manifest sha256>"
   minisign -V -p aitp-release.pub -m aitp-manifest.json
   ```

3. Upload only `aitp-manifest.json.minisig` to the draft
   (`gh release upload vX.Y.Z aitp-manifest.json.minisig`, or edit the draft).
   Optionally check it first: `python3 scripts/release_manifest.py verify
   --manifest aitp-manifest.json --dist <directory with the six programs>`.
4. Publish: run the workflow on the tag with the confirmation
   `gh workflow run Release --ref vX.Y.Z -f publish_confirmation='publish vX.Y.Z'`
   and approve the `release` environment. It checks the draft against the
   digests the tag run recorded and the signature against the keys built
   into the program, then publishes the release (immutable). It becomes the
   latest release (the one `aitp update` reads) only if its version is
   higher than the current latest; publishing an older version leaves the
   latest release as it is (the run's summary says so). Do not change the
   draft while the publish run is running: it re-reads the draft right
   before publishing, but cannot lock it.

A failed tag run leaves no release or only a draft; delete the draft before
building the tag again. A published release cannot be changed: fix a problem
with a new version.

## License

MIT, see [LICENSE](LICENSE).
