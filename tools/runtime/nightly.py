#!/usr/bin/env python3
"""Move a nightly engine branch (renpylinter/8.6.0) to the newest Ren'Py nightly.

    nightly.py bump --src SRC [--build 8.6.0.YYMMDDNN+nightly...]
        Find the newest nightly of the branch's version (or the one given),
        rebase the branch's own commits onto the renpy-build commit that
        nightly was built from, re-lock the nightly's SDK, renios and Ren'Py
        commit, regenerate the pinned uv.lock and commit. Prints JSON:
        {"changed", "build", "commit", "family", "deps_current", "released"};
        "released" is whether an engine release already points at the
        resulting commit.

    nightly.py link-deps --src SRC --release TAG --tarball PATH
        Point the lock at a newly published dependency layer and commit.

SRC is a full-history checkout of the engine branch with a committer identity
configured. Nothing is pushed.
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bundle  # noqa: E402
import recipe  # noqa: E402
import series  # noqa: E402

NIGHTLY = "https://nightly.renpy.org/"
UPSTREAM_BUILD = "https://github.com/renpy/renpy-build.git"
UPSTREAM_RENPY = "https://github.com/renpy/renpy.git"
LOCK = "renpylinter.lock.json"

# Packages upstream's pip task installs (tasks/python3.py) whose versions the
# nightly SDK reveals: distribution -> (bytecode file in lib/pythonX.Y, names
# that hold the version). The pinned uv.lock uses these versions, so the iOS
# runtime ships what the nightly ships; the rest resolve as of the Ren'Py
# commit's time.
SDK_VERSIONS = {
    "requests": ("requests/__version__.pyc", ("__version__",)),
    "urllib3": ("urllib3/_version.pyc", ("__version__",)),
    "idna": ("idna/package_data.pyc", ("__version__",)),
    "charset-normalizer": ("charset_normalizer/version.pyc", ("__version__",)),
    "websockets": ("websockets/version.pyc", ("tag",)),
    "rsa": ("rsa/__init__.pyc", ("__version__",)),
    "pyasn1": ("pyasn1/__init__.pyc", ("__version__",)),
    "six": ("six.pyc", ("__version__",)),
    "pefile": ("pefile.pyc", ("__version__",)),
    "pysocks": ("socks.pyc", ("__version__",)),
}


def log(message):
    print(f"[nightly] {message}", file=sys.stderr, flush=True)


def git(src, *args, capture=True):
    if capture:
        return subprocess.check_output(["git", "-C", str(src), *args], text=True).strip()
    subprocess.run(["git", "-C", str(src), *args], check=True)
    return ""


def fetch_text(url):
    with urllib.request.urlopen(url) as r:
        return r.read().decode()


def download(url, dest):
    log(f"downloading {url}")
    with urllib.request.urlopen(url) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    return bundle.sha256(dest)


def quote(build):
    return urllib.parse.quote(build)


# Nightly discovery ############################################################

def nightly_page(build):
    """The commits and file names a nightly's page lists."""

    page = fetch_text(NIGHTLY + quote(build) + "/")
    renpy = re.search(r"github\.com/renpy/renpy/commits/([0-9a-f]{40})", page)
    build_commit = re.search(r"github\.com/renpy/renpy-build/commits/([0-9a-f]{40})", page)
    files = set(re.findall(r'href="(renpy-[^"/]+)"', page))
    if not (renpy and build_commit):
        raise SystemExit(f"nightly {build}: page names no Ren'Py or renpy-build commit")
    return {"build": build, "renpy_commit": renpy.group(1), "renpy_build_commit": build_commit.group(1),
            "files": files}


def newest_nightly(version):
    """Newest nightly of `version` that has both an SDK and an iOS build."""

    index = fetch_text(NIGHTLY)
    pattern = re.compile(r'href="(' + re.escape(version) + r'\.(\d{8})\+nightly(?:\.dirty)?)"')
    builds = sorted({(int(m.group(2)), m.group(1)) for m in pattern.finditer(index)}, reverse=True)
    for _, build in builds:
        info = nightly_page(build)
        if {f"renpy-{build}-sdk.tar.bz2", f"renpy-{build}-renios.zip"} <= info["files"]:
            return info
        log(f"{build} lacks an SDK tarball or renios zip; trying the previous nightly")
    raise SystemExit(f"no complete {version} nightly on {NIGHTLY}")


# Pinned uv.lock ###############################################################

