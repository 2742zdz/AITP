#!/usr/bin/env python3
"""Checks a built `aitp` release program before it is published.

Usage: check_release.py --build-dir DIR [--build-dir DIR ...] PROGRAM [PROGRAM ...]

Each program must

* carry the AITP brand data (the distinctive strings of src/bin/aitp/brand.rs),
* trust the maintainer's real update signing keys and carry no placeholder
  key (see release_manifest.py),
* name no other distribution built on this kernel: the neutral test brand
  `ktest`, plus each name in AITP_TEST_FOREIGN_NAMES (comma-separated, the
  same variable the tests read), and
* carry no path of the machine that built it: the given build directories
  (each at least two levels deep), CARGO_TARGET_DIR, HOME, CARGO_HOME and
  RUSTUP_HOME, each also in its resolved form (build-release.sh maps them
  to the neutral /aitp/... prefixes).

Only the test brand is refused by default; a distribution built on this
kernel passes its own names in AITP_TEST_FOREIGN_NAMES in its own CI.

Exits 0 when every program passes, 1 otherwise (the reason on stderr).
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
BRAND_SOURCE = SCRIPTS.parent / "src" / "bin" / "aitp" / "brand.rs"
FOREIGN_NAMES_VARIABLE = "AITP_TEST_FOREIGN_NAMES"
TEST_BRAND = "ktest"
# The neutral prefixes build-release.sh maps the build machine's paths to.
NEUTRAL_PREFIXES = ("/aitp/home", "/aitp/rustup", "/aitp/cargo", "/aitp/src", "/aitp/target")
# What may follow a whole neutral prefix: a separator, the NUL that ends a C
# string, or the end of the data. Anything else makes the occurrence an
# ordinary path, so a remapped directory that a following string happens to
# abut is reported rather than excused (a conservative false positive).
NEUTRAL_PREFIX_END = rb"(?=/|\x00|\Z)"


class CheckError(Exception):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckError(message)


def load_release_manifest():
    spec = importlib.util.spec_from_file_location("release_manifest", SCRIPTS / "release_manifest.py")
    require(spec is not None and spec.loader is not None, "cannot load release_manifest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def brand_markers() -> list[bytes]:
    """Distinctive AITP brand strings that only the AITP program carries;
    short or generic values are left out."""
    source = BRAND_SOURCE.read_text(encoding="utf-8")
    values = re.findall(r'b?"((?:[^"\\]|\\.)*)"', source.split("#[cfg(test)]")[0])
    markers = sorted(
        {value for value in values if len(value) >= 10 and re.search(r"aitp|2742zdz", value, re.IGNORECASE)}
    )
    # Values are compared as raw bytes: escapes would not match the binary.
    require(not any("\\" in marker for marker in markers), f"brand markers with escape sequences: {BRAND_SOURCE}")
    require(len(markers) >= 10, f"too few brand markers found in {BRAND_SOURCE}")
    require(
        "AITP local installation CA" in markers and "io.github.2742zdz.aitp.local-proxy" in markers,
        "the brand markers miss the CA and service identities",
    )
    return [marker.encode() for marker in markers]


def foreign_names(environ=os.environ) -> list[str]:
    """The test brand plus AITP_TEST_FOREIGN_NAMES, lower case. A set
    variable that names nothing, or a name shorter than three letters or
    digits, is a misconfiguration, never a weaker check."""
    names = [TEST_BRAND]
    value = environ.get(FOREIGN_NAMES_VARIABLE)
    for name in [] if value is None else value.split(","):
        name = name.strip()
        require(
            len(name) >= 3 and name.isascii() and name.isalnum(),
            f"{FOREIGN_NAMES_VARIABLE}: {name!r} is not a name of at least three letters or digits",
        )
        if name.lower() not in names:
            names.append(name.lower())
    return names


def path_view(data: bytes) -> bytes:
    """`data` lower-cased with both path separators as `/`, so a path matches
    in any case and with either separator."""
    return data.lower().replace(b"\\", b"/")


def build_machine_paths(build_dirs: list[Path], environ=os.environ) -> list[str]:
    for directory in build_dirs:
        require(directory.is_absolute(), f"build directory is not absolute: {directory}")
        # A short directory such as /src or /build also names paths that are
        # not the build machine's (the standard library's .../std/src/...),
        # and the root would match nothing once normalized: build in a
        # directory at least two levels deep.
        for form in (directory, Path(os.path.realpath(directory))):
            require(
                len(form.parts) - (1 if form.anchor else 0) >= 2,
                f"build directory is too short to check reliably: {directory} ({form})",
            )
    home = environ.get("HOME", "")
    candidates = [str(directory) for directory in build_dirs] + [
        environ.get("CARGO_TARGET_DIR", ""),
        home,
        environ.get("USERPROFILE", ""),
        environ.get("CARGO_HOME") or (home and str(Path(home) / ".cargo")),
        environ.get("RUSTUP_HOME") or (home and str(Path(home) / ".rustup")),
    ]
    paths = []
    for candidate in candidates:
        if not candidate:
            continue
        for form in (candidate, os.path.realpath(candidate)):
            # A relative directory (CARGO_TARGET_DIR=target) counts only in
            # its resolved, absolute form.
            if not os.path.isabs(form):
                continue
            form = form.rstrip("/\\")
            # The file system root (or a bare drive) would match every path.
            if re.fullmatch(r"(?:[A-Za-z]:)?", form) is None and form not in paths:
                paths.append(form)
    for path in paths:
        # A build path at or below /aitp, or a leading part of a neutral
        # prefix, cannot be told apart from the remapped paths.
        view = path_view(path.encode()).decode(errors="replace")
        require(
            not (view == "/aitp" or view.startswith("/aitp/"))
            and not any(prefix.startswith(view) for prefix in NEUTRAL_PREFIXES),
            f"build machine path {path} overlaps the neutral prefixes {NEUTRAL_PREFIXES}",
        )
    return paths


def names_path(view: bytes, path: str) -> bool:
    """Whether `view` holds `path` anywhere outside a whole neutral prefix
    (`/src` inside `/aitp/src/...` is the prefix itself, not a build path)."""
    neutral = {
        (match.start(), match.end())
        for prefix in NEUTRAL_PREFIXES
        for match in re.finditer(re.escape(prefix.encode()) + NEUTRAL_PREFIX_END, view)
    }
    needle = path_view(path.encode())
    start = view.find(needle)
    while start >= 0:
        end = start + len(needle)
        if not any(low <= start and end <= high for low, high in neutral):
            return True
        start = view.find(needle, start + 1)
    return False


def check(programs: list[Path], build_dirs: list[Path], environ=os.environ) -> None:
    require(bool(programs), "no program to check")
    require(bool(build_dirs), "at least one --build-dir is required")
    markers = brand_markers()
    names = foreign_names(environ)
    paths = build_machine_paths(build_dirs, environ)
    tool = load_release_manifest()
    try:
        keys = tool.brand_keys()
        tool.require_real_keys(keys)
    except tool.CheckError as error:
        raise CheckError(str(error)) from error
    for program in programs:
        require(program.is_file(), f"program is missing: {program}")
        data = program.read_bytes()
        missing = [marker.decode() for marker in markers if marker not in data]
        require(not missing, f"{program} lacks AITP brand data: {missing}")
        view = path_view(data)
        found = [name for name in names if name.encode() in view]
        require(not found, f"{program} names another distribution: {found}")
        found = [path for path in paths if names_path(view, path)]
        require(not found, f"{program} carries build machine paths: {found}")
        try:
            tool.require_program_keys(program, keys)
        except tool.CheckError as error:
            raise CheckError(str(error)) from error
    print(f"{len(programs)} program(s) carry the AITP brand data and real signing keys, and no build machine path")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--build-dir", action="append", default=[], type=Path, help="absolute build directory (repeatable)")
    parser.add_argument("program", nargs="+", type=Path)
    args = parser.parse_args(argv)
    try:
        check(args.program, args.build_dir)
    except CheckError as error:
        print(f"check_release.py: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
