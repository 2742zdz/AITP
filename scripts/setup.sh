#!/usr/bin/env bash
# AITP installer for Linux and macOS (bash 3.2 or later).
#
#   curl -fsSL https://github.com/2742zdz/AITP/releases/latest/download/setup.sh | bash -s -- \
#       --base-url https://gateway.example.com --api-key '<api_key>'
#
# Downloads aitp-manifest.json of the latest GitHub release, checks it,
# downloads the program for this platform from that same release (by its
# version, so a release published meanwhile cannot mix in), checks its size
# and SHA-256 against the manifest, installs it atomically (default
# ~/.local/bin/aitp), then runs `aitp init`, which configures Claude Code and
# starts (or refreshes) the local proxy service.
#
# Trust: the manifest and the program are trusted because they come over
# HTTPS from GitHub. This script does not check the manifest's minisign
# signature; `aitp update` does (see README).
#
# For tests only: --download-base URL (or AITP_SETUP_DOWNLOAD_BASE) reads the
# manifest and the program from one flat directory instead, and
# AITP_SETUP_RELEASES_URL replaces https://github.com/2742zdz/AITP/releases.
#
# Everything runs from main() at the end, so `curl ... | bash` executes only
# a completely received script, and no command reads the rest of the script
# from standard input.

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: setup.sh --base-url URL --api-key KEY [options]

  --base-url URL       The Gateway base URL (passed to `aitp init`).
  --api-key KEY        The Gateway API key. Omitted: asked for on the terminal.
  --install-dir DIR    Install directory (default: ~/.local/bin).
  --no-start           Configure, but do not start the local proxy service.
  --codex              Also configure Codex (`aitp init-codex`).
  --install-only       Only install or update the program; configure nothing
                       (--base-url and --api-key are then optional).
  -h, --help           Show this help.

AITP is downloaded from the latest release of https://github.com/2742zdz/AITP.
Environment (lower precedence than the flags): AITP_INSTALL_DIR.
EOF
}

say() {
  printf 'aitp-setup: %s\n' "$*" >&2
}

die() {
  say "error: $*"
  exit 1
}

GITHUB_RELEASES="https://github.com/2742zdz/AITP/releases"
MANIFEST_NAME="aitp-manifest.json"
MANIFEST_LIMIT=1048576
PROGRAM_MIN=1048576
PROGRAM_MAX=67108864
VERSION_PATTERN='(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})'
ASSET_NAME_PATTERN='[A-Za-z0-9][A-Za-z0-9._-]*'
ENTRY_PATTERN='\{"sha256":"[0-9a-f]{64}","size":(0|[1-9][0-9]{0,9})\}'

# --- small helpers ----------------------------------------------------------

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print tolower($1)}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print tolower($1)}'
  elif command -v openssl >/dev/null 2>&1; then
    openssl dgst -sha256 "$1" | awk '{print tolower($NF)}'
  else
    die "no SHA-256 tool found (sha256sum, shasum or openssl)"
  fi
}

file_size() {
  wc -c <"$1" | tr -d '[:space:]'
}

strip_slashes() {
  local url="$1"
  while [ "${url%/}" != "${url}" ]; do url="${url%/}"; done
  printf '%s\n' "${url}"
}

# An absolute http(s) URL without credentials, query, fragment or spaces.
check_url() {
  local what="$1" url="$2"
  if ! printf '%s\n' "${url}" | grep -Eq '^https?://([A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(:[0-9]{1,5})?(/[^[:space:]?#]*)?$'; then
    die "${what} is not an http(s) URL without credentials, query or fragment: ${url}"
  fi
  [ "$(printf '%s\n' "${url}" | wc -l | tr -d '[:space:]')" = 1 ] || die "${what} is not a single line"
}

url_host() {
  printf '%s\n' "$1" | sed -E 's#^[a-z]+://(\[[^]]*\]|[^/:]*).*$#\1#'
}

# Plain http only to this machine (tests), or with AITP_SETUP_ALLOW_HTTP=1.
check_download_scheme() {
  case "$1" in
    https://*) return 0 ;;
  esac
  case "$(url_host "$1")" in
    127.0.0.1|localhost|"[::1]") return 0 ;;
  esac
  [ "${AITP_SETUP_ALLOW_HTTP:-}" = 1 ] && return 0
  die "refusing to download over plain http from $1"
}

