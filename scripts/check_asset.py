#!/usr/bin/env python3
"""Checks the executable format of AITP release assets, by asset name.

Usage: check_asset.py ASSET [ASSET ...]

* aitp-linux-x86_64, aitp-linux-aarch64: a static ELF64 (little-endian) for
  that machine: no program interpreter, no shared-library dependency
  (DT_NEEDED) and no GNU C library symbol version, so it runs on any Linux
  of that architecture.
* aitp-macos-x86_64, aitp-macos-aarch64: a 64-bit Mach-O executable for that
  CPU.
* aitp-windows-x86_64.exe, aitp-windows-aarch64.exe: a PE32+ image for that
  machine, linked with /Brepro (its debug directory has the reproducible
  build entry, so the image carries no time stamp), and naming no PDB by a
  path (a CodeView entry, if any, names the file only).

The release check (check_release.py) covers the contents; this covers the
format. Exits 0 when every asset passes, 1 otherwise.
"""

from __future__ import annotations

import re
import struct
import sys
from pathlib import Path


class CheckError(Exception):
    pass


def require(condition: object, message: str) -> None:
    if not condition:
        raise CheckError(message)


def window(data: bytes, offset: int, size: int, what: str) -> bytes:
    require(offset >= 0 and size >= 0 and offset + size <= len(data), f"{what} extends past the end of the file")
    return data[offset:offset + size]


# --- Linux ------------------------------------------------------------------

ELF_HEADER = struct.Struct("<16sHHIQQQIHHHHHH")
PROGRAM_HEADER = struct.Struct("<IIQQQQQQ")
DYNAMIC_ENTRY = struct.Struct("<qQ")
PT_LOAD, PT_DYNAMIC, PT_INTERP = 1, 2, 3
PF_X = 1
DT_NULL, DT_NEEDED = 0, 1
GLIBC_VERSION = re.compile(rb"(?:GLIBC|GLIBCXX|CXXABI)_[A-Za-z0-9_.-]+")


def check_elf(data: bytes, machine: int) -> str:
    require(len(data) >= ELF_HEADER.size and data[:4] == b"\x7fELF", "not an ELF file")
    (ident, elf_type, elf_machine, version, entry, ph_offset, _sh, _flags, header_size, ph_size, ph_count,
     _shs, _shn, _shstr) = ELF_HEADER.unpack_from(data)
    require(ident[4] == 2 and ident[5] == 1, "not a little-endian ELF64 file")
    require(ident[6] == 1 and version == 1 and header_size == ELF_HEADER.size, "unsupported ELF version or header")
    require(elf_type in (2, 3), "neither an executable nor a static PIE")
    require(elf_machine == machine, f"ELF machine {elf_machine}, expected {machine}")
    require(ph_count > 0 and ph_size >= PROGRAM_HEADER.size, "no usable program header table")
    window(data, ph_offset, ph_size * ph_count, "the program header table")
    headers = []
    for index in range(ph_count):
        kind, flags, offset, address, _physical, file_size, memory_size, _align = PROGRAM_HEADER.unpack_from(
            data, ph_offset + index * ph_size)
        require(kind != PT_INTERP, "the program asks for a dynamic loader (PT_INTERP)")
        headers.append((kind, flags, offset, address, file_size, memory_size))
    loads = [header for header in headers if header[0] == PT_LOAD]
    for _kind, _flags, offset, _address, file_size, memory_size in loads:
        window(data, offset, file_size, "a loadable segment")
        require(file_size <= memory_size, "a loadable segment is larger in the file than in memory")
    require(any(flags & PF_X and address <= entry < address + file_size
                for _, flags, _, address, file_size, _ in loads), "the entry point is in no executable segment")
    for kind, _flags, offset, _address, file_size, _memory_size in headers:
        if kind != PT_DYNAMIC:
            continue
        dynamic = window(data, offset, file_size, "the dynamic segment")
        require(len(dynamic) % DYNAMIC_ENTRY.size == 0, "the dynamic segment has a partial entry")
        for position in range(0, len(dynamic), DYNAMIC_ENTRY.size):
            tag, _value = DYNAMIC_ENTRY.unpack_from(dynamic, position)
            require(tag != DT_NEEDED, "the program needs a shared library (DT_NEEDED)")
            if tag == DT_NULL:
                break
    found = sorted({match.decode() for match in GLIBC_VERSION.findall(data)})
    require(not found, f"the program names GNU C library symbol versions: {found}")
    return "static ELF"


# --- macOS ------------------------------------------------------------------

MACH_HEADER = struct.Struct("<IiiIIIII")
MH_MAGIC_64, MH_EXECUTE = 0xFEEDFACF, 2


