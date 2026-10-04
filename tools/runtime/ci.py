#!/usr/bin/env python3
"""GitHub Actions glue for deps-ios.yml and engine-ios.yml.

Build steps run on macOS from the tooling checkout (cwd) with the engine
branch in ./src. Release steps run on Linux with only tools/runtime checked
out and the bundle artifact in ./dist.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bundle  # noqa: E402
import driver  # noqa: E402

ROOT = Path.cwd()
SRC = ROOT / "src"
INPUTS = ROOT / "inputs"
DIST = ROOT / "dist"


def gh(*args):
    return subprocess.check_output(["gh", *args], text=True).strip()


def lock():
    return json.loads((SRC / "renpylinter.lock.json").read_text())


def families():
    return json.loads((HERE.parent.parent / "families.json").read_text())


def env(name):
    value = os.environ.get(name, "")
    return value


def releases():
    return gh("api", "--paginate", f"repos/{env('GH_REPO')}/releases", "--jq", ".[].tag_name").splitlines()


def tags(prefix):
    out = gh("api", f"repos/{env('GH_REPO')}/git/matching-refs/tags/{prefix}", "--jq", ".[].ref")
    return [r.removeprefix("refs/tags/") for r in out.splitlines()]


def revisions(prefix):
    pattern = re.compile(re.escape(prefix) + r"(\d+)$")
    names = set(releases()) | set(tags(prefix))
    return sorted(int(m.group(1)) for n in names if (m := pattern.match(n)))


def download_release_asset(tag, pattern, dest):
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(["gh", "release", "download", tag, "-p", pattern, "-D", str(dest), "--clobber"], check=True)
    return dest / pattern


# deps ##########################################################################

def deps_branch():
    config = families()
    family = config["families"][env("FAMILY")]
    version = env("SOURCE_VERSION") or family["baseline"]["version"]
    if version not in family["members"]:
        raise SystemExit(f"{version} is not a member of {env('FAMILY')}")
    return version, f"renpylinter/{version}"


def deps_config(args):
    version, branch = deps_branch()
    suffix = "" if not env("SOURCE_VERSION") else f"-from-{version}"
    print(f"branch={branch}")
    print(f"bundle=deps-{env('FAMILY')}{suffix}-ios")


def fetch_url(url, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    return dest


def sdl_input():
    """Download the global SDL layer the lock names; return its path."""

    layer = driver.sdl_layer(lock())
    return download_release_asset(layer["release"], layer["asset"], INPUTS / "sdl")


def sdl_path():
    return INPUTS / "sdl" / driver.sdl_layer(lock())["asset"]


def base_ref(args):
    """The upstream renpy-build ref the branch is based on, for `git fetch`."""

    l = lock()
    if l.get("renpy_build_tag"):
        print(f"refs/tags/{l['renpy_build_tag']}:refs/tags/{l['renpy_build_tag']}")
    else:
        print(l["renpy_build_commit"])


def upstream_renios_path():
    return INPUTS / "upstream" / Path(lock()["upstream_renios"]["url"]).name


def deps_inputs(args):
    l = lock()
    sdl_input()
    fetch_url(l["upstream_renios"]["url"], upstream_renios_path())
    prefix = f"deps-{env('FAMILY')}-r"
    previous = revisions(prefix)
    if previous:
        tag = prefix + str(previous[-1])
        download_release_asset(tag, f"deps-{env('FAMILY')}-ios.tar.gz", INPUTS / "previous")
        (INPUTS / "previous" / "TAG").write_text(tag)


def deps_build(args):
    version, _ = deps_branch()
    out = DIST / "bundle"
    cmd = [sys.executable, "-u", str(HERE / "driver.py"), "deps", "--family", env("FAMILY"),
           "--src", str(SRC), "--out", str(out), "--sdl", str(sdl_path()),
           "--upstream-renios", str(upstream_renios_path())]
    if env("SOURCE_VERSION"):
        cmd += ["--source-version", version]
    previous = INPUTS / "previous" / f"deps-{env('FAMILY')}-ios.tar.gz"
    if previous.exists():
        cmd += ["--previous", str(previous), "--previous-tag", (INPUTS / "previous" / "TAG").read_text()]
    subprocess.run(cmd, check=True)
    pack(out, f"deps-{env('FAMILY')}-ios.tar.gz")


# engine ########################################################################

def engine_config(args):
    version = env("ENGINE_VERSION")
    if version not in families()["versions"]:
        raise SystemExit(f"Unknown engine version {version}")
    print(f"branch=renpylinter/{version}")
    print(f"bundle=renpy-runtime-{version}-ios")


def engine_inputs(args):
    l = lock()
    if not l["deps"].get("release"):
        raise SystemExit("renpylinter.lock.json has no deps release yet")
    download_release_asset(l["deps"]["release"], f"deps-{l['deps']['family']}-ios.tar.gz", INPUTS / "deps")
    sdl_input()
    fetch_url(l["renpy_sdk"]["url"], INPUTS / "sdk" / Path(l["renpy_sdk"]["url"]).name)
    fetch_url(l["upstream_renios"]["url"], upstream_renios_path())


def engine_build(args):
    l = lock()
    version = env("ENGINE_VERSION")
    out = DIST / "bundle"
    subprocess.run([sys.executable, "-u", str(HERE / "driver.py"), "engine", "--version", version,
                    "--src", str(SRC), "--out", str(out),
                    "--deps", str(INPUTS / "deps" / f"deps-{l['deps']['family']}-ios.tar.gz"),
                    "--sdl", str(sdl_path()),
                    "--sdk", str(INPUTS / "sdk" / Path(l["renpy_sdk"]["url"]).name),
                    "--live2d-header", str(INPUTS / "live2d" / "Live2DCubismCore.h"),
                    "--upstream-renios", str(upstream_renios_path())], check=True)
    pack(out, f"renpy-runtime-{version}-ios.tar.gz")


def pack(out, name):
    info = json.loads((out / "build-info.json").read_text())
    archive = DIST / name
    bundle.tarball(out, archive, info["source_date_epoch"])
    (DIST / (name + ".sha256")).write_text(f"{bundle.sha256(archive)}  {name}\n")
    shutil.copy2(out / "build-info.json", DIST / "build-info.json")
    shutil.copy2(out / "SHA256SUMS", DIST / "SHA256SUMS")
    shutil.rmtree(out)


# release #######################################################################

def release_prepare(args):
    archives = list(DIST.glob("*.tar.gz"))
    if len(archives) != 1:
        raise SystemExit(f"expected one tarball in dist/, found {archives}")
    archive = archives[0]
    digest, name = (DIST / (archive.name + ".sha256")).read_text().split()
    if name != archive.name or bundle.sha256(archive) != digest:
        raise SystemExit("tarball checksum mismatch")

    with tempfile.TemporaryDirectory() as tmp:
        bundle.extract(archive, tmp)
        bundle.verify_sums(tmp)
        info = json.loads((Path(tmp) / "build-info.json").read_text())
        if (Path(tmp) / "build-info.json").read_bytes() != (DIST / "build-info.json").read_bytes():
            raise SystemExit("build-info.json asset differs from the one in the tarball")

    if info.get("trial", True):
        raise SystemExit("trial bundles are never released")
    if not info["toolchain"].get("matches_lock"):
        raise SystemExit("bundle was not built with the locked Xcode")
    if info["kind"] != args.kind:
        raise SystemExit(f"bundle kind {info['kind']} != {args.kind}")
    if args.kind == "deps":
        if info["family"] != args.name:
            raise SystemExit("family mismatch")
        if info["source_version"] != info["baseline"]["version"]:
            raise SystemExit("verification bundles built from a non-baseline branch are not published")
        prefix = f"deps-{args.name}-r"
        commit = info["baseline"]["branch_commit"]
        title_what = f"{args.name} dependency layer"
    else:
        if info["engine"] != args.name:
            raise SystemExit("engine version mismatch")
        prefix = f"renpy-{args.name}-r"
        commit = info["renpy_build"]["branch_commit"]
        title_what = f"Ren'Py {args.name} runtime"

    for gate, result in info["gates"].items():
        print(f"gate {gate}: {json.dumps(result)[:200]}")

    tag = prefix + str((revisions(prefix) or [0])[-1] + 1)
    notes = render_notes(info, archive.name)
    Path("notes.md").write_text(notes)
    # Engines built from a Ren'Py nightly are published as prereleases.
    prerelease = bool(info.get("prerelease"))
    if prerelease:
        title_what += f" (nightly {info['prerelease']['build']})"
    with open(os.environ["GITHUB_ENV"], "a") as f:
        f.write(f"RELEASE_TAG={tag}\nRELEASE_COMMIT={commit}\nRELEASE_TITLE={tag}: {title_what}\n"
                f"RELEASE_ARCHIVE={archive}\nRELEASE_PRERELEASE={'1' if prerelease else ''}\n")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"tag={tag}\n")
    print(f"next release: {tag} at {commit}")


def render_notes(info, archive):
    lines = []
    tc = info["toolchain"]
    if info["kind"] == "deps":
        b = info["baseline"]
        tree = info.get("sdl_build_tree") or {"library": "SDL2", **info["sdl2_build_tree"]}
        link = info.get("sdl_link_check") or info["sdl2_link_check"]
        lines += [f"Dependency layer `{info['family']}` for RenPyLinter iOS.", "",
                  f"- Baseline: `{b['branch']}` @ `{b['branch_commit']}` "
                  f"(upstream `{b.get('renpy_build_tag') or b['renpy_build_commit']}`)",
                  f"- Recipe sha256: `{info['recipe_sha256']}`",
                  f"- Archives: {', '.join(sorted(info['archives']['ios-arm64']))}",
                  f"- {tree['library']} compiled in the build tree for headers: {tree['version']}; "
                  f"lib{tree['library']} comes from `{link['release']}`"]
    else:
        rb, rp = info["renpy_build"], info["renpy"]
        sdl = info.get("sdl") or {"library": "SDL2", **info["sdl2"]}
        lines += [f"Ren'Py {info['engine']} engine runtime for RenPyLinter iOS (Python {info['python']}).", ""]
        if info.get("prerelease"):
            pre = info["prerelease"]
            lines += [f"Built from Ren'Py {pre['channel']} `{pre['build']}` ({pre['page']}); "
                      "Ren'Py has not released this version.", ""]
        lines += [f"- renpy-build: `{rb.get('tag') or rb.get('ref')}` @ `{rb.get('tag_commit') or rb.get('base_commit')}` "
                  f"+ branch commit `{rb['branch_commit']}`",
                  f"- Ren'Py: `{rp.get('version') or rp['tag']}` @ `{rp['commit']}`",
                  f"- Dependency layer: `{info['deps']['release']}` (`{info['deps']['sha256']}`)",
                  f"- {sdl['library']}: `{sdl['release']}`"]
    lines += [f"- Targets: device arm64 and simulator arm64, minimum iOS {tc['minimum_ios']}",
              f"- Xcode {tc['xcode']} ({tc['xcode_build']}), SDKs {tc['sdks']}",
              f"- Tooling commit: `{info['tooling_commit']}`",
              f"- Patches sha256: `{info['patches_sha256']}`", "",
              "All release gates passed; results are in build-info.json. "
              f"Verify with `gh attestation verify {archive} -R ITJesse/renpy-build`.",
              "", f"Workflow: {os.environ.get('GITHUB_SERVER_URL', '')}/{env('GH_REPO')}/actions/runs/"
              f"{os.environ.get('GITHUB_RUN_ID', '')}"]
    return "\n".join(lines) + "\n"


def release_publish(args):
    tag, commit = env("RELEASE_TAG"), env("RELEASE_COMMIT")
    if tag in releases() or tag in tags(tag):
        raise SystemExit(f"{tag} already exists; releases are immutable")
    subprocess.run(["gh", "api", "--method", "POST", f"repos/{env('GH_REPO')}/git/refs",
                    "-f", f"ref=refs/tags/{tag}", "-f", f"sha={commit}"], check=True)
    archive = Path(env("RELEASE_ARCHIVE"))
    prerelease = ["--prerelease"] if env("RELEASE_PRERELEASE") else []
    subprocess.run(["gh", "release", "create", tag, "--verify-tag", "--target", commit, *prerelease,
                    "--title", env("RELEASE_TITLE"),
                    "--notes-file", "notes.md", str(archive), str(archive) + ".sha256",
                    str(DIST / "build-info.json"), str(DIST / "SHA256SUMS")], check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command")
    ap.add_argument("--kind")
    ap.add_argument("--name")
    args = ap.parse_args()
    {
        "base-ref": base_ref,
        "deps-config": deps_config, "deps-inputs": deps_inputs, "deps-build": deps_build,
        "engine-config": engine_config, "engine-inputs": engine_inputs, "engine-build": engine_build,
        "release-prepare": release_prepare, "release-publish": release_publish,
    }[args.command](args)


if __name__ == "__main__":
    main()
