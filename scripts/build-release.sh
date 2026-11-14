#!/usr/bin/env bash
# Builds the release `aitp` program with the build machine's directories
# remapped to neutral prefixes, so the program carries no local path:
#
#   HOME         -> /aitp/home
#   RUSTUP_HOME  -> /aitp/rustup
#   CARGO_HOME   -> /aitp/cargo  (crate sources in panic locations)
#   workspace    -> /aitp/src    (the Cargo workspace holding this kernel)
#   target dir   -> /aitp/target (build script outputs, generated sources)
#
# Rust code is remapped with `--remap-path-prefix`, C code built by
# dependencies (ring, aws-lc) with `-ffile-prefix-map` (GCC 8+, Clang 10+).
# For x86_64 Windows the C code is built by MSVC, which has no prefix map:
# its compiler gets `/d1trimfile:<directory>\` instead, which cuts the
# directory off __FILE__, leaving a relative path (AWS-LC's assertions carry
# __FILE__, also widened; check_release.py checks both forms in the result).
# For arm64 Windows ring builds its C code with Clang (-ffile-prefix-map).
#
# Release targets (one per run):
#
#   x86_64-unknown-linux-musl, aarch64-unknown-linux-musl
#       static, built natively on that architecture with musl-gcc (CC or
#       CC_<target> overrides it). The Linux kernel headers are exposed to
#       the C code (`-isystem`, never the host C library's headers): without
#       linux/random.h AWS-LC takes musl's getentropy(), which has no
#       fallback when the kernel lacks getrandom (before 3.17); with it,
#       AWS-LC falls back to /dev/urandom (scripts/test_linux_entropy.py).
#   x86_64-apple-darwin, aarch64-apple-darwin
#   x86_64-pc-windows-msvc, aarch64-pc-windows-msvc
#       linked with /Brepro (reproducible PE: no time stamp), which
#       .cargo/config.toml sets as rustflags; those are replaced by
#       CARGO_ENCODED_RUSTFLAGS here, so this script passes it itself.
#
# Linux GNU targets build too (the CI's check of the remapping on the host),
# but are not release assets.
#
# Usage: build-release.sh [cargo build arguments, e.g. --locked --target T]
# It refuses to build while the brand's update signing keys are the
# placeholders or unusable (see below).
# The target directory is CARGO_TARGET_DIR or <workspace>/target; set
# CARGO_TARGET_DIR instead of passing --target-dir. The build always names
# its one target explicitly (so a `build.target` in a configuration file
# cannot change it), and the program is written to
# <target dir>/<triple>/release/aitp (aitp.exe for Windows); that path is
# the script's only output on stdout (on Windows in the mixed form C:/...).
#
# The flags are passed through CARGO_ENCODED_RUSTFLAGS, which takes the place
# of rustflags from Cargo configuration files; RUSTFLAGS or
# CARGO_ENCODED_RUSTFLAGS already in the environment are kept. C flags are
# added to CFLAGS and CXXFLAGS and to every target- or host-specific C flag
# variable that is set (the cc crate uses the most specific one); a variable
# whose name holds a dash cannot be extended from the shell, so the script
# refuses to run while one is set. (AWS-LC maps its own sources already;
# the C flags cover the remaining C code and generated headers.)
set -euo pipefail

fail() {
  echo "build-release.sh: $*" >&2
  exit 1
}

# The target comes from --target, else CARGO_BUILD_TARGET, else the host;
# it is then passed to Cargo explicitly.
targets=()
cargo_arguments=()
previous=""
for argument in "$@"; do
  if [[ "$previous" == "--target" ]]; then
    targets+=("$argument")
  else
    case "$argument" in
      --target-dir | --target-dir=*) fail "set CARGO_TARGET_DIR instead of $argument" ;;
      --target=*) targets+=("${argument#--target=}") ;;
      --target) ;;
      *) cargo_arguments+=("$argument") ;;
    esac
  fi
  previous="$argument"
done
[[ "$previous" == "--target" ]] && fail "--target needs a value"
if [[ ${#targets[@]} -eq 0 ]]; then
  if [[ -n "${CARGO_BUILD_TARGET:-}" ]]; then
    targets=("$CARGO_BUILD_TARGET")
  else
    host="$(rustc -vV | sed -n 's/^host: //p')"
    [[ -n "$host" ]] || fail "cannot read the host target from rustc -vV"
    targets=("$host")
  fi
fi
[[ ${#targets[@]} -eq 1 ]] || fail "build one target at a time, not ${targets[*]}"
triple="${targets[0]}"
# Windows runs this script under Git Bash: paths go to the tools in the
# mixed form (C:/Users/...), which rustc and Python read as Windows paths.
windows_host=false
case "$(uname -s)" in
  MINGW* | MSYS* | CYGWIN*) windows_host=true ;;
esac
native_path() {
  if $windows_host; then cygpath -m "$1"; else printf '%s\n' "$1"; fi
}

kind=""
case "$triple" in
  *-unknown-linux-gnu | *-unknown-linux-gnu?*) kind=linux-gnu ;;
  x86_64-unknown-linux-musl | aarch64-unknown-linux-musl) kind=linux-musl ;;
  x86_64-apple-darwin | aarch64-apple-darwin) kind=macos ;;
  x86_64-pc-windows-msvc | aarch64-pc-windows-msvc) kind=windows ;;
  *) fail "not a verified target for the remapped release build: $triple" ;;