def check_macho(data: bytes, cpu: int) -> str:
    require(len(data) >= MACH_HEADER.size, "not a Mach-O file")
    magic, cpu_type, _subtype, file_type, count, size, _flags, _reserved = MACH_HEADER.unpack_from(data)
    require(magic == MH_MAGIC_64, "not a 64-bit little-endian Mach-O file (or a universal one)")
    require(cpu_type == cpu, f"Mach-O CPU type {cpu_type:#x}, expected {cpu:#x}")
    require(file_type == MH_EXECUTE, "not a Mach-O executable")
    commands = window(data, MACH_HEADER.size, size, "the load commands")
    position = 0
    for _ in range(count):
        require(position + 8 <= len(commands), "the load commands are truncated")
        _command, command_size = struct.unpack_from("<II", commands, position)
        require(command_size >= 8 and position + command_size <= len(commands), "a load command is truncated")
        position += command_size
    require(count > 0, "no load commands")
    return "Mach-O executable"


# --- Windows ----------------------------------------------------------------

IMAGE_DEBUG_TYPE_CODEVIEW, IMAGE_DEBUG_TYPE_REPRO = 2, 16


def check_pe(data: bytes, machine: int) -> str:
    require(data[:2] == b"MZ" and len(data) >= 0x40, "not a PE image")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    require(window(data, pe, 24, "the PE header")[:4] == b"PE\0\0", "not a PE image")
    pe_machine, sections = struct.unpack_from("<HH", data, pe + 4)
    optional_size = struct.unpack_from("<H", data, pe + 20)[0]
    require(pe_machine == machine, f"PE machine {pe_machine:#x}, expected {machine:#x}")
    optional = pe + 24
    require(optional_size >= 168 and struct.unpack_from("<H", window(data, optional, 2, "the optional header"))[0]
            == 0x20B, "not a PE32+ image with a debug data directory")
    debug_rva, debug_size = struct.unpack_from("<II", window(data, optional + 160, 8, "the data directories"))
    require(debug_rva and debug_size and debug_size % 28 == 0,
            "no debug directory (the reproducible build entry is missing: link with /Brepro)")

    def file_offset(rva: int, size: int) -> int:
        for index in range(sections):
            section = window(data, optional + optional_size + index * 40, 40, "the section table")
            virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from("<IIII", section, 8)
            if virtual_address <= rva and rva + size <= virtual_address + max(virtual_size, raw_size):
                require(rva + size <= virtual_address + raw_size, "debug data is not backed by the file")
                return raw_offset + rva - virtual_address
        raise CheckError("debug data is in no section")

    directory = window(data, file_offset(debug_rva, debug_size), debug_size, "the debug directory")
    kinds = []
    for position in range(0, debug_size, 28):
        _chars, _stamp, _major, _minor, kind, size, _address, pointer = struct.unpack_from("<IIHHIIII", directory,
                                                                                            position)
        kinds.append(kind)
        if kind == IMAGE_DEBUG_TYPE_CODEVIEW:
            record = window(data, pointer, size, "the CodeView record")
            require(record[:4] == b"RSDS" and len(record) > 24, "unknown CodeView record")
            name = record[24:].split(b"\0", 1)[0]
            require(b"/" not in name and b"\\" not in name, f"the image names its PDB by a path: {name!r}")
    require(IMAGE_DEBUG_TYPE_REPRO in kinds, "no reproducible build entry: link with /Brepro")
    return "PE32+ image linked with /Brepro"


ASSETS = {
    "aitp-linux-x86_64": (check_elf, 62),
    "aitp-linux-aarch64": (check_elf, 183),
    "aitp-macos-x86_64": (check_macho, 0x01000007),
    "aitp-macos-aarch64": (check_macho, 0x0100000C),
    "aitp-windows-x86_64.exe": (check_pe, 0x8664),
    "aitp-windows-aarch64.exe": (check_pe, 0xAA64),
}


def check(path: Path) -> str:
    require(path.name in ASSETS, f"not a release asset name: {path.name}")
    require(path.is_file() and not path.is_symlink(), f"missing or not a regular file: {path}")
    function, expected = ASSETS[path.name]
    try:
        return function(path.read_bytes(), expected)
    except struct.error as error:
        raise CheckError(f"truncated headers: {error}") from error


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    status = 0
    for argument in argv:
        path = Path(argument)
        try:
            print(f"{path.name}: {check(path)}")
        except (CheckError, OSError) as error:
            print(f"check_asset.py: {path}: {error}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main(sys.argv[1:]))
