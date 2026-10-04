"""Mach-O static archive inspection with Xcode's tools."""

import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path

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


def weak_only_symbols(path):
    """Defined external symbols that every member defines weak.

    These are the link-once copies of inline functions and implicit template
    instantiations: each object that uses one emits its own, and an object
    whose optimiser inlined every use emits none.
    """

    weak, strong = set(), set()
    for line in run("nm", "-m", "-g", "-U", str(path)).splitlines():
        m = re.match(r"^[0-9a-f]+ \([^)]*\) (.*) (\S+)$", line)
        if m:
            (weak if "weak" in m.group(1).split() else strong).add(m.group(2))
    return weak - strong


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


@lru_cache(maxsize=None)
def llvm_nm():
    """Homebrew's llvm-nm: (path, version line)."""

    path = os.environ.get("RPL_LLVM_NM") or str(
        Path(subprocess.check_output(["brew", "--prefix", "llvm"], text=True).strip()) / "bin" / "llvm-nm")
    version = subprocess.check_output([path, "--version"], text=True)
    return path, next(l.strip() for l in version.splitlines() if "version" in l)


def symbol_table(path, arch, fallbacks=None):
    """``nm -m`` output for one archive.

    Upstream's 8.6 nightlies contain LLVM bitcode (assimp is built with LTO
    by a newer LLVM than Xcode's), which Xcode's nm cannot read. Those
    archives are read with llvm-nm, whose -m output has the same form
    (bitcode definitions show as "(LTO,CODE)" and the like), and are listed
    in ``fallbacks``.
    """

    try:
        return run("nm", "-m", "-arch", arch, str(path))
    except subprocess.CalledProcessError as e:
        if "Unknown attribute kind" not in e.stderr and "Producer:" not in e.stderr:
            raise
    nm, version = llvm_nm()
    out = subprocess.run([nm, "-m", f"--arch={arch}", str(path)], check=True, capture_output=True, text=True).stdout
    if fallbacks is not None:
        fallbacks.append({"archive": Path(path).name, "reader": version})
    return out


def external_references(paths, arch="arm64", fallbacks=None):
    """Undefined external symbols of ``paths`` that none of them defines.

    These are what system libraries must provide. Weak references are kept:
    a weak reference to an API newer than the deployment target is NULL on
    older iOS, which is only safe behind a run-time availability check.
    Linker-synthesized objc_msgSend$ selector stubs are excluded.
    """

    defined, referenced = set(), set()
    for path in paths:
        for line in symbol_table(path, arch, fallbacks).splitlines():
            if " non-external " in line:
                continue
            m = re.search(r"\((undefined|common|[^)]*,[^)]*)\).* external (?:\[[^\]]*\] )?(\S+)", line)
            if not m:
                continue
            section, symbol = m.groups()
            if section == "undefined":
                if not symbol.startswith("_objc_msgSend$"):
                    referenced.add(symbol)
            else:
                defined.add(symbol)
    return referenced - defined


def defined_functions(path):
    """Text symbols (external and local) defined in an archive."""

    out = run("nm", "-j", "-U", str(path))
    return {l.strip() for l in out.splitlines() if l.strip() and not l.endswith(":")}


def flattened_calls(path, symbol, local, depth=2, seen=None):
    """call_sequence with calls to functions defined in the archive expanded."""

    seen = set() if seen is None else seen
    seen.add(symbol)
    result = []
    for target in call_sequence(path, symbol):
        if depth and target in local and target not in seen:
            result += flattened_calls(path, target, local, depth - 1, seen)
        else:
            result.append(target)
    return result
