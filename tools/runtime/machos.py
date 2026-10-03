"""Mach-O static archive inspection with Xcode's tools."""

import re
import subprocess
from functools import lru_cache

# LC_BUILD_VERSION platform numbers (mach-o/loader.h)
PLATFORMS = {1: "macos", 2: "ios", 7: "iossimulator"}
TARGET_PLATFORM = {"ios-arm64": 2, "ios-sim-arm64": 7}


def run(*args):
    return subprocess.run(["xcrun", *args], check=True, capture_output=True, text=True).stdout


def archs(path):
    return run("lipo", "-archs", str(path)).split()


def build_versions(path):
    """{member: (platform, minos)} from ``otool -l``; (None, None) if absent."""

    result = {}
    member = None
    platform = None
    in_cmd = False
    for line in run("otool", "-l", str(path)).splitlines():
        m = re.match(r"^.*\((.+)\):$", line)
        if m:
            member = m.group(1)
            result[member] = (None, None)
            continue
        line = line.strip()
        if line.startswith("cmd "):
            in_cmd = line == "cmd LC_BUILD_VERSION"
        elif in_cmd and line.startswith("platform "):
            platform = int(line.split()[1])
        elif in_cmd and line.startswith("minos "):
            result[member] = (platform, line.split()[1])
            in_cmd = False
    return result


def exported_symbols(path):
    """Sorted defined external symbols across all members."""

    out = run("nm", "-g", "-U", "-j", str(path))
    return sorted({l.strip() for l in out.splitlines() if l.strip() and not l.endswith(":")})


@lru_cache(maxsize=None)
def section_sizes(path):
    """Sum of section sizes by class over all members: text, data, bss."""

    sizes = {"text": 0, "data": 0, "bss": 0}
    for line in run("size", "-m", str(path)).splitlines():
        m = re.match(r"\s*Section \((__\w+), ?([\w.]+)\): (\d+)", line)
        if not m:
            continue
        segment, section, size = m.group(1), m.group(2), int(m.group(3))
        if segment == "__TEXT":
            sizes["text"] += size
        elif segment.startswith("__DATA"):
            if section in ("__bss", "__common"):
                sizes["bss"] += size
            else:
                sizes["data"] += size
    return sizes


def call_sequence(path, symbol):
    """Branch-relocation targets, in address order, inside one function.

    ``--disassemble-symbols`` bounds the listing to the symbol itself, so the
    result is not confused by local labels (ltmpN) sharing its address.
    """

    out = run("objdump", "-d", "-r", "--no-show-raw-insn", f"--disassemble-symbols={symbol}", str(path))
    return re.findall(r"ARM64_RELOC_BRANCH26\s+(\S+)", out)