esac
case "$kind" in
  linux-musl)
    [[ "$(uname -s):$(uname -m)" == "Linux:${triple%%-*}" ]] ||
      fail "$triple builds natively only (on Linux ${triple%%-*}, not $(uname -s) $(uname -m))" ;;
  windows) $windows_host || fail "$triple builds on Windows only" ;;
  macos) [[ "$(uname -s)" == Darwin ]] || fail "$triple builds on macOS only" ;;
esac
# The MSVC flags below hold backslashes, which the cc crate's shell-escaped
# mode would read as escapes.
if [[ "$kind" == windows && -n "${CC_SHELL_ESCAPED_FLAGS:-}" ]]; then
  fail "CC_SHELL_ESCAPED_FLAGS is not supported for Windows release builds"
fi
if env | grep -E '^(C|CXX)FLAGS_[^=]*-' >/dev/null; then
  fail "C flag variables named with a dash cannot be extended; use the underscore form"
fi

kernel="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
kernel="$(native_path "$kernel")"
manifest="$kernel/Cargo.toml"
# (Windows: python3 may be the store stub; the runner's Python is python.)
python="${PYTHON:-python3}"
$windows_host && python="${PYTHON:-python}"

# A published program must trust the maintainer's real update signing keys,
# never the placeholder keys (release_manifest.py PLACEHOLDER_KEYS) or an
# unusable key.
status=0
"$python" -B "$kernel/scripts/release_manifest.py" keys --require-real >&2 || status=$?
case "$status" in
  0) ;;
  3) fail "the update signing keys are placeholders or unusable" ;;
  *) fail "cannot read the update signing keys (release_manifest.py exited $status)" ;;
esac
workspace_manifest="$(cargo locate-project --workspace --manifest-path "$manifest" --message-format plain)"
if $windows_host; then
  workspace_manifest="$(cygpath -m "$workspace_manifest")"
fi
workspace="$(dirname "$workspace_manifest")"

target_dir="${CARGO_TARGET_DIR:-$workspace/target}"
mkdir -p "$target_dir"
target_dir="$(cd "$target_dir" && pwd)"
target_dir="$(native_path "$target_dir")"
export CARGO_TARGET_DIR="$target_dir"

home="${HOME:-}"
cargo_home="${CARGO_HOME:-${home:+$home/.cargo}}"
rustup_home="${RUSTUP_HOME:-${home:+$home/.rustup}}"
if $windows_host; then
  # On Windows the tools see the user profile, not Git Bash's HOME.
  home="${USERPROFILE:-$home}"
  for name in home cargo_home rustup_home; do
    if [[ -n "${!name}" ]]; then
      converted="$(cygpath -m "${!name}")"
      printf -v "$name" '%s' "$converted"
    fi
  done
fi

