"""Apple toolchain for renpy-build's iOS and host tasks.

Upstream renpy-build cross-compiles iOS on Linux with clang/lld and SDK
tarballs. RenPyLinter builds on a macOS runner instead, so after upstream's
``build_environment`` has filled in its generic variables, this module replaces
the compiler, archiver and SDK settings with the selected Xcode's.

Only toolchain variables are touched. Task recipes and configure arguments
stay exactly as upstream wrote them, apart from the deployment target, which
RenPyLinter fixes at iOS 15.6, and the optimisation level, which RenPyLinter
fixes at -Os for every version (see _set_optimization).
"""

import os
import re
import shlex
import subprocess
from functools import lru_cache
from pathlib import Path

MINIMUM_IOS = "15.6"

# Every object and link is optimised for size, whatever upstream chose.
OPTIMIZATION = "-Os"
_OPTIMIZATION_FLAG = re.compile(r"(?<!\S)-O(?:[0-3sz]|fast)?(?!\S)")
_CMAKE_BUILD_TYPE = re.compile(r"-DCMAKE_BUILD_TYPE=\S+")

# Upstream tasks copy the build machine's /usr/share/misc/config.sub (Ubuntu
# autotools-dev 20220109.1). That exact file is vendored here; engine branches
# refer to it as {{ config_sub }}.
CONFIG_SUB = Path(__file__).resolve().parent / "config.sub"


# renpy-build arch name -> (Xcode SDK, clang target triple)
IOS_TARGETS = {
    "arm64": ("iphoneos", f"arm64-apple-ios{MINIMUM_IOS}"),
    "sim-arm64": ("iphonesimulator", f"arm64-apple-ios{MINIMUM_IOS}-simulator"),
}

_VERSION_MIN = re.compile(r"\s-m(?:iphoneos|ios-simulator|ios|macos|macosx)-version-min=\S+")


@lru_cache(maxsize=None)
def xcrun(*args):
    return subprocess.check_output(["xcrun", *args], text=True).strip()


def sdk_path(sdk):
    return xcrun("--sdk", sdk, "--show-sdk-path")


def tool(sdk, name):
    return xcrun("--sdk", sdk, "--find", name)


HOST_LIBRARIES = ("xz", "openssl@3", "libffi")


@lru_cache(maxsize=None)
def host_library_prefixes():
    """Homebrew prefixes of HOST_LIBRARIES.

    Tasks run with Homebrew off PATH, so the driver resolves the prefixes and
    passes them in RPL_HOST_LIBRARY_PREFIXES.
    """

    if "RPL_HOST_LIBRARY_PREFIXES" in os.environ:
        return os.environ["RPL_HOST_LIBRARY_PREFIXES"].split(":")
    return [subprocess.check_output(["brew", "--prefix", f], text=True).strip() for f in HOST_LIBRARIES]


def description():
    """Toolchain identity recorded in build-info.json."""

    version = subprocess.check_output(["xcodebuild", "-version"], text=True).split("\n")
    return {
        "xcode": version[0].replace("Xcode ", "").strip(),
        "xcode_build": version[1].replace("Build version ", "").strip(),
        "developer_dir": subprocess.check_output(["xcode-select", "-p"], text=True).strip()
        if not os.environ.get("DEVELOPER_DIR") else os.environ["DEVELOPER_DIR"],
        "clang": subprocess.check_output([tool("iphoneos", "clang"), "--version"], text=True).split("\n")[0],
        "sdks": {sdk: xcrun("--sdk", sdk, "--show-sdk-version") for sdk in ("iphoneos", "iphonesimulator", "macosx")},
        "minimum_ios": MINIMUM_IOS,
        "host_libraries": subprocess.check_output(["brew", "list", "--versions", *HOST_LIBRARIES],
                                                  text=True).split("\n")[:-1],
    }


def _strip_version_min(c, name):
    if name in c.environ:
        c.environ[name] = _VERSION_MIN.sub("", " " + c.environ[name]).strip()


def _set_tools(c, sdk, target_args):
    """Mirror upstream's llvm(): ccache-wrapped clang linking with lld.

    The flag order matters: upstream's tools/cmake_build_variables.cmake strips
    the literal "ccache " prefix and "-fuse-ld=lld -Wno-unused-command-line-argument ".
    Only the compiler binaries (Xcode's clang) and the SDK differ from upstream.
    """

    clang = shlex.quote(tool(sdk, "clang"))
    clangxx = shlex.quote(tool(sdk, "clang++"))
    clang_args = "-fuse-ld=lld -Wno-unused-command-line-argument " + target_args
    cxx_args = "-stdlib=libc++" if c.kind not in ("host", "host-python", "cross") else ""

    c.var("clang_args", clang_args)
    c.var("cxx_clang_args", cxx_args)
    c.env("CC", f"ccache {clang} {clang_args} -std=gnu17")
    c.env("CXX", f"ccache {clangxx} {clang_args} -std=gnu++17 {cxx_args}".rstrip())
    c.env("CPP", f"ccache {clang} {clang_args} -E")
    c.env("AR", tool(sdk, "ar"))
    c.env("RANLIB", tool(sdk, "ranlib"))
    c.env("STRIP", tool(sdk, "strip"))
    c.env("NM", tool(sdk, "nm"))
    c.var("lipo", tool(sdk, "lipo"))

    for name in ("READELF", "WINDRES", "RC", "LD"):
        c.environ.pop(name, None)


