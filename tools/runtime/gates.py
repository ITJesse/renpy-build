"""Release gates. Every check raises GateFailure; nothing is published after one."""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import machos
import xcode_toolchain


class GateFailure(SystemExit):
    pass


def fail(message):
    raise GateFailure(f"GATE FAILED: {message}")


TARGET_SDK = {"ios-arm64": "iphoneos", "ios-sim-arm64": "iphonesimulator"}
TARGET_ARCH = {"ios-arm64": "arm64", "ios-sim-arm64": "sim-arm64"}


def archive_platforms(target, archives):
    """Single arm64 slice; every member carries the target platform and minos."""

    want_platform = machos.TARGET_PLATFORM[target]
    report = {}
    for archive in archives:
        if machos.archs(archive) != ["arm64"]:
            fail(f"{target}/{archive.name}: architectures {machos.archs(archive)}, want arm64 only")
        versions = machos.build_versions(archive)
        if not versions:
            fail(f"{target}/{archive.name}: no object members")
        bad = {m: v for m, v in versions.items()
               if v != (want_platform, xcode_toolchain.MINIMUM_IOS)}
        if bad:
            sample = dict(list(bad.items())[:5])
            fail(f"{target}/{archive.name}: {len(bad)} members with wrong LC_BUILD_VERSION "
                 f"(want platform {want_platform} minos {xcode_toolchain.MINIMUM_IOS}): {sample}")
        report[archive.name] = {"platform": machos.PLATFORMS[want_platform],
                                "minos": xcode_toolchain.MINIMUM_IOS, "members": len(versions)}
    return report


# On iOS, renpy-build's libSDL2main.a only holds SDL_dummy_main.o; the host
# application provides main(), so the shell does too.
STUB = """\
/* RenPyLinter link gate: the host application calls the engine entry point. */
extern int %(entry)s(int argc, char **argv);
int main(int argc, char **argv) { return %(entry)s(argc, argv); }
"""

STUB_NO_ENTRY = """\
/* RenPyLinter link gate for a dependency layer without an engine. */
int main(int argc, char **argv) { (void) argc; (void) argv; return 0; }
"""


def link(target, *, force_load, archives, frameworks_dir, frameworks, libraries, entry=None, log_dir=None,
         output=None):
    """Link an empty iOS executable with Apple's ld (as the application does).

    ``force_load`` archives are linked whole, so every one of their objects
    must resolve; the rest only satisfy references.
    """

    sdk = TARGET_SDK[target]
    triple = xcode_toolchain.IOS_TARGETS[TARGET_ARCH[target]][1]
    with tempfile.TemporaryDirectory() as tmp:
        stub = Path(tmp) / "stub.c"
        stub.write_text(STUB % {"entry": entry} if entry else STUB_NO_ENTRY)
        out = Path(tmp) / "shell"
        cmd = ["xcrun", "--sdk", sdk, "clang", "-target", triple, str(stub), "-o", str(out),
               "-Wl,-dead_strip_dylibs"]
        for a in force_load:
            cmd += ["-Wl,-force_load," + str(a)]
        cmd += [str(a) for a in archives if a not in force_load]
        cmd += ["-F", str(frameworks_dir)]
        for f in frameworks:
            cmd += ["-framework", f]
        cmd += [f"-l{l}" for l in libraries]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            names = ",".join(a.name for a in force_load) or "none"
            if log_dir:
                Path(log_dir).mkdir(parents=True, exist_ok=True)
                (Path(log_dir) / f"link-{target}-{names}.log").write_text(" ".join(cmd) + "\n\n" + result.stderr)
            fail(f"{target}: shell link with force_load={names} failed:\n{result.stderr[-4000:]}")
        if output:
            shutil.copy2(out, output)
        return True


def exports_diff(previous, current):
    """Symbols (archive, name) present before and missing now."""

    return sorted(set(previous) - set(current))


def entry_points(archive, wanted):
    exported = set(machos.exported_symbols(archive))
    missing = [s for s in wanted if "_" + s not in exported]
    if missing:
        fail(f"{archive}: missing entry points {missing}")
    return wanted


ZONE_EPILOGUE = ["_PyMem_SetAllocator"] * 3 + ["_malloc_zone_pressure_relief", "_malloc_destroy_zone"]
PYTHON_MAIN = {
    "renpython_main": "_Py_BytesMain",
    "renpython_main_wide": "_Py_Main",
    "launcher_main": "_Py_RunMain",
    "launcher_main_wide": "_Py_RunMain",
}