# Reads the version constants from the bytecode without executing it: the
# code objects are only disassembled. The input is the nightly SDK whose
# sha256 is being locked from Ren'Py's own server.
VERSION_SCAN = r"""
import dis, json, marshal, re, sys
from pathlib import Path
base = Path(sys.argv[1])
found = {}
for dist, (rel, names) in json.loads(sys.argv[2]).items():
    path = base / rel
    if not path.exists():
        continue
    code = marshal.loads(path.read_bytes()[16:])
    last = None
    for ins in dis.get_instructions(code):
        if ins.opname == "LOAD_CONST":
            last = ins.argval
        elif ins.opname in ("STORE_NAME", "STORE_GLOBAL") and ins.argval in names:
            if isinstance(last, str) and re.fullmatch(r"\d+(\.\d+)+", last):
                found[dist] = last
                break
print(json.dumps(found))
"""


def sdk_versions(sdk_tar, python, work):
    """Versions of SDK_VERSIONS packages in the SDK's lib/<python>/."""

    lib = work / "sdk-lib"
    with tarfile.open(sdk_tar) as tar:
        members = [m for m in tar.getmembers()
                   if re.match(r"^[^/]+/lib/" + re.escape(python) + r"/", m.name) and m.isfile()
                   and any(m.name.endswith("/" + rel) for rel, _ in SDK_VERSIONS.values())]
        tar.extractall(lib, members=members, filter="data")
    roots = list(lib.glob(f"*/lib/{python}"))
    if not roots:
        return {}
    version = python.removeprefix("python")
    # The bytecode is read by the Python version that wrote it.
    out = subprocess.check_output(["uv", "run", "-q", "--no-project", "--python", version, "python", "-c",
                                   VERSION_SCAN, str(roots[0]), json.dumps(SDK_VERSIONS)], text=True)
    return json.loads(out)