# download URL FILE LIMIT
download() {
  local protocols='=https'
  case "$1" in
    http://*) protocols='=http,https' ;;
  esac
  curl -fsSL --proto "${protocols}" --proto-redir "${protocols}" --retry 2 \
    --connect-timeout 20 --max-filesize "$3" -o "$2" "$1" </dev/null ||
    die "download failed: $1"
  [ -f "$2" ] || die "download failed: $1"
  [ "$(file_size "$2")" -le "$3" ] || die "$1 is larger than $3 bytes"
}

# Compares two X.Y.Z versions; prints -1, 0 or 1.
compare_versions() {
  local IFS=.
  # shellcheck disable=SC2206
  local left=($1) right=($2) index
  for index in 0 1 2; do
    if [ "${left[index]}" -lt "${right[index]}" ]; then echo -1; return; fi
    if [ "${left[index]}" -gt "${right[index]}" ]; then echo 1; return; fi
  done
  echo 0
}

# The version an aitp program reports ("aitp X.Y.Z"), or nothing.
program_version() {
  local output
  output="$("$1" --version 2>/dev/null </dev/null)" || return 0
  if printf '%s\n' "${output}" | grep -Eqx "aitp ${VERSION_PATTERN}"; then
    printf '%s\n' "${output#aitp }"
  fi
}

platform_asset() {
  local system architecture
  case "$(uname -s)" in
    Linux) system=linux ;;
    Darwin) system=macos ;;
    *) die "unsupported operating system $(uname -s); on Windows use setup.ps1" ;;
  esac
  case "$(uname -m)" in
    x86_64|amd64) architecture=x86_64 ;;
    arm64|aarch64) architecture=aarch64 ;;
    *) die "unsupported architecture $(uname -m)" ;;
  esac
  # An x86_64 shell under Rosetta on Apple silicon: install the native program.
  if [ "${system}" = macos ] && [ "${architecture}" = x86_64 ] &&
    [ "$(sysctl -n hw.optional.arm64 2>/dev/null || true)" = 1 ]; then
    architecture=aarch64
  fi
  printf 'aitp-%s-%s\n' "${system}" "${architecture}"
}