# "from<TAB>to" lines; each directory also by its physical path when that
# differs (a symbolic link in the path).
mappings=""
add_mapping() {
  local from="$1" to="$2" physical
  [[ -n "$from" && "$from" != "/" ]] || return 0
  if $windows_host; then
    [[ "$from" =~ ^[A-Za-z]:/. ]] || fail "not an absolute path: $from"
  else
    [[ "$from" == /* ]] || fail "not an absolute path: $from"
  fi
  [[ "$from" =~ [[:space:]=] ]] && fail "paths with whitespace or '=' cannot be remapped: $from"
  from="${from%/}"
  mappings+="$from"$'\t'"$to"$'\n'
  # (On Windows rustc compares paths by component, so C:/a matches C:\a.)
  if [[ -d "$from" ]] && ! $windows_host; then
    physical="$(cd "$from" && pwd -P)"
    if [[ "$physical" != "$from" ]]; then
      [[ "$physical" =~ [[:space:]=] ]] && fail "paths with whitespace or '=' cannot be remapped: $physical"
      mappings+="$physical"$'\t'"$to"$'\n'
    fi
  fi
}
add_mapping "$home" /aitp/home
add_mapping "$rustup_home" /aitp/rustup
add_mapping "$cargo_home" /aitp/cargo
add_mapping "$workspace" /aitp/src
add_mapping "$target_dir" /aitp/target

# The compilers apply the last matching mapping: the longest (most specific)
# prefix goes last, so a directory inside another maps to its own prefix.
# (Generated first, so a failure stops the script under set -e/pipefail.)
ordered="$(printf '%s' "$mappings" | awk -F '\t' 'NF == 2 { print length($1) "\t" $0 }' | sort -n -k1,1)"
rust_flags=()
c_flags=""
while IFS=$'\t' read -r _ from to; do
  [[ -n "$from" ]] || continue
  rust_flags+=("--remap-path-prefix=$from=$to")
  if [[ "$triple" == x86_64-pc-windows-msvc ]]; then
    # MSVC (cl); the compiler sees C:\..., in its own spelling.
    windows_from="$(cygpath -w "$from")"
    c_flags+=" /d1trimfile:$windows_from\\"
  elif [[ "$triple" == aarch64-pc-windows-msvc ]]; then
    # Clang (ring builds its C code with it for this target), C:\... spelling.
    windows_from="$(cygpath -w "$from")"
    c_flags+=" -ffile-prefix-map=$windows_from=$to"
  else
    c_flags+=" -ffile-prefix-map=$from=$to"
  fi
done <<<"$ordered"
[[ ${#rust_flags[@]} -gt 0 ]] || fail "no directory to remap"
# Configuration-file rustflags no longer apply (see above): the Windows
# release keeps its reproducible PE image.
if [[ "$kind" == windows ]]; then
  rust_flags+=("-Clink-arg=/Brepro")
fi

# musl: the kernel headers, by themselves, in a directory inside the target
# directory (remapped like it; a fixed name per target, so the flags and any
# path the C code records do not vary between builds; removed afterwards).
# Exported first, so the C flags below are added to this variable too.
if [[ "$kind" == linux-musl ]]; then
  suffix="${triple//-/_}"
  cc_variable="CC_$suffix"
  cflags_variable="CFLAGS_$suffix"
  export "$cc_variable=${!cc_variable:-${CC:-musl-gcc}}"
  uapi_dir="$target_dir/aitp-linux-uapi-$triple"
  rm -rf "$uapi_dir"
  trap 'rm -rf "$uapi_dir"' EXIT
  mkdir -p "$uapi_dir"
  multiarch="$(gcc -print-multiarch)"
  for name in linux asm-generic; do
    [[ -d "/usr/include/$name" ]] || fail "kernel headers missing: /usr/include/$name (install linux-libc-dev)"
    ln -s "/usr/include/$name" "$uapi_dir/$name"
  done
  [[ -d "/usr/include/$multiarch/asm" ]] || fail "kernel headers missing: /usr/include/$multiarch/asm"
  ln -s "/usr/include/$multiarch/asm" "$uapi_dir/asm"
  export "$cflags_variable=${!cflags_variable:-${CFLAGS:-}} -isystem $uapi_dir"
  # Fail before building when the headers do not work with this compiler.
  printf '#include <linux/random.h>\n#include <stdlib.h>\nint main(void) { return RNDGETENTCNT == 0; }\n' |
    "${!cc_variable}" -isystem "$uapi_dir" -Werror -x c -c -o "$uapi_dir/probe.o" - ||
    fail "the kernel headers do not compile with ${!cc_variable}"
fi

separator=$'\x1f'
encoded="${CARGO_ENCODED_RUSTFLAGS:-}"
if [[ -z "$encoded" && -n "${RUSTFLAGS:-}" ]]; then
  read -r -d "" -a existing <<<"$RUSTFLAGS" || true
  encoded="$(IFS="$separator"; printf '%s' "${existing[*]}")"
fi
for flag in "${rust_flags[@]}"; do
  encoded="${encoded:+$encoded$separator}$flag"
done
export CARGO_ENCODED_RUSTFLAGS="$encoded"
unset RUSTFLAGS
export CFLAGS="${CFLAGS:-}$c_flags"
export CXXFLAGS="${CXXFLAGS:-}$c_flags"
while IFS= read -r name; do
  export "$name=${!name}$c_flags"
done < <(compgen -e | grep -E '^((C|CXX)FLAGS_[A-Za-z0-9_]+|(TARGET|HOST)_(C|CXX)FLAGS)$' || true)

# Cargo reports on stderr; stdout carries only the program's path.
cargo build --manifest-path "$manifest" --release --bin aitp --target "$triple" \
  ${cargo_arguments[@]+"${cargo_arguments[@]}"} >&2
program="$target_dir/$triple/release/aitp"
[[ "$kind" == windows ]] && program+=".exe"
[[ -f "$program" ]] || fail "cargo did not write $program"
printf '%s\n' "$program"