def pinned_uv_lock(renpy_commit, sdk_tar, python, work):
    """uv.lock for the commit's pyproject.toml, as of the commit, with the
    versions the nightly SDK ships."""

    project = work / "uv-project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project / "renpy")], check=True)
    git(project / "renpy", "fetch", "-q", "--depth", "1", UPSTREAM_RENPY, renpy_commit, capture=False)
    when = int(git(project / "renpy", "log", "-1", "--format=%ct", "FETCH_HEAD"))
    git(project / "renpy", "checkout", "-q", "FETCH_HEAD", "--", "pyproject.toml", capture=False)
    shutil.copy2(project / "renpy" / "pyproject.toml", project / "pyproject.toml")
    exclude_newer = datetime.fromtimestamp(when, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pins = sdk_versions(sdk_tar, python, work)
    cmd = ["uv", "lock", "--project", str(project), "--exclude-newer", exclude_newer]
    for dist, version in sorted(pins.items()):
        cmd += ["--upgrade-package", f"{dist}=={version}"]
    subprocess.run(cmd, check=True)
    uv = subprocess.check_output(["uv", "--version"], text=True).split()[1]
    note = (f"Ren'Py does not track uv.lock (renpy 9fb435f). Resolved with uv {uv} for the commit's "
            f"pyproject.toml with --exclude-newer {exclude_newer} (the commit time)")
    if pins:
        note += ", with " + ", ".join(f"{d} {v}" for d, v in sorted(pins.items())) + \
                " pinned to the versions the nightly SDK ships"
    return (project / "uv.lock").read_bytes(), note + ". The pip task reads its versions from this file."


# bump #########################################################################

def bump(args):
    src = args.src.resolve()
    lock = json.loads((src / LOCK).read_text())
    if not lock.get("prerelease"):
        raise SystemExit(f"{src} is not a nightly engine branch")
    version = lock["engine"]
    info = nightly_page(args.build) if args.build else newest_nightly(version)
    build = info["build"]
    log(f"nightly {build}: renpy {info['renpy_commit']}, renpy-build {info['renpy_build_commit']}")

    old_base = lock["renpy_build_commit"]
    new_base = info["renpy_build_commit"]
    if build == lock["prerelease"]["build"] and new_base == old_base:
        print(json.dumps(status(src, lock, changed=False)))
        return

    if git(src, "status", "--porcelain", "--untracked-files=no"):
        raise SystemExit(f"{src} has uncommitted changes")

    # Rebase the branch's own commits onto the nightly's renpy-build commit.
    if new_base != old_base:
        git(src, "fetch", "-q", "--no-tags", UPSTREAM_BUILD, new_base, capture=False)
        ahead = git(src, "rev-list", "--count", f"{old_base}..HEAD")
        log(f"rebasing {ahead} branch commits from {old_base[:10]} onto {new_base[:10]}")
        result = subprocess.run(["git", "-C", str(src), "rebase", "--onto", new_base, old_base],
                                capture_output=True, text=True)
        if result.returncode != 0:
            subprocess.run(["git", "-C", str(src), "rebase", "--abort"])
            raise SystemExit(f"rebase onto renpy-build {new_base} failed; resolve by hand:\n"
                             f"{result.stdout}{result.stderr}")

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        base_url = NIGHTLY + quote(build) + "/"
        sdk_name = f"renpy-{build}-sdk.tar.bz2"
        renios_name = f"renpy-{build}-renios.zip"
        sdk_sha = download(base_url + quote(sdk_name), work / "sdk.tar.bz2")
        renios_sha = download(base_url + quote(renios_name), work / "renios.zip")
        uv_lock, uv_note = pinned_uv_lock(info["renpy_commit"], work / "sdk.tar.bz2",
                                          f"python{'.'.join(lock['python'].split('.')[:2])}", work)

    pin = lock["renpy_uv_lock"]
    (src / pin["file"]).write_bytes(uv_lock)
    pin["sha256"] = hashlib.sha256(uv_lock).hexdigest()
    pin["note"] = uv_note

    lock["renpy_build_commit"] = new_base
    lock["renpy_commit"] = info["renpy_commit"]
    lock["renpy_version"] = build.split("+", 1)[0]
    lock["prerelease"]["build"] = build
    lock["prerelease"]["page"] = base_url
    lock["renpy_sdk"]["url"] = base_url + sdk_name
    lock["renpy_sdk"]["sha256"] = sdk_sha
    lock["upstream_renios"]["url"] = base_url + renios_name
    lock["upstream_renios"]["sha256"] = renios_sha
    lock["patches_sha256"] = series.digest(src)
    (src / LOCK).write_text(json.dumps(lock, indent=2, ensure_ascii=False) + "\n")

    git(src, "add", LOCK, pin["file"], capture=False)
    git(src, "commit", "-q", "-m", f"lock: Ren'Py nightly {build}", "-m",
        f"Ren'Py {info['renpy_commit']}, renpy-build {new_base}.", capture=False)
    print(json.dumps(status(src, lock, changed=True)))


def status(src, lock, changed):
    head = git(src, "rev-parse", "HEAD")
    return {"changed": changed, "build": lock["prerelease"]["build"], "commit": head,
            "family": lock["deps"]["family"], "deps_current": deps_current(src, lock),
            "released": released_at(lock["engine"], head)}


def released_at(version, commit):
    """Whether a renpy-<version>-r<N> tag already points at `commit`."""

    out = subprocess.check_output(["gh", "api", "repos/{owner}/{repo}/git/matching-refs/tags/"
                                   f"renpy-{version}-r", "--jq", ".[] | [.ref, .object.sha] | @tsv"], text=True)
    for line in out.splitlines():
        ref, sha = line.split("\t")
        if re.fullmatch(r"refs/tags/renpy-" + re.escape(version) + r"-r\d+", ref) and sha == commit:
            return True
    return False


def deps_current(src, lock):
    """Whether the locked deps release was built from this checkout's recipe."""

    if not lock["deps"].get("release"):
        return False
    families = json.loads((HERE.parent.parent / "families.json").read_text())
    modules = families["families"][lock["deps"]["family"]]["deps_modules"]
    computed = recipe.compute(src, modules)["recipe_sha256"]
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["gh", "release", "download", lock["deps"]["release"], "-p", "build-info.json",
                        "-D", tmp], check=True)
        released = json.loads((Path(tmp) / "build-info.json").read_text())["recipe_sha256"]
    log(f"deps recipe: checkout {computed}, {lock['deps']['release']} {released}")
    return computed == released


# link-deps ####################################################################

def link_deps(args):
    src = args.src.resolve()
    lock = json.loads((src / LOCK).read_text())
    lock["deps"]["release"] = args.release
    lock["deps"]["sha256"] = bundle.sha256(args.tarball)
    (src / LOCK).write_text(json.dumps(lock, indent=2, ensure_ascii=False) + "\n")
    git(src, "add", LOCK, capture=False)
    git(src, "commit", "-q", "-m", f"lock: link {args.release}", capture=False)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(required=True)
    p = sub.add_parser("bump")
    p.add_argument("--src", type=Path, required=True)
    p.add_argument("--build", help="a specific nightly, e.g. 8.6.0.26100401+nightly.dirty")
    p.set_defaults(func=bump)
    p = sub.add_parser("link-deps")
    p.add_argument("--src", type=Path, required=True)
    p.add_argument("--release", required=True)
    p.add_argument("--tarball", type=Path, required=True)
    p.set_defaults(func=link_deps)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