# Checks the manifest at $1 (the canonical form the release writes: one line
# of sorted, compact JSON) and sets release_version, expected_sha256 and
# expected_size for asset $2.
read_manifest() {
  local manifest="$1" asset="$2" entry entries count
  [ "$(file_size "${manifest}")" -gt 0 ] || die "the manifest is empty"
  [ "$(tail -c 1 "${manifest}" | od -An -tx1 | tr -d '[:space:]')" = 0a ] ||
    die "the manifest does not end with a line break"
  [ "$(wc -l <"${manifest}" | tr -d '[:space:]')" = 1 ] || die "the manifest is not a single line"
  entries="\"${ASSET_NAME_PATTERN}\":${ENTRY_PATTERN}"
  grep -Eqx "\\{\"assets\":\\{${entries}(,${entries})*\\},\"distribution\":\"aitp\",\"manifest_schema\":1,\"version\":\"${VERSION_PATTERN}\"\\}" "${manifest}" ||
    die "the manifest is not an AITP release manifest (schema 1, distribution aitp)"
  release_version="$(sed -E 's/^.*,"version":"([^"]*)"\}$/\1/' "${manifest}")"
  entry="$(grep -Eo "[{,]\"${asset}\":${ENTRY_PATTERN}" "${manifest}" || true)"
  count="$(printf '%s' "${entry}" | grep -c . || true)"
  [ "${count}" = 1 ] || die "the manifest lists ${asset} ${count} times (expected once); this platform may not be released"
  expected_sha256="$(printf '%s\n' "${entry}" | sed -E 's/^.*"sha256":"([0-9a-f]{64})".*$/\1/')"
  expected_size="$(printf '%s\n' "${entry}" | sed -E 's/^.*"size":([0-9]+)\}$/\1/')"
  if [ "${expected_size}" -lt "${PROGRAM_MIN}" ] || [ "${expected_size}" -gt "${PROGRAM_MAX}" ]; then
    die "the manifest gives ${asset} a size of ${expected_size} bytes, not between ${PROGRAM_MIN} and ${PROGRAM_MAX}"
  fi
}

# Removed on exit: the work directory and a staged file not yet renamed.
cleanup_work=""
staged=""
cleanup() {
  [ -z "${cleanup_work}" ] || rm -rf -- "${cleanup_work}"
  [ -z "${staged}" ] || rm -f -- "${staged}"
}

# --- main -------------------------------------------------------------------

main() {
  local base_url="" api_key="" have_key=0
  local download_base="${AITP_SETUP_DOWNLOAD_BASE:-}" install_dir="${AITP_INSTALL_DIR:-}"
  local releases="${AITP_SETUP_RELEASES_URL:-${GITHUB_RELEASES}}"
  local no_start=0 codex=0 install_only=0

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --base-url|--api-key|--download-base|--install-dir)
        [ "$#" -ge 2 ] || die "missing value for $1"
        case "$1" in
          --base-url) base_url="$2" ;;
          --api-key) api_key="$2"; have_key=1 ;;
          --download-base) download_base="$2" ;;
          --install-dir) install_dir="$2" ;;
        esac
        shift 2
        ;;
      --base-url=*) base_url="${1#*=}"; shift ;;
      --api-key=*) api_key="${1#*=}"; have_key=1; shift ;;
      --download-base=*) download_base="${1#*=}"; shift ;;
      --install-dir=*) install_dir="${1#*=}"; shift ;;
      --no-start) no_start=1; shift ;;
      --codex) codex=1; shift ;;
      --install-only) install_only=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) usage >&2; die "unknown argument: $1" ;;
    esac
  done

  if [ "${install_only}" = 0 ]; then
    [ -n "${base_url}" ] || { usage >&2; die "--base-url is required"; }
  fi
  [ -z "${base_url}" ] || check_url "--base-url" "${base_url}"

  # The manifest of the latest release; the program comes from the release
  # the manifest names (or, for tests, both from one flat directory).
  local manifest_url asset_base=""
  if [ -n "${download_base}" ]; then
    download_base="$(strip_slashes "${download_base}")"
    check_url "the download base" "${download_base}"
    check_download_scheme "${download_base}"
    manifest_url="${download_base}/${MANIFEST_NAME}"
    asset_base="${download_base}"
  else
    releases="$(strip_slashes "${releases}")"
    check_url "the releases URL" "${releases}"
    check_download_scheme "${releases}"
    manifest_url="${releases}/latest/download/${MANIFEST_NAME}"
  fi

  if [ "${install_only}" = 0 ] && [ "${have_key}" = 0 ]; then
    if { : </dev/tty; } 2>/dev/null; then
      printf 'API key for %s: ' "${base_url}" >/dev/tty
      IFS= read -r -s api_key </dev/tty || true
      printf '\n' >/dev/tty
    else
      die "--api-key is required (no terminal to ask for it)"
    fi
  fi
  if [ "${install_only}" = 0 ]; then
    case "${api_key}" in
      ''|*[![:space:]]*) ;;
      *) api_key="" ;;
    esac
    [ -n "${api_key}" ] || die "the API key is empty"
    case "${api_key}" in
      *$'\n'*|*$'\r'*) die "the API key must be a single line" ;;
    esac
  fi

  [ -n "${install_dir}" ] || install_dir="${HOME:?HOME is not set}/.local/bin"
  case "${install_dir}" in
    /*) ;;
    *) install_dir="$(pwd)/${install_dir}" ;;
  esac

  command -v curl >/dev/null 2>&1 || die "curl is required"
  local asset install_path backup_path work candidate
  asset="$(platform_asset)"
  install_path="${install_dir}/aitp"
  backup_path="${install_dir}/aitp-previous"

  work="$(mktemp -d "${TMPDIR:-/tmp}/aitp-setup.XXXXXX")"
  cleanup_work="${work}"
  trap cleanup EXIT
  candidate="${work}/${asset}"

  say "release manifest: ${manifest_url}"
  download "${manifest_url}" "${work}/${MANIFEST_NAME}" "${MANIFEST_LIMIT}"
  release_version="" expected_sha256="" expected_size=""
  read_manifest "${work}/${MANIFEST_NAME}" "${asset}"
  [ -n "${asset_base}" ] || asset_base="${releases}/download/v${release_version}"
  say "latest release: aitp ${release_version} (${asset})"

  mkdir -p -- "${install_dir}"
  [ ! -L "${install_path}" ] || die "refusing to install over a symbolic link: ${install_path}"
  if [ -e "${install_path}" ] && [ ! -f "${install_path}" ]; then
    die "not a regular file: ${install_path}"
  fi

  local updated=0 installed_version=""
  if [ -f "${install_path}" ] && [ "$(sha256_file "${install_path}")" = "${expected_sha256}" ]; then
    say "aitp ${release_version} is already installed at ${install_path}; download skipped"
  else
    if [ -f "${install_path}" ] && [ -x "${install_path}" ]; then
      installed_version="$(program_version "${install_path}")"
    fi
    if [ -n "${installed_version}" ] && [ "$(compare_versions "${installed_version}" "${release_version}")" = 1 ]; then
      say "aitp ${installed_version} at ${install_path} is newer than ${release_version}; keeping it"
    else
      say "downloading ${asset_base}/${asset} (${expected_size} bytes)"
      download "${asset_base}/${asset}" "${candidate}" "${expected_size}"
      [ "$(file_size "${candidate}")" = "${expected_size}" ] ||
        die "${asset} has $(file_size "${candidate}") bytes, the manifest says ${expected_size}; nothing installed"
      [ "$(sha256_file "${candidate}")" = "${expected_sha256}" ] ||
        die "${asset} does not match the manifest's SHA-256; nothing installed"
      chmod 0755 "${candidate}"
      [ "$(program_version "${candidate}")" = "${release_version}" ] ||
        die "the downloaded program does not report version ${release_version}; nothing installed"

      if [ -f "${install_path}" ]; then
        staged="$(mktemp "${install_dir}/.aitp.install.XXXXXX")"
        cp -- "${install_path}" "${staged}"
        chmod 0755 "${staged}"
        [ ! -L "${backup_path}" ] || die "refusing to replace a symbolic link: ${backup_path}"
        mv -f -- "${staged}" "${backup_path}"
        staged=""
      fi
      staged="$(mktemp "${install_dir}/.aitp.install.XXXXXX")"
      cp -- "${candidate}" "${staged}"
      chmod 0755 "${staged}"
      mv -f -- "${staged}" "${install_path}"
      staged=""
      updated=1
      if [ -n "${installed_version}" ]; then
        say "updated ${install_path} from aitp ${installed_version} to ${release_version} (previous program: ${backup_path})"
      else
        say "installed aitp ${release_version} at ${install_path}"
      fi
    fi
  fi

  local on_path=0
  case ":${PATH:-}:" in
    *":${install_dir}:"*|*":${install_dir}/:"*) on_path=1 ;;
  esac

  if [ "${install_only}" = 1 ]; then
    say "done (--install-only: nothing configured)"
  else
    local skip_start="${AITP_SKIP_START:-}"
    [ "${no_start}" = 0 ] || skip_start=1
    say "configuring Claude Code (aitp init)"
    AITP_BINARY_UPDATED="${updated}" AITP_SKIP_START="${skip_start}" \
      "${install_path}" init --base-url "${base_url}" --api-key "${api_key}" </dev/null ||
      die "aitp init failed"
    if [ "${codex}" = 1 ]; then
      say "configuring Codex (aitp init-codex)"
      AITP_BINARY_UPDATED=0 AITP_SKIP_START="${skip_start}" \
        "${install_path}" init-codex --base-url "${base_url}" --api-key "${api_key}" </dev/null ||
        die "aitp init-codex failed"
    fi
  fi
  api_key=""

  local aitp_command=aitp
  [ "${on_path}" = 1 ] || aitp_command="${install_path}"
  printf '\n' >&2
  if [ "${on_path}" = 0 ]; then
    say "${install_dir} is not on your PATH; to run aitp by name, add to your shell profile:"
    say "  export PATH=\"${install_dir}:\$PATH\""
  fi
  if [ "${install_only}" = 0 ]; then
    if [ "${no_start}" = 1 ]; then
      say "next: start the local proxy with: ${aitp_command} start"
    fi
    say "next: run Claude Code as usual (claude); it now goes through the AITP local proxy."
    say "      (${aitp_command} claude also works, and starts the proxy if it is not running.)"
    if [ "${codex}" = 1 ]; then
      say "      For Codex run: ${aitp_command} codex"
    fi
    say "check: ${aitp_command} doctor    remove: ${aitp_command} uninstall"
  fi
}

main "$@"
