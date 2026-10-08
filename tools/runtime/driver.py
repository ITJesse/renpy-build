#!/usr/bin/env python3
"""RenPyLinter iOS runtime pipeline driver.

    driver.py prepare --src SRC
        Clone Ren'Py at the lock's commit, create the build Python environment
        and the pinned build-tool environment.

    driver.py deps --family NAME --src SRC --out DIR [--previous TARBALL]
        Build a family's dependency layer from its baseline branch, gate it and
        lay out the release bundle in DIR.

    driver.py engine --version X.Y.Z --src SRC --deps TARBALL --sdk TARBALL --out DIR
        Build one engine against a released dependency layer, gate it and lay
        out the release bundle in DIR.

SRC is a checkout of a renpylinter/<version> branch. The global SDL layer's
release tarball (sdl2-ios-* or sdl3-ios-*, named in the lock) is passed with
--sdl to both build commands.
"""

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLING = HERE.parent.parent
sys.path.insert(0, str(HERE))

import bundle  # noqa: E402
import pybytecode  # noqa: E402
import gates  # noqa: E402
import machos  # noqa: E402
import recipe  # noqa: E402
import series  # noqa: E402
import xcode_toolchain  # noqa: E402

TARGETS = ["ios-arm64", "ios-sim-arm64"]
ARCH = {"ios-arm64": "arm64", "ios-sim-arm64": "sim-arm64"}
ENGINE_FOLDER = {"ios-arm64": "release", "ios-sim-arm64": "debug"}
# Global SDL layer: releases built on renpylinter/sdl2 and renpylinter/sdl3.
SDL_ASSET = {"SDL2": "renpylinter-sdl2-ios-arm64.tar.gz", "SDL3": "renpylinter-sdl3-ios-arm64.tar.gz"}

# Tasks whose outputs are not part of the dependency bundle: build tools,
# the MetalANGLE framework (global layer) and the SDK link.
UNPACKAGED_MODULES = {"nasm", "metalangle"}

# Modules built purely from renpy-build's own files.
FIRST_PARTY = {"toolchain": "source/mockrt.c"}

LICENSE_FALLBACK = {"zlib": ["README"]}

LICENSE_NAME = re.compile(r"^(COPYING|LICEN[CS]E|NOTICE|COPYRIGHT)([._-].*)?$", re.I)

# System frameworks and libraries an iOS app links for the Ren'Py runtime.
LINK_FRAMEWORKS = [
    "AudioToolbox", "AVFoundation", "CoreAudio", "CoreBluetooth", "CoreFoundation", "CoreGraphics",
    "CoreHaptics", "CoreMedia", "CoreMotion", "CoreVideo", "Foundation", "GameController",
    "IOKit", "Metal", "OpenGLES", "QuartzCore", "Security", "UIKit", "VideoToolbox", "MetalANGLE",
    # SDL3_image's ImageIO backend (8.6); upstream's renios project links both.
    "ImageIO", "MobileCoreServices",
]
LINK_LIBRARIES = ["c++", "iconv"]


def log(message):
    print(f"[rpl] {message}", flush=True)


