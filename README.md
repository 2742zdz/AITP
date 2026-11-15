# AITP

AITP is a local proxy client for Claude Code and Codex. It runs on your
machine, points the official clients at a local proxy, and forwards their
traffic to an API relay Gateway that you choose with a base URL and an API
key. AITP marks the client settings, certificates and background service
it installs, and `aitp uninstall` removes them again.

## Install

Linux and macOS (bash; x86_64 and arm64):

```sh
curl -fsSL https://github.com/2742zdz/AITP/releases/latest/download/setup.sh | bash -s -- \
    --base-url https://gateway.example.com --api-key '<api_key>'
```

Windows (Windows PowerShell 5.1 or PowerShell 7; x64 and arm64):

```powershell
& ([scriptblock]::Create((irm https://github.com/2742zdz/AITP/releases/latest/download/setup.ps1))) -BaseUrl 'https://gateway.example.com' -ApiKey '<api_key>'
```

The installer reads `aitp-manifest.json` of the latest release, downloads
the program for your platform from that same release, checks its size and
SHA-256 against the manifest and installs it: `~/.local/bin/aitp`, or
`%LOCALAPPDATA%\AITP\bin\aitp.exe` (added to your user PATH) on Windows. An
installed program that is already current is not downloaded again; a newer
one is kept. A replaced program stays next to it as `aitp-previous`
(`aitp-previous.exe`). Then it runs `aitp init --base-url ... --api-key ...`,
which configures Claude Code and starts (or refreshes) the local proxy
service. Running the installer again updates AITP and applies the given
Gateway again.

| bash | PowerShell | |
| --- | --- | --- |
| `--base-url URL` | `-BaseUrl URL` | the Gateway (required) |
| `--api-key KEY` | `-ApiKey KEY` | the API key; omitted: asked for without echo |
| `--install-dir DIR` | `-InstallDir DIR` | another install directory (also `AITP_INSTALL_DIR`) |
| `--no-start` | `-NoStart` | configure, but do not start the local proxy (`aitp start` later) |
| `--codex` | `-Codex` | also configure Codex (`aitp init-codex`) |
| `--install-only` | `-InstallOnly` | only install or update the program |

Afterwards run Claude Code as usual (`claude`): `aitp init` points its user
settings (`~/.claude/settings.json`) at the local proxy. `aitp claude` works
too and starts the proxy first if it is not running. Codex goes through the
proxy with `aitp codex`.

The installer never prints the API key. It hands it to `aitp init` as a
command-line argument, the only way `aitp init` takes it, so it is visible
to other processes of the same machine while `aitp init` runs (and, in the
one-liners above, in your shell history: omit `--api-key` / `-ApiKey` to be
asked for it instead).

**Trust.** The installer trusts what it downloads because it comes over
HTTPS from GitHub; it checks the program against the manifest, but does not
verify the manifest's minisign signature itself. `aitp update` does (see
below). To check a release by hand before installing, see "To check the
keys yourself".

To remove AITP (services, settings, data and the program):

```sh
aitp uninstall
```

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
release asset with its size and SHA-256 and is signed with
[minisign](https://jedisct1.github.io/minisign/)'s legacy Ed25519 format
(`minisign -S -l`). The program accepts an update only if the manifest's
signature verifies with one of the two public keys built into it, and only
if the downloaded file matches the manifest.

| Key | Key id | Public key |
| --- | --- | --- |
| Release | `7B8244A1E282487E` | `RWR+SILioUSCe02FjXoH9VHY/swzxXGRUmz3ixLge+R1Q9YhspEvMs8A` |
| Recovery | `30D8EBD4CDEAE8F0` | `RWTw6OrN1OvYMNRnHzX9mhZugihDvcDXMqqpCGfKAIOxZwUnQU1sba/L` |

- The **release** key signs every release. Its private key is held only by
  the `Release` workflow, as a secret of the protected `release` environment
  (a required reviewer approves each publish run); it is not in this
  repository and no other job or pull request can read it.
- The **recovery** key's private key is kept offline by the maintainer. It
  signs by hand when the release key cannot be used (lost, or to be
  replaced), and lets a later version drop the release key.

Trade-off: whoever controls the maintainer's GitHub account (and so can
approve the `release` environment) can publish a release that installed
programs accept. This is accepted in exchange for releases without an
offline signing step; the recovery key stays out of reach of GitHub.

To check the keys yourself:

```sh
# the keys the program is built with (source of truth: src/bin/aitp/brand.rs)
python3 scripts/release_manifest.py keys --require-real

# a downloaded release, against one of the keys above
minisign -V -P RWR+SILioUSCe02FjXoH9VHY/swzxXGRUmz3ixLge+R1Q9YhspEvMs8A \
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

Releases are built and signed by the `Release` workflow
(`.github/workflows/release.yml`). A pull request, or a run by hand on a
branch, builds a preview of the six programs and the unsigned manifest; it
never sees the signing key. The manifest also lists the two installers,
`setup.sh` and `setup.ps1`, published with each release exactly as they are
in `scripts/` of the tagged commit.

1. Set the version in `Cargo.toml` (and `Cargo.lock`) on `main`, then push the
   tag `vX.Y.Z` for that commit. The workflow builds and checks the six
   programs, writes `aitp-manifest.json` and uploads them to a **draft**
   release; the run's summary lists the files' SHA-256.
2. Publish: run the workflow on the tag with the confirmation
   `gh workflow run Release --ref vX.Y.Z -f publish_confirmation='publish vX.Y.Z'`
   and approve the `release` environment. The run checks the draft against
   the digests the tag run recorded and regenerates the manifest from the
   programs (byte for byte), then signs it with the release key
   (`scripts/sign_manifest.py`), uploads `aitp-manifest.json.minisig` to the
   draft, verifies the signature with the keys built into the program and
   publishes the release (immutable). It becomes the latest release (the one
   `aitp update` reads) only if its version is higher than the current
   latest; publishing an older version leaves the latest release as it is
   (the run's summary says so).

Do not change the draft, or which release is the latest, while the publish
run is running: it re-reads the draft right before publishing, but cannot
lock it. Publish runs of different tags run one at a time; while one runs, a
newer waiting one cancels an older waiting one (start that again).

**Signing with the recovery key instead** (the release key unavailable): if
the draft already holds `aitp-manifest.json.minisig`, the publish run does
not sign; it only verifies that signature with the built-in keys. On the
offline signing machine, download `aitp-manifest.json` from the draft,
compare its SHA-256 with the tag run's summary, and sign it:

```sh
minisign -S -l -s aitp-recovery.key -p aitp-recovery.pub -m aitp-manifest.json \
    -x aitp-manifest.json.minisig -t "aitp-manifest X.Y.Z sha256:<manifest sha256>"
minisign -V -p aitp-recovery.pub -m aitp-manifest.json
```

Upload only `aitp-manifest.json.minisig` to the draft
(`gh release upload vX.Y.Z aitp-manifest.json.minisig`, or edit the draft),
optionally check it with `python3 scripts/release_manifest.py verify
--manifest aitp-manifest.json --dist <directory with the release files>`,
then publish as in step 2.

A failed tag run leaves no release or only a draft; delete the draft before
building the tag again. A published release cannot be changed: fix a problem
with a new version.

## License

MIT, see [LICENSE](LICENSE).
