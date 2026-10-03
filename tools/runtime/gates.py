"""Release gates. Every check raises GateFailure; nothing is published after one."""

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


STUB = """\
/* RenPyLinter link gate: SDL2main provides main() and calls SDL_main. */
extern int %(entry)s(int argc, char **argv);
int SDL_main(int argc, char **argv) { return %(entry)s(argc, argv); }
"""

STUB_NO_ENTRY = """\
/* RenPyLinter link gate for a dependency layer without an engine. */
int SDL_main(int argc, char **argv) { (void) argc; (void) argv; return 0; }
"""


def link(target, *, force_load, archives, frameworks_dir, frameworks, libraries, entry=None, log_dir=None):
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
        return machos.build_versions(out) and True


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
