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

SRC is a checkout of a renpylinter/<version> branch. The sdl2 release tarball
is passed with --sdl2 to both build commands.
"""

import argparse
import hashlib
import json
import os
import re
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
import gates  # noqa: E402
import machos  # noqa: E402
import recipe  # noqa: E402
import series  # noqa: E402
import xcode_toolchain  # noqa: E402

TARGETS = ["ios-arm64", "ios-sim-arm64"]
ARCH = {"ios-arm64": "arm64", "ios-sim-arm64": "sim-arm64"}
ENGINE_FOLDER = {"ios-arm64": "release", "ios-sim-arm64": "debug"}
SDL2_ARCHIVE = {"ios-arm64": "release/libSDL2.a", "ios-sim-arm64": "debug/libSDL2.a"}

# Tasks whose outputs are not part of the dependency bundle: build tools,
# the MetalANGLE framework (global layer) and the SDK link.
UNPACKAGED_MODULES = {"nasm", "metalangle"}

# Modules built purely from renpy-build's own files.
FIRST_PARTY = {"toolchain": "source/mockrt.c"}

LICENSE_NAME = re.compile(r"^(COPYING|LICEN[CS]E|NOTICE|COPYRIGHT)([._-].*)?$", re.I)

# System frameworks and libraries an iOS app links for the Ren'Py runtime.
LINK_FRAMEWORKS = [
    "AudioToolbox", "AVFoundation", "CoreAudio", "CoreBluetooth", "CoreFoundation", "CoreGraphics",
    "CoreHaptics", "CoreMedia", "CoreMotion", "CoreVideo", "Foundation", "GameController",
    "IOKit", "Metal", "OpenGLES", "QuartzCore", "Security", "UIKit", "VideoToolbox", "MetalANGLE",
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
    if not (renpy / ".git").exists():
        run(["git", "init", "-q", renpy])
        run(["git", "-C", renpy, "fetch", "-q", "--depth", "1", "https://github.com/renpy/renpy.git",
             lock["renpy_commit"]])
        run(["git", "-C", renpy, "checkout", "-q", "FETCH_HEAD"])
    if git_head(renpy) != lock["renpy_commit"]:
        raise SystemExit(f"renpy/ is at {git_head(renpy)}, lock requires {lock['renpy_commit']}")

    if lock["host_python"] == "uv-project":
        run(["uv", "sync", "--project", renpy, "--frozen", "--no-install-project"])
    else:
        env = src / "tmp" / "rpl-env"
        if not env.exists():
            run(["uv", "venv", "-q", "--python", lock["host_python"], env])
            run(["uv", "pip", "install", "-q", "--python", env / "bin" / "python", "-r", src / "requirements.txt"])

    tools = src / "tmp" / "rpl-tools"
    if not (tools / "bin" / "python").exists():
        run(["uv", "venv", "-q", "--python", "3.12", tools])
    run(["uv", "pip", "install", "-q", "--python", tools / "bin" / "python", "-r", HERE / "build-tools.txt"])
    build_autotools(tools)


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
        run(["curl", "-sfL", "--retry", "4", "-o", archive, pin["url"]])
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


def task_env(src, lock):
    env = {k: v for k, v in os.environ.items() if k not in HOST_LEAKS}
    # Like `uv run` / an activated venv: the build environment's scripts
    # (cython, ...) come first, then the pinned build tools.
    env["PATH"] = f"{build_python(src, lock).parent}:{tool_path(src)}:{env['PATH']}"
    env["VIRTUAL_ENV"] = str(build_python(src, lock).parent.parent)
    env["LIBTOOLIZE"] = "glibtoolize"
    env.setdefault("CCACHE_COMPILERCHECK", "content")
    env.setdefault("PYTHONHASHSEED", "0")
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


def unpack_sdl2(tar_path, lock, work):
    verify_tarball(tar_path, lock["sdl2"]["sha256"], "sdl2 release")
    dest = work / "sdl2"
    shutil.rmtree(dest, ignore_errors=True)
    bundle.extract(tar_path, dest)
    bundle.verify_sums(dest)
    return {t: dest / SDL2_ARCHIVE[t] for t in TARGETS}


def check_branch(src, lock, version):
    if lock["engine"] != version:
        raise SystemExit(f"{src} is the {lock['engine']} branch, not {version}")
    tag_commit = output(["git", "-C", src, "rev-parse", lock["renpy_build_tag"] + "^{commit}"])
    if tag_commit != lock["renpy_build_commit"]:
        raise SystemExit(f"{lock['renpy_build_tag']} is {tag_commit}, lock says {lock['renpy_build_commit']}")
    merge_base = output(["git", "-C", src, "merge-base", "HEAD", tag_commit])
    if merge_base != tag_commit:
        raise SystemExit(f"branch is not based on {lock['renpy_build_tag']}")
    if series.digest(src) != lock["patches_sha256"]:
        raise SystemExit(f"patches_sha256 {series.digest(src)} does not match the lock")
    # Root patches applied by an earlier (resumed) run are the only allowed edits.
    dirty = {line[3:] for line in output(["git", "-C", src, "status", "--porcelain",
                                          "--untracked-files=no"]).splitlines()}
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
        hits = []
        for root in roots:
            if not root.is_dir():
                continue
            for path in sorted(root.glob("*")) + sorted(root.glob("*/*")) + sorted(root.glob("*/docs/*")):
                if path.is_file() and LICENSE_NAME.match(path.name):
                    hits.append(path)
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


def sdl2_tree_info(src):
    task = (src / "tasks" / "sdl2.py").read_text()
    version = re.search(r'^version\s*=\s*"([^"]+)"', task, re.M).group(1)
    patch_dir = src / "patches" / f"SDL2-{version}"
    return {
        "version": version,
        "source_sha256": bundle.sha256(src / "source" / f"SDL2-{version}.tar.gz"),
        "patches": {p.name: bundle.sha256(p) for p in sorted(patch_dir.glob("*")) if p.is_file()},
        "note": "Built in the dependency tree for SDL2_image and pygame_sdl2 headers only; "
                "libSDL2.a is not part of this bundle (global layer: sdl2-ios release).",
    }


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
    recipe_matches = family["recipe_sha256"] == computed["recipe_sha256"]
    if not recipe_matches and source_version == baseline["version"] and not args.trial:
        raise SystemExit(f"families.json recipe_sha256 for {args.family} is {family['recipe_sha256']}, "
                         f"checkout computes {computed['recipe_sha256']}")

    series.apply(src)
    run_tasks(src, lock, baseline_python(config, baseline["version"]), family["deps_modules"])

    if out.exists():
        raise SystemExit(f"{out} exists; refusing to mix with an earlier bundle")
    out.mkdir(parents=True)

    sdl2 = unpack_sdl2(args.sdl2, lock, src / "tmp" / "rpl-inputs")
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
        externals = sorted((out / "link-check" / target).glob("*.a")) + [sdl2[target]]
        for archive in archives:
            gates.link(target, force_load=[archive], archives=archives + externals,
                       frameworks_dir=install_dir(src, target), frameworks=LINK_FRAMEWORKS,
                       libraries=LINK_LIBRARIES, log_dir=src / "tmp" / "rpl-logs")
        gate_report["links"][target] = {"force_loaded": [a.name for a in archives], "result": "pass"}

        lines = export_lines(out / target)
        (out / "exports").mkdir(exist_ok=True)
        (out / "exports" / f"{target}.txt").write_text("\n".join(lines) + "\n")

    if args.previous:
        prev_dir = src / "tmp" / "rpl-inputs" / "previous"
        shutil.rmtree(prev_dir, ignore_errors=True)
        bundle.extract(args.previous, prev_dir)
        for target in TARGETS:
            previous = (prev_dir / "exports" / f"{target}.txt").read_text().splitlines()
            current = (out / "exports" / f"{target}.txt").read_text().splitlines()
            removed = gates.exports_diff(previous, current)
            if removed:
                gates.fail(f"{target}: {len(removed)} exported symbols removed since previous release: {removed[:20]}")
            gate_report["exports"][target] = {"previous": args.previous_tag, "removed": 0,
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
        "sdl2_build_tree": sdl2_tree_info(src),
        "sdl2_link_check": {"release": lock["sdl2"]["release"], "sha256": lock["sdl2"]["sha256"]},
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


def baseline_python(config, version):
    return config["versions"][version]["python"]


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
    """Official SDK: bind it to the tag and take the compiled common scripts."""

    verify_tarball(sdk_tar, lock["renpy_sdk"]["sha256"], "Ren'Py SDK")
    dest = work / "sdk"
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    with tarfile.open(sdk_tar) as tar:
        members = [m for m in tar.getmembers()
                   if re.match(r"^[^/]+/renpy/", m.name) and (m.isfile() or m.isdir())]
        tar.extractall(dest, members=members, filter="data")
    sdk_renpy = next(dest.iterdir()) / "renpy"
    tag_renpy = src / "renpy" / "renpy"

    vc = (sdk_renpy / "vc_version.py").read_text()
    if f"version = '{lock['renpy_tag']}'" not in vc:
        raise SystemExit(f"SDK vc_version.py does not name {lock['renpy_tag']}")

    # Every Python source shipped in the SDK must equal the tag's.
    differing = []
    for py in sorted(sdk_renpy.rglob("*.py")):
        rel = py.relative_to(sdk_renpy)
        if rel.name == "vc_version.py":
            continue
        tag = tag_renpy / rel
        if not tag.exists() or bundle.sha256(tag) != bundle.sha256(py):
            differing.append(str(rel))
    if differing:
        raise SystemExit(f"SDK renpy/ Python sources differ from tag {lock['renpy_tag']}: {differing[:20]}")

    magic = re.search(rb'^RPYC_MAGIC\s*=\s*b"([^"]*)"', (tag_renpy / "script.py").read_bytes(), re.M)
    compiled = {}
    stale = []
    for path in sorted(list(sdk_renpy.rglob("*.rpyc")) + list(sdk_renpy.rglob("*.rpymc"))):
        rel = path.relative_to(sdk_renpy)
        stem = rel.with_suffix(path.suffix[:-1])
        sources = [tag_renpy / stem, tag_renpy / rel.parent / (rel.stem + "_ren.py")]
        source = next((s for s in sources if s.exists()), None)
        if source is None:
            raise SystemExit(f"SDK {rel} has no source in tag {lock['renpy_tag']}")
        if magic:
            digest = hashlib.md5(source.read_bytes() + magic.group(1)).digest()
            if digest != path.read_bytes()[-16:]:
                stale.append(str(rel))
        compiled[str(rel)] = path
    return compiled, stale


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


def compile_python(hostpython, items):
    """items: [(source, destination, display path)] compiled to unchecked-hash pyc."""

    script = (
        "import json, py_compile, sys\n"
        "for src, dst, dfile in json.load(sys.stdin):\n"
        "    py_compile.compile(src, cfile=dst, dfile=dfile, doraise=True,\n"
        "                       invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)\n"
    )
    payload = json.dumps([[str(a), str(b), c] for a, b, c in items])
    subprocess.run([str(hostpython), "-c", script], input=payload, text=True, check=True)


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
    sdl2 = unpack_sdl2(args.sdl2, lock, src / "tmp" / "rpl-inputs")
    sdk_compiled, sdk_stale = check_sdk(src, lock, args.sdk, src / "tmp" / "rpl-inputs")

    live2d = install_live2d_header(src, lock, args.live2d_header)

    series.apply(src)
    run_tasks(src, lock, version_cfg["python"], version_cfg["engine_modules"])

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
        externals = sorted((deps_dir / "link-check" / target).glob("*.a")) + [sdl2[target]]
        gates.link(target, force_load=engine_paths, archives=all_archives + externals,
                   frameworks_dir=install_dir(src, target), frameworks=LINK_FRAMEWORKS,
                   libraries=LINK_LIBRARIES, entry="launcher_main", log_dir=src / "tmp" / "rpl-logs")
        gate_report["links"][target] = {"force_loaded": engine_archives, "entry": "launcher_main",
                                        "result": "pass"}

    # Python standard library (pythonlib task output).
    stdlib = src / "renpy" / "lib" / pythonver
    bundle.copy_tree(stdlib, out / "python" / "lib" / pythonver)

    # Ren'Py: pristine tag sources; .py compiled, SDK-compiled scripts added.
    hostpython = install_dir(src, "ios-arm64") / "bin" / f"hostpython{version_cfg['python']}"
    tracked = output(["git", "-C", src / "renpy", "ls-files", "renpy"]).splitlines()
    py_items, copied, skipped = [], [], []
    for rel in tracked:
        path = src / "renpy" / rel
        dest = out / rel
        if rel.endswith(".py"):
            dest = dest.with_suffix(".pyc")
            py_items.append((path, dest, rel))
        elif Path(rel).suffix in (".pyx", ".pxd", ".pyi") or Path(rel).name in ("py.typed",):
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
    compile_python(hostpython, py_items)
    for rel, path in sdk_compiled.items():
        shutil.copy2(path, out / "renpy" / rel)

    if list(out.rglob("Live2DCubismCore.h")):
        gates.fail("the proprietary Live2DCubismCore.h must not be bundled")

    py_sources = {r for r in tracked if r.endswith(".py")}
    pycs = {str(p.relative_to(out)) for p in (out / "renpy").rglob("*.pyc")}
    if {r[:-3] + ".pyc" for r in py_sources} != pycs:
        gates.fail(f"pyc reconciliation: {len(pycs)} pyc for {len(py_sources)} .py")
    gate_report["pyc"] = {"py_in_tag": len(py_sources), "pyc": len(pycs), "result": "pass"}

    licenses = out / "LICENSES"
    bundle.copy_tree(deps_dir / "LICENSES", licenses)
    python_src = next((src / "tmp" / "build").glob(f"python{version_cfg['python']}.ios-arm64-py*"))
    python_license = next(python_src.glob("Python-*/LICENSE"))
    shutil.copy2(python_license, licenses / "python-LICENSE")
    shutil.copy2(src / "renpy" / "LICENSE.txt", licenses / "renpy-LICENSE.txt")
    pyobjus = src / "tmp" / "build" / f"pyobjus.ios-arm64-py{version_cfg['python']}" / "pyobjus" / "LICENSE"
    if pyobjus.exists():
        shutil.copy2(pyobjus, licenses / "pyobjus-LICENSE")

    info = {
        "schema": 1,
        "kind": "engine",
        "engine": args.version,
        "family": lock["deps"]["family"],
        "renpy_build": {"branch_commit": git_head(src), "tag": lock["renpy_build_tag"],
                        "tag_commit": lock["renpy_build_commit"]},
        "renpy": {"tag": lock["renpy_tag"], "commit": git_head(src / "renpy")},
        "tooling_commit": git_head(TOOLING),
        "python": lock["python"],
        "deps": {"release": lock["deps"]["release"], "sha256": lock["deps"]["sha256"],
                 "tooling_commit": deps_info["tooling_commit"]},
        "sdl2": lock["sdl2"],
        "renpy_sdk": {**lock["renpy_sdk"], "compiled_scripts": len(sdk_compiled),
                      "stale_in_sdk": sdk_stale},
        "patches_sha256": series.digest(src),
        "patches": series.describe(src),
        "toolchain": toolchain,
        "compile_flags": {t: compile_flags(src, t, version_cfg) for t in TARGETS},
        "archives": archives_info,
        "renpy_files": {"compiled": len(py_items), "copied": len(copied), "excluded": skipped},
        "live2d_header": live2d,
        "gates": gate_report,
        "source_date_epoch": commit_time(src),
        "build_seconds": int(time.time() - started),
    }
    finish(out, info, args.trial)


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
    p.add_argument("--sdl2", type=Path, required=True)
    p.add_argument("--previous", type=Path)
    p.add_argument("--previous-tag")
    p.add_argument("--source-version")
    p.set_defaults(func=deps)

    p = sub.add_parser("engine")
    p.add_argument("--version", required=True)
    p.add_argument("--src", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--deps", type=Path, required=True)
    p.add_argument("--sdl2", type=Path, required=True)
    p.add_argument("--sdk", type=Path, required=True)
    p.add_argument("--live2d-header", type=Path, required=True)
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