def _set_optimization(c):
    """Replace upstream's optimisation level with OPTIMIZATION.

    Upstream puts -O3 in CFLAGS/LDFLAGS (7.8, 8.1+) or only in CC/CXX (7.5,
    8.0), which _set_tools replaces, so those versions would otherwise build
    without any optimisation. CXXFLAGS is always set too: autotools projects
    fall back to their own "-g -O2" when it is unset.

    CMake's Release build type appends -O3 after CFLAGS, so CMake projects
    are built as MinSizeRel, which appends "-Os -DNDEBUG" instead (NDEBUG as
    in Release). CPython's OPT (-O3) precedes CFLAGS in Python 3, but in
    Python 2.7 it follows them (CFLAGS = BASECFLAGS @CFLAGS@ OPT EXTRA_CFLAGS),
    so the level is also passed in EXTRA_CFLAGS, which both versions append
    last and take from the environment. FFmpeg appends its own -O3 after
    --extra-cflags; its archives only serve the link gates (the application
    links the global FFmpeg layer).
    """

    for name in ("CFLAGS", "CXXFLAGS", "LDFLAGS"):
        rest = _OPTIMIZATION_FLAG.sub("", c.environ.get(name, "")).strip()
        c.environ[name] = f"{OPTIMIZATION} {rest}".rstrip()

    rest = _OPTIMIZATION_FLAG.sub("", c.environ.get("EXTRA_CFLAGS", "")).strip()
    c.environ["EXTRA_CFLAGS"] = f"{rest} {OPTIMIZATION}".lstrip()

    for name, value in list(c.variables.items()):
        if "-DCMAKE_BUILD_TYPE=" in value:
            c.var(name, _CMAKE_BUILD_TYPE.sub("-DCMAKE_BUILD_TYPE=MinSizeRel", value), expand=False)


def _add_cmake_args(c, extra):
    """Append to the variable holding upstream's CMake arguments.

    8.4+ keep them in ``cmake_args``; 7.8 and 8.1-8.3 expand them into
    ``cmake``. Either way it is the variable naming the project include.
    """

    for name, value in list(c.variables.items()):
        if "-DCMAKE_PROJECT_INCLUDE_BEFORE=" in value:
            c.var(name, value + extra, expand=False)


def apply(c):
    """Rewrite the toolchain part of a renpy-build Context in place."""

    c.var("config_sub", str(CONFIG_SUB))

    # Deterministic archive members (no timestamps/uid in ar headers).
    c.env("ZERO_AR_DATE", "1")

    _set_optimization(c)

    for name in ("SDKROOT", "MACOSX_DEPLOYMENT_TARGET", "IPHONEOS_DEPLOYMENT_TARGET"):
        c.environ.pop(name, None)

    if c.kind in ("host", "host-python", "cross"):
        sdk = "macosx"
        _set_tools(c, sdk, f"-isysroot {shlex.quote(sdk_path(sdk))}")
        # Upstream's host has libssl-dev, liblzma-dev and libffi-dev; the macOS
        # SDK lacks them. Only build-machine programs (host Python) see these.
        pkgconfig = ["{{ install }}/lib/pkgconfig"]
        for prefix in host_library_prefixes():
            c.env("CFLAGS", f"{{{{ CFLAGS }}}} -I{prefix}/include")
            c.env("CPPFLAGS", f"{{{{ CPPFLAGS }}}} -I{prefix}/include")
            c.env("LDFLAGS", f"{{{{ LDFLAGS }}}} -L{prefix}/lib")
            pkgconfig.append(f"{prefix}/lib/pkgconfig")
        # Nothing else from Homebrew (e.g. libb2) may be discovered.
        c.env("PKG_CONFIG_LIBDIR", ":".join(pkgconfig))
        c.environ.pop("PKG_CONFIG_PATH", None)
        return

    if c.platform != "ios":
        raise SystemExit(f"Only iOS targets are supported by this driver, not {c.platform}.")

    sdk, triple = IOS_TARGETS[c.arch]
    sysroot = sdk_path(sdk)

    _set_tools(c, sdk, f"-target {triple} -isysroot {shlex.quote(sysroot)}")

    # The -target triple carries the deployment target; drop upstream's 13.0 flags.
    for name in ("CFLAGS", "CXXFLAGS", "LDFLAGS", "CPPFLAGS"):
        _strip_version_min(c, name)

    # Upstream exports IPHONEOS_DEPLOYMENT_TARGET, which Linux compilers ignore.
    # Apple's /usr/bin/cc would apply it to build-machine helpers (freetype's
    # apinames) and produce iOS binaries; the -target triple already carries it.
    c.environ.pop("IPHONEOS_DEPLOYMENT_TARGET", None)

    # Only the target install tree may provide .pc files.
    c.env("PKG_CONFIG_LIBDIR", "{{ install }}/lib/pkgconfig")
    c.environ.pop("PKG_CONFIG_PATH", None)

    # Upstream points CMake at {{cross}}/sdk, which run_tasks links to Xcode's
    # SDK; CMake on a macOS host additionally needs the Apple variables.
    _add_cmake_args(c, f" -DCMAKE_OSX_SYSROOT={shlex.quote(sysroot)}"
                   " -DCMAKE_OSX_ARCHITECTURES=arm64"
                   f" -DCMAKE_OSX_DEPLOYMENT_TARGET={MINIMUM_IOS}")