def run(cmd, **kwargs):
    log("$ " + " ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], check=True, **kwargs)


def output(cmd, **kwargs):
    return subprocess.check_output([str(c) for c in cmd], text=True, **kwargs).strip()


def families():
    return json.loads((TOOLING / "families.json").read_text())


def load_lock(src):
    return json.loads((src / "renpylinter.lock.json").read_text())


def sdl_layer(lock):
    """The global SDL layer an engine branch links: lock "sdl", or "sdl2" up to 8.5."""

    if "sdl" in lock:
        layer = dict(lock["sdl"])
    else:
        layer = {"library": "SDL2", **lock["sdl2"]}
    if layer["library"] not in SDL_ASSET:
        raise SystemExit(f"Unknown global SDL library {layer['library']}")
    layer.setdefault("asset", SDL_ASSET[layer["library"]])
    return layer


def renpy_version(lock):
    """The Ren'Py version vc_version.py names: the tag, or a nightly's version."""

    return lock.get("renpy_version") or lock["renpy_tag"]


def git_head(path):
    return output(["git", "-C", path, "rev-parse", "HEAD"])


def commit_time(path):
    return int(output(["git", "-C", path, "log", "-1", "--format=%ct"]))


# Environment ##################################################################

def build_python(src, lock):
    if lock["host_python"] == "uv-project":
        return src / "renpy" / ".venv" / "bin" / "python"
    return src / "tmp" / "rpl-env" / "bin" / "python"


def tool_path(src):
    return src / "tmp" / "rpl-tools" / "bin"


def check_xcode(lock, allow_mismatch=False):
    """The lock pins the Xcode release builds use; local trial builds may override."""

    toolchain = xcode_toolchain.description()
    toolchain["matches_lock"] = toolchain["xcode"] == lock["xcode"]
    if not toolchain["matches_lock"]:
        if not allow_mismatch:
            raise SystemExit(f"Xcode {toolchain['xcode']} selected, lock requires {lock['xcode']}")
        log(f"WARNING: Xcode {toolchain['xcode']} != lock {lock['xcode']}; not a release build")
    return toolchain


def prepare(args):
    src = args.src.resolve()
    lock = load_lock(src)

    renpy = src / "renpy"
    checkout(renpy, "https://github.com/renpy/renpy.git", lock["renpy_commit"])
    # Up to 8.4, pygame_sdl2 is a separate repository next to renpy/.
    if lock.get("pygame_sdl2_commit"):
        checkout(src / "pygame_sdl2", "https://github.com/renpy/pygame_sdl2.git", lock["pygame_sdl2_commit"])

    if lock["host_python"] == "uv-project":
        install_uv_lock(src, lock)
        run(["uv", "sync", "--project", renpy, "--frozen", "--no-install-project"])
    else:
        env = src / "tmp" / "rpl-env"
        if not env.exists():
            run(["uv", "venv", "-q", "--python", lock["host_python"], env])
            run(["uv", "pip", "install", "-q", "--python", env / "bin" / "python",
                 *host_requirements(src, lock)])

    tools = src / "tmp" / "rpl-tools"
    if not (tools / "bin" / "python").exists():
        run(["uv", "venv", "-q", "--python", "3.12", tools])
    run(["uv", "pip", "install", "-q", "--python", tools / "bin" / "python", "-r", HERE / "build-tools.txt"])
    build_autotools(tools)


def install_uv_lock(src, lock):
    """Ren'Py 8.6 no longer tracks uv.lock; the branch provides a pinned one.

    renpy/uv.lock is ignored by Ren'Py's .gitignore, so the checkout stays
    clean. Upstream's pip task reads its package versions from it.
    """

    pin = lock.get("renpy_uv_lock")
    target = src / "renpy" / "uv.lock"
    if not pin:
        if not target.exists():
            raise SystemExit("renpy has no uv.lock and the lock names no renpy_uv_lock")
        return
    source = src / pin["file"]
    if bundle.sha256(source) != pin["sha256"]:
        raise SystemExit(f"{pin['file']} sha256 does not match the lock")
    tracked = subprocess.run(["git", "-C", str(src / "renpy"), "ls-files", "--error-unmatch", "uv.lock"],
                             capture_output=True).returncode == 0
    if tracked:
        raise SystemExit("renpy tracks uv.lock; drop renpy_uv_lock from the lock")
    if target.exists() and bundle.sha256(target) != pin["sha256"]:
        raise SystemExit(f"{target} differs from {pin['file']}")
    shutil.copy2(source, target)


def build_autotools(prefix):
    """GNU autotools pinned in autotools.json, built from verified sources."""

    config = json.loads((HERE / "autotools.json").read_text())
    pins = config["packages"]
    stamp = prefix / "autotools.json"
    if stamp.exists() and json.loads(stamp.read_text()) == config:
        return
    work = prefix / "src"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    env = dict(os.environ, PATH=f"{prefix / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin")
    for env_name in ("CFLAGS", "CPPFLAGS", "LDFLAGS", "CPATH", "LIBRARY_PATH", "SDKROOT"):
        env.pop(env_name, None)
    for pin in pins:
        archive = work / Path(pin["url"]).name
        urls = [pin["url"]]
        if pin["url"].startswith("https://ftp.gnu.org/gnu/"):
            urls.insert(0, pin["url"].replace("https://ftp.gnu.org/gnu/", "https://mirrors.kernel.org/gnu/", 1))
        for index, url in enumerate(urls):
            try:
                run(["curl", "-sfL", "--connect-timeout", "15", "--max-time", "90",
                     "--retry", "1", "-o", archive, url])
                break
            except subprocess.CalledProcessError:
                if index == len(urls) - 1:
                    raise
        if bundle.sha256(archive) != pin["sha256"]:
            raise SystemExit(f"{archive.name}: sha256 mismatch")
        run(["tar", "xf", archive, "-C", work])
        source = work / re.sub(r"\.tar\.(xz|gz)$", "", archive.name)
        run(["./configure", f"--prefix={prefix}", *pin["configure"]], cwd=source, env=env,
            stdout=subprocess.DEVNULL)
        run(["make", f"-j{os.cpu_count() or 2}"], cwd=source, env=env, stdout=subprocess.DEVNULL)
        run(["make", "install"], cwd=source, env=env, stdout=subprocess.DEVNULL)
    shutil.rmtree(work)
    for name, digest in config["aclocal"]["files"].items():
        source = HERE / "aclocal" / name
        if bundle.sha256(source) != digest:
            raise SystemExit(f"aclocal/{name}: sha256 mismatch")
        shutil.copy2(source, prefix / "share" / "aclocal" / name)
    stamp.write_text(json.dumps(config))


# Host settings that would leak host headers, libraries or .pc files into
# target builds. Upstream builds in a clean Ubuntu environment without them.
HOST_LEAKS = ("PKG_CONFIG_PATH", "PKG_CONFIG_LIBDIR", "PKG_CONFIG_SYSROOT_DIR", "CPATH", "C_INCLUDE_PATH",
              "CPLUS_INCLUDE_PATH", "OBJC_INCLUDE_PATH", "LIBRARY_PATH", "CFLAGS", "CXXFLAGS", "CPPFLAGS",
              "LDFLAGS", "SDKROOT", "ACLOCAL_PATH", "MACOSX_DEPLOYMENT_TARGET", "IPHONEOS_DEPLOYMENT_TARGET")


def host_requirements(src, lock):
    """uv arguments for the branch's requirements.txt.

    The lock may skip packages that cannot install on the host Python (with a
    reason) and add constraints for packages upstream left unpinned whose
    later releases break the pinned ones.
    """

    adjust = lock.get("host_requirements", {})
    skip = {entry["requirement"] for entry in adjust.get("skip", [])}
    lines = (src / "requirements.txt").read_text().splitlines()
    kept = [l for l in lines if l.strip() and l.strip() not in skip]
    missing = skip - {l.strip() for l in lines}
    if missing:
        raise SystemExit(f"host_requirements.skip names lines not in requirements.txt: {sorted(missing)}")
    work = src / "tmp" / "rpl-requirements"
    work.mkdir(parents=True, exist_ok=True)
    (work / "requirements.txt").write_text("\n".join(kept) + "\n")
    args = ["-r", str(work / "requirements.txt")]
    constraints = [entry["requirement"] for entry in adjust.get("constraints", [])]
    if constraints:
        (work / "constraints.txt").write_text("\n".join(constraints) + "\n")
        args += ["-c", str(work / "constraints.txt")]
    return args


def checkout(path, url, commit):
    if not (path / ".git").exists():
        run(["git", "init", "-q", path])
        run(["git", "-C", path, "fetch", "-q", "--depth", "1", url, commit])
        run(["git", "-C", path, "checkout", "-q", "FETCH_HEAD"])
    if git_head(path) != commit:
        raise SystemExit(f"{path} is at {git_head(path)}, lock requires {commit}")


# Host programs tasks may use (8.6's librenpy runs `uv --project renpy run`).
# Everything else Homebrew provides (for example sdl2-config or GNU install)
# stays off PATH, as on upstream's clean build host, so local builds behave
# like CI.
HOST_PROGRAMS = ("pkg-config", "ccache", "ld64.lld", "uv")

# Apple's clang passes the compiler's last -O option on to the Darwin linker
# verbatim; upstream's clang does not. Apple's ld accepts every level, but
# ld64.lld only takes a number and fails on -Os ("number expected"), which
# breaks every command that compiles and links at once, configure's probes
# included. lld's -O only decides whether bind opcodes in a linked image are
# compacted and never touches the archived objects, so the wrapper drops the
# levels lld cannot parse, as if upstream's clang had driven the link.
LD64_LLD_WRAPPER = """#!/bin/sh
for arg do
    shift
    case $arg in
        -O|-Os|-Oz|-Og|-Ofast) ;;
        *) set -- "$@" "$arg" ;;
    esac
done
exec {real} "$@"
"""


def host_bin(src):
    path = src / "tmp" / "rpl-hostbin"
    path.mkdir(parents=True, exist_ok=True)
    for name in HOST_PROGRAMS:
        target = shutil.which(name)
        if not target:
            raise SystemExit(f"{name} is required on the build machine")
        link = path / name
        if link.is_symlink() or link.exists():
            link.unlink()
        if name == "ld64.lld":
            link.write_text(LD64_LLD_WRAPPER.format(real=shlex.quote(target)))
            link.chmod(0o755)
        else:
            link.symlink_to(target)
    return path


def task_env(src, lock):
    env = {k: v for k, v in os.environ.items() if k not in HOST_LEAKS}
    # Like `uv run` / an activated venv: the build environment's scripts
    # (cython, ...) come first, then the pinned build tools, the few host
    # programs and the system directories.
    env["PATH"] = ":".join([str(build_python(src, lock).parent), str(tool_path(src)), str(host_bin(src)),
                            "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    env["VIRTUAL_ENV"] = str(build_python(src, lock).parent.parent)
    env["RPL_HOST_LIBRARY_PREFIXES"] = ":".join(xcode_toolchain.host_library_prefixes())
    env["LIBTOOLIZE"] = "glibtoolize"
    env.setdefault("CCACHE_COMPILERCHECK", "content")
    env.setdefault("PYTHONHASHSEED", "0")
    # `uv run` in tasks must use the pinned renpy/uv.lock as is, never re-lock.
    env["UV_FROZEN"] = "1"
    return env


def run_tasks(src, lock, python_major, modules):
    run([build_python(src, lock), "-u", HERE / "run_tasks.py", "--root", src, "--python", python_major,
         "--archs", ",".join(ARCH[t] for t in TARGETS), *modules], env=task_env(src, lock))


def install_dir(src, target):
    return src / "tmp" / f"install.ios-{ARCH[target]}"


# Inputs #######################################################################

def verify_tarball(path, expected, what):
    actual = bundle.sha256(path)
    if actual != expected:
        raise SystemExit(f"{what}: {path} sha256 {actual}, lock requires {expected}")
    return actual


def unpack_sdl(tar_path, lock, work):
    layer = sdl_layer(lock)
    verify_tarball(tar_path, layer["sha256"], f"{layer['library']} release")
    dest = work / "sdl"
    shutil.rmtree(dest, ignore_errors=True)
    bundle.extract(tar_path, dest)
    bundle.verify_sums(dest)
    archives = {t: dest / ENGINE_FOLDER[t] / f"lib{layer['library']}.a" for t in TARGETS}
    for archive in archives.values():
        if not archive.is_file():
            raise SystemExit(f"{layer['release']} has no {archive.relative_to(dest)}")
    return archives


def check_branch(src, lock, version):
    if lock["engine"] != version:
        raise SystemExit(f"{src} is the {lock['engine']} branch, not {version}")
    # Released engines are based on an upstream tag; nightly ones (8.6.0) on
    # the upstream commit the nightly was built from.
    base = lock.get("renpy_build_tag") or lock["renpy_build_commit"]
    base_commit = output(["git", "-C", src, "rev-parse", base + "^{commit}"])
    if base_commit != lock["renpy_build_commit"]:
        raise SystemExit(f"{base} is {base_commit}, lock says {lock['renpy_build_commit']}")
    merge_base = output(["git", "-C", src, "merge-base", "HEAD", base_commit])
    if merge_base != base_commit:
        raise SystemExit(f"branch is not based on {base}")
    if series.digest(src) != lock["patches_sha256"]:
        raise SystemExit(f"patches_sha256 {series.digest(src)} does not match the lock")
    # Root patches applied by an earlier (resumed) run are the only allowed edits.
    status = subprocess.check_output(["git", "-C", str(src), "status", "--porcelain=v1", "-z",
                                      "--untracked-files=no"], text=True)
    dirty = {entry[3:] for entry in status.split("\0") if entry}
    unexpected = dirty - series.root_patch_paths(src)
    if unexpected:
        raise SystemExit(f"{src} has uncommitted changes: {sorted(unexpected)}")


def collect_licenses(src, modules, dest):
    """Copy license files of every module's sources; fail if one has none."""

    found = {}
    for module in modules:
        if module in FIRST_PARTY:
            # renpy-build's own code; the repository has no separate license file.
            source = src / FIRST_PARTY[module]
            (dest / module).mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest / module / source.name)
            (dest / module / "NOTICE.txt").write_text(
                f"Built from renpy-build {FIRST_PARTY[module]} (included here); "
                "https://github.com/renpy/renpy-build has no separate license file.\n")
            found[module] = sorted(str(p.relative_to(dest)) for p in (dest / module).iterdir())
            continue
        roots = [src / "tmp" / "build" / f"{module}.ios-arm64", src / "tmp" / "source" / module]
        # 8.6's sdl3 and sdl3_image unpack to tmp/source/<Name>-<version>.
        source = src / "tmp" / "source"
        if source.is_dir():
            roots += sorted(p for p in source.iterdir()
                            if p.is_dir() and p.name.lower().startswith(module + "-"))
        hits = []
        for root in roots:
            if not root.is_dir():
                continue
            for path in sorted(root.glob("*")) + sorted(root.glob("*/*")) + sorted(root.glob("*/docs/*")):
                if path.is_file() and LICENSE_NAME.match(path.name):
                    hits.append(path)
        if not hits:
            # Sources that keep their license in another file (zlib < 1.2.12: README).
            for root in roots:
                for name in LICENSE_FALLBACK.get(module, []):
                    hits += sorted(p for p in root.glob(f"*/{name}") if p.is_file())
        if not hits:
            raise SystemExit(f"No license file found for {module} under {roots}")
        for path in hits:
            target = dest / module / path.name
            if target.exists() and bundle.sha256(target) != bundle.sha256(path):
                target = dest / module / f"{path.parent.name}-{path.name}"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        found[module] = sorted(str(p.relative_to(dest)) for p in (dest / module).iterdir())
    return found


def archive_report(target, archives):
    report = gates.archive_platforms(target, archives)
    for archive in archives:
        report[archive.name].update(machos.section_sizes(archive))
        report[archive.name]["sha256"] = bundle.sha256(archive)
    return report


def export_lines(target_dir):
    lines = []
    for archive in sorted((target_dir / "lib").glob("*.a")):
        lines += [f"{archive.name}\t{s}" for s in machos.exported_symbols(archive)]
    return lines


def weak_export_lines(target_dir):
    lines = []
    for archive in sorted((target_dir / "lib").glob("*.a")):
        lines += [f"{archive.name}\t{s}" for s in sorted(machos.weak_only_symbols(archive))]
    return lines


def sdl_tree_info(src, lock, recipe_modules):
    """The SDL the dependency tree compiles for headers and SDL*_image."""

    library = sdl_layer(lock)["library"]
    module = library.lower()
    task = (src / "tasks" / f"{module}.py").read_text()
    version = re.search(r'^version\s*=\s*"([^"]+)"', task, re.M).group(1)
    info = {"library": library, "version": version, "recipe": recipe_modules[module],
            "note": f"Built in the dependency tree for headers and {library}_image only; "
                    f"lib{library}.a is not part of this bundle (global layer: {module}-ios release)."}
    if library == "SDL2":
        info["source_sha256"] = bundle.sha256(src / "source" / f"SDL2-{version}.tar.gz")
    return info


def tars_info(src):
    """sha256 of every archive tasks downloaded to tmp/tars (8.6+ download at build time)."""

    tars = src / "tmp" / "tars"
    if not tars.is_dir():
        return {}
    return {p.name: bundle.sha256(p) for p in sorted(tars.iterdir()) if p.is_file()}


# deps #########################################################################

def deps(args):
    started = time.time()
    config = families()
    family = config["families"][args.family]
    baseline = family["baseline"]
    src = args.src.resolve()
    out = args.out.resolve()
    lock = load_lock(src)

    # Verification builds (e.g. legacy's 7.8.2/8.3.7 comparison) may build the
    # layer from another member's branch; those bundles are never published.
    source_version = args.source_version or baseline["version"]
    if source_version not in family["members"]:
        raise SystemExit(f"{source_version} is not a member of {args.family}")
    check_branch(src, lock, source_version)
    toolchain = check_xcode(lock, args.trial)

    computed = recipe.compute(src, family["deps_modules"])
    # Nightly families follow upstream master; their engines check the recipe
    # against the deps release instead (see engine()).
    recipe_matches = family["recipe_sha256"] in (None, computed["recipe_sha256"])
    if not recipe_matches and source_version == baseline["version"] and not args.trial:
        raise SystemExit(f"families.json recipe_sha256 for {args.family} is {family['recipe_sha256']}, "
                         f"checkout computes {computed['recipe_sha256']}")

    series.apply(src)
    run_tasks(src, lock, config["versions"][source_version]["python"], family["deps_modules"])

    if out.exists():
        raise SystemExit(f"{out} exists; refusing to mix with an earlier bundle")
    out.mkdir(parents=True)

    sdl = unpack_sdl(args.sdl, lock, src / "tmp" / "rpl-inputs")
    global_archives = set(config["global_archives"])
    expected = set(family["deps_archives"])

    info_targets = {}
    relocated = {}
    for target in TARGETS:
        install = install_dir(src, target)
        lib = install / "lib"
        built = {p.name for p in lib.glob("*.a") if not p.is_symlink()}
        deps_set = built - global_archives
        if deps_set != expected:
            raise SystemExit(f"{target}: dependency archives {sorted(deps_set)} differ from families.json "
                             f"deps_archives (missing {sorted(expected - deps_set)}, extra {sorted(deps_set - expected)})")

        tdir = out / target
        (tdir / "lib").mkdir(parents=True)
        for name in sorted(deps_set):
            shutil.copy2(lib / name, tdir / "lib" / name)
        if (lib / "pkgconfig").is_dir():
            bundle.copy_tree(lib / "pkgconfig", tdir / "lib" / "pkgconfig")
        bundle.copy_tree(install / "include", tdir / "include")
        relocated[target] = bundle.relocate_out(tdir, install)

        check = out / "link-check" / target
        check.mkdir(parents=True)
        for name in config["link_check_archives"]:
            shutil.copy2(lib / name, check / name)

    markers = out / "done-markers"
    markers.mkdir()
    packaged = set(family["deps_modules"]) - UNPACKAGED_MODULES
    for marker in sorted((src / "tmp" / "complete").iterdir()):
        task_name, _, rest = marker.name.partition("-")
        module, _, where = rest.partition(".")
        if module in packaged and where in ("ios-arm64", "ios-sim-arm64"):
            (markers / marker.name).write_text("renpylinter deps bundle\n")

    licenses = collect_licenses(src, sorted(packaged), out / "LICENSES")

    # Gates.
    gate_report = {"platforms": {}, "links": {}, "exports": {}}
    for target in TARGETS:
        archives = sorted((out / target / "lib").glob("*.a"))
        info_targets[target] = archive_report(target, archives)
        gate_report["platforms"][target] = "pass"
        externals = sorted((out / "link-check" / target).glob("*.a")) + [sdl[target]]
        for archive in archives:
            gates.link(target, force_load=[archive], archives=archives + externals,
                       frameworks_dir=install_dir(src, target), frameworks=LINK_FRAMEWORKS,
                       libraries=LINK_LIBRARIES, log_dir=src / "tmp" / "rpl-logs")
        gate_report["links"][target] = {"force_loaded": [a.name for a in archives], "result": "pass"}

        lines = export_lines(out / target)
        (out / "exports").mkdir(exist_ok=True)
        (out / "exports" / f"{target}.txt").write_text("\n".join(lines) + "\n")

    upstream = unpack_upstream_renios(args.upstream_renios, lock, src / "tmp" / "rpl-inputs")
    gate_report["system_imports"] = {}
    for target in TARGETS:
        ours = machos.external_references(sorted((out / target / "lib").glob("*.a"))
                                          + sorted((out / "link-check" / target).glob("*.a")) + [sdl[target]])
        gate_report["system_imports"][target] = system_import_gate(ours, upstream, target, config)

    if args.previous:
        prev_dir = src / "tmp" / "rpl-inputs" / "previous"
        shutil.rmtree(prev_dir, ignore_errors=True)
        bundle.extract(args.previous, prev_dir)
        for target in TARGETS:
            previous = (prev_dir / "exports" / f"{target}.txt").read_text().splitlines()
            current = (out / "exports" / f"{target}.txt").read_text().splitlines()
            removed, dropped = gates.exports_diff(previous, current, weak_export_lines(prev_dir / target))
            if removed:
                gates.fail(f"{target}: {len(removed)} exported symbols removed since previous release: {removed[:20]}")
            gate_report["exports"][target] = {"previous": args.previous_tag, "removed": 0,
                                              "weak_dropped": len(dropped),
                                              "added": len(set(current) - set(previous))}
    else:
        gate_report["exports"] = {"previous": None, "note": "first release of this family"}

    info = {
        "schema": 1,
        "kind": "deps",
        "family": args.family,
        "baseline": {**baseline, "branch_commit": git_head(src)},
        "source_version": source_version,
        "recipe_matches_families_json": recipe_matches,
        "tooling_commit": git_head(TOOLING),
        "recipe_sha256": computed["recipe_sha256"],
        "recipe": computed["modules"],
        "patches_sha256": series.digest(src),
        "patches": series.describe(src),
        "toolchain": toolchain,
        "build_tools": {"pip": [l for l in (HERE / "build-tools.txt").read_text().split("\n")
                                if l and not l.startswith("#")],
                        "autotools": json.loads((HERE / "autotools.json").read_text())},
        "sdl_build_tree": sdl_tree_info(src, lock, computed["modules"]),
        "sdl_link_check": sdl_layer(lock),
        "downloads": tars_info(src),
        "metalangle": {p.name: bundle.sha256(p) for p in sorted((src / "source").glob("MetalANGLE*"))},
        "modules": family["deps_modules"],
        "archives": info_targets,
        "relocated_files": relocated,
        "install_prefix_placeholder": bundle.PREFIX_PLACEHOLDER,
        "link_check_note": "link-check/ holds FFmpeg and aom built in this tree, used only by the link "
                           "gates. The application links the global FFmpeg layer instead.",
        "licenses": licenses,
        "gates": gate_report,
        "source_date_epoch": commit_time(src),
        "build_seconds": int(time.time() - started),
    }
    finish(out, info, args.trial)


def finish(out, info, trial):
    info["trial"] = trial
    info["files"] = {str(p.relative_to(out)): bundle.sha256(p) for p in bundle.files(out)}
    bundle.write_json(out / "build-info.json", info)
    bundle.write_sums(out)
    log(f"bundle ready: {out}")


# engine #######################################################################

def unpack_deps(src, lock, tar_path, trial=False):
    if trial and lock["deps"]["sha256"] != bundle.sha256(tar_path):
        log("WARNING: deps tarball is not the locked release (trial build)")
    else:
        verify_tarball(tar_path, lock["deps"]["sha256"], "deps release")
    dest = src / "tmp" / "rpl-inputs" / "deps"
    shutil.rmtree(dest, ignore_errors=True)
    bundle.extract(tar_path, dest)
    sums = bundle.verify_sums(dest)
    info = json.loads((dest / "build-info.json").read_text())
    if info["family"] != lock["deps"]["family"]:
        raise SystemExit(f"deps bundle is family {info['family']}, lock wants {lock['deps']['family']}")

    for target in TARGETS:
        install = install_dir(src, target)
        if install.exists():
            raise SystemExit(f"{install} exists; engine builds start from a clean tmp/")
        install.mkdir(parents=True)
        bundle.copy_tree(dest / target, install)
        bundle.relocate_in(install, info["relocated_files"][target], install)

    complete = src / "tmp" / "complete"
    complete.mkdir(parents=True, exist_ok=True)
    for marker in (dest / "done-markers").iterdir():
        shutil.copy2(marker, complete / marker.name)
    return dest, sums, info


def check_sdk(src, lock, sdk_tar, work):
    """Official (or nightly) SDK: bind it to the tag or commit and take the
    compiled common scripts.

    Returns the compiled scripts, the stale ones, the license file, the SDK's
    vc_version.py and the Python sources the SDK ships that git does not track
    (generated by setup.py, e.g. 8.6's renpy/styledata/stylesets.py); the
    engine build checks after its own generation that it produced each of them
    identically, and bundles them.
    """

    verify_tarball(sdk_tar, lock["renpy_sdk"]["sha256"], "Ren'Py SDK")
    dest = work / "sdk"
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    with tarfile.open(sdk_tar) as tar:
        members = [m for m in tar.getmembers()
                   if (re.match(r"^[^/]+/renpy/", m.name) or re.match(r"^[^/]+/LICENSE\.txt$", m.name))
                   and (m.isfile() or m.isdir())]
        tar.extractall(dest, members=members, filter="data")
    sdk_root = next(dest.iterdir())
    sdk_renpy = sdk_root / "renpy"
    tag_renpy = src / "renpy" / "renpy"

    version = renpy_version(lock)
    vc = (sdk_renpy / "vc_version.py").read_text()
    # 7.8/8.1+ write the full version; 7.5/8.0 only the build number.
    full = re.search(r"^version = u?['\"]" + re.escape(version) + r"['\"]$", vc, re.M)
    build = re.search(r"^vc_version = " + re.escape(version.rsplit(".", 1)[1]) + r"$", vc, re.M)
    if not (full or build):
        raise SystemExit(f"SDK vc_version.py does not name {version}")

    # Every Python source shipped in the SDK must equal the checkout's: the
    # tracked ones now, the generated ones once the build has generated them.
    tracked = set(output(["git", "-C", src / "renpy", "ls-files", "renpy"]).splitlines())
    differing, generated = [], {}
    for py in sorted(sdk_renpy.rglob("*.py")):
        rel = py.relative_to(sdk_renpy)
        if rel.name == "vc_version.py":
            continue
        tag = tag_renpy / rel
        if f"renpy/{rel.as_posix()}" not in tracked:
            generated[str(rel)] = py
        elif bundle.sha256(tag) != bundle.sha256(py):
            differing.append(str(rel))
    if differing:
        raise SystemExit(f"SDK renpy/ Python sources differ from {version}: {differing[:20]}")

    magic = re.search(rb'^RPYC_MAGIC\s*=\s*b"([^"]*)"', (tag_renpy / "script.py").read_bytes(), re.M)
    compiled = {}
    stale = []
    for path in sorted(list(sdk_renpy.rglob("*.rpyc")) + list(sdk_renpy.rglob("*.rpymc"))):
        rel = path.relative_to(sdk_renpy)
        stem = rel.with_suffix(path.suffix[:-1])
        sources = [tag_renpy / stem, tag_renpy / rel.parent / (rel.stem + "_ren.py")]
        source = next((s for s in sources if s.exists()), None)
        if source is None:
            raise SystemExit(f"SDK {rel} has no source in {version}")
        if magic:
            digest = hashlib.md5(source.read_bytes() + magic.group(1)).digest()
            if digest != path.read_bytes()[-16:]:
                stale.append(str(rel))
        compiled[str(rel)] = path

    # Official SDKs ship LICENSE.txt; nightlies do not, and the lock then names
    # the Ren'Py file it is made from.
    license_file = sdk_root / "LICENSE.txt"
    if not license_file.exists():
        name = lock["renpy_sdk"].get("license")
        if not name:
            raise SystemExit("The SDK has no LICENSE.txt and the lock names no renpy_sdk.license")
        license_file = src / "renpy" / name
        if f"{name}" not in output(["git", "-C", src / "renpy", "ls-files", name]).splitlines():
            raise SystemExit(f"renpy_sdk.license {name} is not tracked by Ren'Py")
    return compiled, stale, license_file, sdk_renpy / "vc_version.py", generated


def install_live2d_header(src, lock, header):
    """Live2DCubismCore.h for renpy.gl2.live2dmodel (compile time only).

    Upstream's live2d task unpacks the Cubism SDK to {{install}}/cubism; its
    annotator then adds Core/include and sets CUBISM. The header is
    proprietary: it comes from a repository secret, is checked against the
    lock and never enters a bundle.
    """

    pin = lock["live2d_header"]
    if bundle.sha256(header) != pin["sha256"]:
        raise SystemExit(f"Live2DCubismCore.h sha256 does not match the lock ({pin['sdk']})")
    for target in TARGETS:
        include = install_dir(src, target) / "cubism" / "Core" / "include"
        include.mkdir(parents=True, exist_ok=True)
        shutil.copy2(header, include / "Live2DCubismCore.h")
    return {"sdk": pin["sdk"], "sha256": pin["sha256"],
            "abi": gates.live2d_abi(install_dir(src, "ios-arm64") / "cubism" / "Core" / "include")}


def unpack_upstream_renios(zip_path, lock, work):
    """Upstream's official iOS prebuilt archives, the baseline for system imports."""

    verify_tarball(zip_path, lock["upstream_renios"]["sha256"], "upstream renios")
    dest = work / "upstream-renios"
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    run(["unzip", "-q", zip_path, "renios/prototype/prebuilt/*", "-d", dest])
    return dest / "renios" / "prototype" / "prebuilt"


def upstream_references(prebuilt, target, fallbacks):
    archives = [p for p in sorted((prebuilt / ENGINE_FOLDER[target]).glob("*.a"))
                if p.name not in ("libSDL2_test.a", "libSDL3_test.a")]
    if not archives:
        raise SystemExit(f"upstream renios has no {ENGINE_FOLDER[target]} archives")
    return machos.external_references(archives, fallbacks=fallbacks)


def system_import_gate(ours, upstream, target, config):
    """The system import gate, reporting baseline archives read with llvm-nm."""

    fallbacks = []
    report = gates.system_imports_vs_upstream(ours, upstream_references(upstream, target, fallbacks),
                                              config["reviewed_system_imports"])
    if fallbacks:
        report["upstream_read_with_llvm_nm"] = fallbacks
    return report


def compile_python(hostpython, items, python_major, mtime):
    """items: [(source, destination, display path)].

    Optimized like upstream's Context.compile for Python 2 (-OO, see
    pybytecode.py). Python 3: unchecked-hash pyc. Python 2: pyo, whose header
    embeds the source mtime, so sources are set to SOURCE_DATE_EPOCH first.
    """

    if python_major == "2":
        for source, _, _ in items:
            os.utime(source, (mtime, mtime))
        script = (
            "import json, py_compile, sys\n"
            "for src, dst, dfile in json.load(sys.stdin):\n"
            "    py_compile.compile(src, cfile=dst, dfile=dfile, doraise=True)\n"
        )
        cmd = [str(hostpython), "-" + "O" * pybytecode.OPTIMIZE, "-c", script]
    else:
        script = (
            "import json, py_compile, sys\n"
            "for src, dst, dfile in json.load(sys.stdin):\n"
            "    py_compile.compile(src, cfile=dst, dfile=dfile, doraise=True, optimize=%d,\n"
            "                       invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)\n"
            % pybytecode.OPTIMIZE
        )
        cmd = [str(hostpython), "-c", script]
    payload = json.dumps([[str(a), str(b), c] for a, b, c in items])
    subprocess.run(cmd, input=payload, text=True, check=True)


def engine(args):
    started = time.time()
    config = families()
    version_cfg = config["versions"][args.version]
    src = args.src.resolve()
    out = args.out.resolve()
    lock = load_lock(src)

    check_branch(src, lock, args.version)
    toolchain = check_xcode(lock, args.trial)
    if lock["deps"]["family"] != version_cfg["family"]:
        raise SystemExit("lock and families.json disagree on the family")

    deps_dir, deps_sums, deps_info = unpack_deps(src, lock, args.deps, args.trial)
    sdl = unpack_sdl(args.sdl, lock, src / "tmp" / "rpl-inputs")
    sdk_compiled, sdk_stale, renpy_license, vc_version, sdk_generated = check_sdk(
        src, lock, args.sdk, src / "tmp" / "rpl-inputs")

    # A family that follows upstream master has no fixed recipe in
    # families.json: the deps release must have been built from this
    # checkout's recipe.
    family = config["families"][version_cfg["family"]]
    if family["recipe_sha256"] is None:
        computed = recipe.compute(src, family["deps_modules"])["recipe_sha256"]
        if computed != deps_info["recipe_sha256"] and not args.trial:
            raise SystemExit(f"deps recipe {deps_info['recipe_sha256']} != this checkout's {computed}; "
                             "rebuild the dependency layer first")

    live2d = install_live2d_header(src, lock, args.live2d_header)

    # Ren'Py's distribution build generates renpy/vc_version.py; upstream's
    # checkout has one. Without it, setup.py's "import renpy" derives the
    # version from the git branch, which fails for a detached 7.x checkout.
    # The SDK's copy (checked against the tag above) is untracked here, so the
    # pristine renpy/ bundle, built from git's file list, is unaffected.
    shutil.copy2(vc_version, src / "renpy" / "renpy" / "vc_version.py")

    series.apply(src)
    run_tasks(src, lock, version_cfg["python"], version_cfg["engine_modules"])

    # Sources Ren'Py generates (not tracked) must come out as the SDK has them.
    for rel, sdk_file in sdk_generated.items():
        ours = src / "renpy" / "renpy" / rel
        if not ours.exists() or bundle.sha256(ours) != bundle.sha256(sdk_file):
            gates.fail(f"generated renpy/{rel} differs from the SDK's (or was not generated)")

    # The dependency archives the engine was built and is linked against are
    # exactly the released ones.
    for target in TARGETS:
        for name, digest in deps_sums.items():
            if name.startswith(f"{target}/lib/") and name.endswith(".a"):
                built = install_dir(src, target) / "lib" / Path(name).name
                if bundle.sha256(built) != digest:
                    gates.fail(f"{built} changed during the engine build")

    if out.exists():
        raise SystemExit(f"{out} exists; refusing to mix with an earlier bundle")
    out.mkdir(parents=True)

    pythonver = version_cfg["pythonver"]
    engine_archives = [f"lib{pythonver}.a", "librenpy.a", "librenpython.a"]
    gate_report = {"entry_points": {}, "session_zone": {}, "deps_sha256": "pass", "links": {}, "platforms": {}}
    archives_info = {}

    for target in TARGETS:
        folder = out / "lib" / ENGINE_FOLDER[target]
        folder.mkdir(parents=True)
        for name in engine_archives:
            shutil.copy2(install_dir(src, target) / "lib" / name, folder / name)
        for name, digest in deps_sums.items():
            if name.startswith(f"{target}/lib/") and name.endswith(".a"):
                shutil.copy2(deps_dir / name, folder / Path(name).name)
                if bundle.sha256(folder / Path(name).name) != digest:
                    gates.fail(f"copied {name} does not match deps SHA256SUMS")

        engine_paths = [folder / n for n in engine_archives]
        archives_info[ENGINE_FOLDER[target]] = archive_report(target, engine_paths)
        gate_report["platforms"][target] = "pass"

        librenpython = folder / "librenpython.a"
        gate_report["entry_points"][target] = gates.entry_points(librenpython, version_cfg["entry_points"])
        if version_cfg["python"] == "3":
            gate_report["session_zone"][target] = gates.session_zone(librenpython)

        all_archives = sorted(folder.glob("*.a"))
        externals = sorted((deps_dir / "link-check" / target).glob("*.a")) + [sdl[target]]
        gates.link(target, force_load=engine_paths, archives=all_archives + externals,
                   frameworks_dir=install_dir(src, target), frameworks=LINK_FRAMEWORKS,
                   libraries=LINK_LIBRARIES, entry="launcher_main", log_dir=src / "tmp" / "rpl-logs")
        gate_report["links"][target] = {"force_loaded": engine_archives, "entry": "launcher_main",
                                        "result": "pass"}

    upstream = unpack_upstream_renios(args.upstream_renios, lock, src / "tmp" / "rpl-inputs")
    gate_report["system_imports"] = {}
    for target in TARGETS:
        ours = machos.external_references(sorted((out / "lib" / ENGINE_FOLDER[target]).glob("*.a"))
                                          + sorted((deps_dir / "link-check" / target).glob("*.a")) + [sdl[target]])
        gate_report["system_imports"][target] = system_import_gate(ours, upstream, target, config)

    hostpython = install_dir(src, "ios-arm64") / "bin" / f"hostpython{version_cfg['python']}"

    # Python standard library (pythonlib task output), recompiled from its
    # sources at optimize=2 with deterministic headers (pybytecode.py). The
    # roots are where the pythonlib task finds modules: the target install's
    # lib (site-packages included), pytmp (pyjnius, pyobjus, steam), source/
    # (8.5's brotli), the committed steamapi.py and runtime/ (site.py,
    # sitecustomize.py and sysconfig.py are compiled from runtime/ files of
    # another name).
    stdlib = src / "renpy" / "lib" / pythonver
    bundle.copy_tree(stdlib, out / "python" / "lib" / pythonver)
    stdlib_report = pybytecode.recompile_stdlib(
        hostpython, version_cfg["python"], out / "python" / "lib" / pythonver, pythonver,
        source_roots=[install_dir(src, "ios-arm64") / "lib" / pythonver, src / "tmp" / f"py{version_cfg['python']}",
                      src / "source", src / "steamapi", src / "runtime"],
        extra_sources=sorted((src / "runtime").glob("*.py")))
    log(f"standard library: {stdlib_report['recompiled']} modules recompiled, "
        f"{stdlib_report['bytes_before']} -> {stdlib_report['bytes_after']} bytes")

    # Ren'Py: pristine tag sources and the generated sources the SDK ships;
    # .py compiled, SDK-compiled scripts added.
    tracked = output(["git", "-C", src / "renpy", "ls-files", "renpy"]).splitlines()
    bytecode = ".pyo" if version_cfg["python"] == "2" else ".pyc"
    py_items, copied, skipped = [], [], []
    for rel in tracked:
        path = src / "renpy" / rel
        dest = out / rel
        if rel.endswith(".py"):
            dest = dest.with_suffix(bytecode)
            py_items.append((path, dest, rel))
        elif Path(rel).suffix in (".pyx", ".pxd", ".pxi", ".pyi") or Path(rel).name in ("py.typed",):
            skipped.append(rel)
            continue
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            copied.append(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
    for name in ("helper_tool.rpy", "main.py"):
        if list((out / "renpy").rglob(name)):
            gates.fail(f"renpy/ must be pristine but contains {name}")
    for rel in sorted(sdk_generated):
        path = src / "renpy" / "renpy" / rel
        dest = (out / "renpy" / rel).with_suffix(bytecode)
        dest.parent.mkdir(parents=True, exist_ok=True)
        py_items.append((path, dest, f"renpy/{rel}"))
    # vc_version.py is generated by Ren'Py's distribution build, not tracked
    # in git; without it Ren'Py guesses its version. Use the SDK's copy, which
    # check_sdk() verified names this version.
    py_items.append((vc_version, out / "renpy" / ("vc_version" + bytecode), "renpy/vc_version.py"))
    compile_python(hostpython, py_items, version_cfg["python"], commit_time(src))
    changed_sources = output(["git", "-C", src / "renpy", "diff", "--name-only", "--", "renpy"]).splitlines()
    source_only_scripts = bundle.copy_compiled_scripts(sdk_compiled, out / "renpy", changed_sources)

    if list(out.rglob("Live2DCubismCore.h")):
        gates.fail("the proprietary Live2DCubismCore.h must not be bundled")

    py_sources = {r for r in tracked if r.endswith(".py")} | {f"renpy/{r}" for r in sdk_generated}
    pycs = {str(p.relative_to(out)) for p in (out / "renpy").rglob("*" + bytecode)}
    if {r[:-3] + bytecode for r in py_sources} | {"renpy/vc_version" + bytecode} != pycs:
        gates.fail(f"bytecode reconciliation: {len(pycs)} {bytecode} for {len(py_sources)} .py")
    gate_report["pyc"] = {"py_in_tag": len(py_sources) - len(sdk_generated), "generated": sorted(sdk_generated),
                          "pyc": len(pycs), "suffix": bytecode,
                          "note": "bytecode = tag .py + generated .py the SDK ships + vc_version.py from the SDK",
                          "result": "pass"}

    licenses = out / "LICENSES"
    bundle.copy_tree(deps_dir / "LICENSES", licenses)
    python_src = arch_build_dir(src, f"python{version_cfg['python']}", version_cfg["python"])
    python_license = next(python_src.glob("Python-*/LICENSE"))
    shutil.copy2(python_license, licenses / "python-LICENSE")
    # The Ren'Py repository has no license file; the official SDK ships it
    # (nightlies: the Ren'Py source it is made from, see check_sdk).
    shutil.copy2(renpy_license, licenses / "renpy-LICENSE.txt")
    pyobjus = arch_build_dir(src, "pyobjus", version_cfg["python"], required=False) / "pyobjus" / "LICENSE"
    if pyobjus.exists():
        shutil.copy2(pyobjus, licenses / "pyobjus-LICENSE")

    info = {
        "schema": 1,
        "kind": "engine",
        "engine": args.version,
        "family": lock["deps"]["family"],
        "renpy_build": {"branch_commit": git_head(src), "tag": lock.get("renpy_build_tag"),
                        "ref": lock.get("renpy_build_ref"), "base_commit": lock["renpy_build_commit"]},
        "renpy": {"tag": lock.get("renpy_tag"), "version": renpy_version(lock), "commit": git_head(src / "renpy")},
        "prerelease": lock.get("prerelease"),
        "pygame_sdl2": ({"tag": lock["pygame_sdl2_tag"], "commit": git_head(src / "pygame_sdl2")}
                        if lock.get("pygame_sdl2_commit") else None),
        "tooling_commit": git_head(TOOLING),
        "python": lock["python"],
        "deps": {"release": lock["deps"]["release"], "sha256": lock["deps"]["sha256"],
                 "tooling_commit": deps_info["tooling_commit"]},
        "sdl": sdl_layer(lock),
        "renpy_sdk": {**lock["renpy_sdk"], "compiled_scripts": len(sdk_compiled) - len(source_only_scripts),
                      "patched_source_scripts": source_only_scripts,
                      "stale_in_sdk": sdk_stale, "license_from": str(renpy_license.name)},
        "renpy_uv_lock": lock.get("renpy_uv_lock"),
        "downloads": tars_info(src),
        "patches_sha256": series.digest(src),
        "patches": series.describe(src),
        "toolchain": toolchain,
        "compile_flags": {t: compile_flags(src, t, version_cfg) for t in TARGETS},
        "archives": archives_info,
        "renpy_files": {"compiled": len(py_items), "copied": len(copied), "excluded": skipped},
        "bytecode": {"optimize": pybytecode.OPTIMIZE,
                     "headers": "unchecked-hash" if version_cfg["python"] == "3"
                                else f"mtime {pybytecode.PY2_PYO_MTIME} (stdlib), SOURCE_DATE_EPOCH (renpy)",
                     "stdlib": stdlib_report},
        "live2d_header": live2d,
        "site_packages": site_packages(install_dir(src, "ios-arm64") / "lib" / pythonver / "site-packages"),
        "gates": gate_report,
        "source_date_epoch": commit_time(src),
        "build_seconds": int(time.time() - started),
    }
    finish(out, info, args.trial)


def arch_build_dir(src, module, python, required=True):
    """tmp/build/<module>.ios-arm64[-py<N>]: 8.6 dropped the Python suffix."""

    for name in (f"{module}.ios-arm64-py{python}", f"{module}.ios-arm64"):
        path = src / "tmp" / "build" / name
        if path.is_dir():
            return path
    if required:
        raise SystemExit(f"no build directory for {module} (ios-arm64)")
    return src / "tmp" / "build" / f"{module}.ios-arm64"


def site_packages(path):
    """name -> version of what upstream's pip task installed (certifi is unpinned upstream)."""

    found = {}
    for info in sorted(path.glob("*.dist-info")) + sorted(path.glob("*.egg-info")):
        name, _, version = info.name.rsplit(".", 1)[0].partition("-")
        found[name] = version
    return found


def compile_flags(src, target, version_cfg):
    """CFLAGS the renpython task used, read back from the toolchain module."""

    sdk, triple = xcode_toolchain.IOS_TARGETS[ARCH[target]]
    return {"target": triple, "sdk": xcode_toolchain.xcrun("--sdk", sdk, "--show-sdk-version"),
            "defines": ["-DIOS", '-DPLATFORM="ios"', f'-DARCH="{ARCH[target]}"',
                        f'-DPYTHONVER="{version_cfg["pythonver"]}"',
                        f'-DPYCVER="{version_cfg["pythonver"].replace("python", "").replace(".", "")}"']}


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(required=True)

    p = sub.add_parser("prepare")
    p.add_argument("--src", type=Path, required=True)
    p.set_defaults(func=prepare)

    p = sub.add_parser("deps")
    p.add_argument("--family", required=True)
    p.add_argument("--src", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--sdl", "--sdl2", type=Path, required=True, help="global SDL layer release tarball")
    p.add_argument("--previous", type=Path)
    p.add_argument("--previous-tag")
    p.add_argument("--source-version")
    p.add_argument("--upstream-renios", type=Path, required=True)
    p.set_defaults(func=deps)

    p = sub.add_parser("engine")
    p.add_argument("--version", required=True)
    p.add_argument("--src", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--deps", type=Path, required=True)
    p.add_argument("--sdl", "--sdl2", type=Path, required=True, help="global SDL layer release tarball")
    p.add_argument("--sdk", type=Path, required=True)
    p.add_argument("--live2d-header", type=Path, required=True)
    p.add_argument("--upstream-renios", type=Path, required=True)
    p.set_defaults(func=engine)

    for name, p in sub.choices.items():
        if name != "prepare":
            p.add_argument("--trial", action="store_true",
                           help="local trial build: tolerate a different Xcode and recipe; "
                                "the bundle is marked trial and cannot be released")
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