def session_zone(archive):
    """RenPyPythonSession marker, and the zone teardown right after each Python main."""

    if b"RenPyPythonSession" not in Path(archive).read_bytes():
        fail(f"{archive}: RenPyPythonSession marker missing")
    report = {}
    for function, main in PYTHON_MAIN.items():
        calls = machos.call_sequence(archive, "_" + function)
        if calls.count(main) != 1:
            fail(f"{archive}: {function} calls {main} {calls.count(main)} times: {calls}")
        after = calls[calls.index(main) + 1:][:len(ZONE_EPILOGUE)]
        if after != ZONE_EPILOGUE:
            fail(f"{archive}: {function}: after {main} got {after}, want {ZONE_EPILOGUE}")
        # The installer may be called or inlined (then malloc_create_zone shows).
        installs = [i for i, c in enumerate(calls)
                    if c in ("_rpl_install_python_zone_allocator", "_malloc_create_zone")]
        install = installs[0] if installs else -1
        preinit = [i for i, c in enumerate(calls) if c.startswith("_Py_PreInitialize")]
        initialize = [i for i, c in enumerate(calls) if c == "_Py_InitializeFromConfig"]
        if not (preinit and initialize and preinit[0] < install < initialize[0]):
            fail(f"{archive}: {function}: allocator install not between Py_PreInitialize* and "
                 f"Py_InitializeFromConfig: {calls}")
        report[function] = {"python_main": main, "after": after}
    return report


# Values of the Cubism Core declarations renpy.gl2.live2dmodel relies on, as
# in Cubism SDK 4 (upstream builds with CubismSdkForNative-4-r.6.2 / 4-r.1).
# Core functions are resolved by name at run time, so these constants and the
# vector layouts are the whole compile-time ABI surface.
LIVE2D_ABI = """\
#include <stddef.h>
#include "Live2DCubismCore.h"
#define CHECK(e) _Static_assert(e, #e)
CHECK(csmAlignofMoc == 64);
CHECK(csmAlignofModel == 16);
CHECK(csmBlendAdditive == 1);
CHECK(csmBlendMultiplicative == 2);
CHECK(csmIsDoubleSided == 4);
CHECK(csmIsInvertedMask == 8);
CHECK(csmIsVisible == 1);
CHECK(csmVisibilityDidChange == 2);
CHECK(csmOpacityDidChange == 4);
CHECK(csmDrawOrderDidChange == 8);
CHECK(csmRenderOrderDidChange == 16);
CHECK(csmVertexPositionsDidChange == 32);
CHECK(csmMocVersion_Unknown == 0);
CHECK(csmMocVersion_30 == 1);
CHECK(csmMocVersion_33 == 2);
CHECK(csmMocVersion_40 == 3);
CHECK(sizeof(csmFlags) == 1);
CHECK(sizeof(csmVersion) == 4);
CHECK(sizeof(csmMocVersion) == 4);
CHECK(sizeof(csmVector2) == 8 && offsetof(csmVector2, Y) == 4);
CHECK(sizeof(csmVector4) == 16 && offsetof(csmVector4, W) == 12);
"""


def live2d_abi(header_dir):
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.c"
        probe.write_text(LIVE2D_ABI)
        result = subprocess.run(["xcrun", "--sdk", "iphoneos", "clang", "-target",
                                 xcode_toolchain.IOS_TARGETS["arm64"][1], "-fsyntax-only",
                                 "-I", str(header_dir), str(probe)], capture_output=True, text=True)
        if result.returncode != 0:
            fail(f"Live2DCubismCore.h ABI differs from what live2dmodel was written against:\n{result.stderr}")
    return "pass"


def system_imports_vs_upstream(ours, upstream, reviewed):
    """System symbols we reference that upstream's official build does not.

    A strong reference to a symbol missing from the device's libraries stops
    the app at launch, an unguarded weak one crashes when called; configure
    link tests do not know the deployment target. Upstream's official renios archives were built
    against an older SDK, so every system symbol they need exists on old iOS.
    Anything new must be listed in families.json "reviewed_system_imports"
    with the evidence that it exists on the minimum iOS version.
    """

    new = sorted(ours - upstream)
    unreviewed = [s for s in new if s not in reviewed]
    if unreviewed:
        fail("strong system imports not used by upstream's official iOS build and not reviewed for "
             f"iOS {xcode_toolchain.MINIMUM_IOS} (families.json reviewed_system_imports): {unreviewed}")
    return {"ours": len(ours), "upstream": len(upstream), "new_reviewed": new}
