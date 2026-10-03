"""patches/renpylinter/series handling for engine branches.

Each non-comment line is ``<patch file> <target>``. ``root`` patches are
applied here, with ``patch -p1`` at the checkout root. ``task:<module>``
patches are applied by that task; this module only verifies that the task
references the file, so a listed patch cannot silently go unused.
"""

import hashlib
import subprocess
from pathlib import Path

PATCH_DIR = Path("patches/renpylinter")


def parse(root):
    directory = root / PATCH_DIR
    series = directory / "series"
    if not series.is_file():
        raise SystemExit(f"{series} is missing")

    entries = []
    for number, line in enumerate(series.read_text().splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2:
            raise SystemExit(f"{series}:{number}: expected '<patch> <target>'")
        name, target = parts
        if target != "root" and not target.startswith("task:"):
            raise SystemExit(f"{series}:{number}: unknown target {target!r}")
        if not (directory / name).is_file():
            raise SystemExit(f"{series}:{number}: {name} does not exist")
        entries.append((name, target))

    listed = {name for name, _ in entries}
    present = {p.name for p in directory.iterdir() if p.is_file() and p.name != "series"}
    if present - listed:
        raise SystemExit(f"Patches not listed in series: {sorted(present - listed)}")
    return entries


def digest(root):
    """sha256 over the series file and every patch, in series order."""

    h = hashlib.sha256()
    directory = root / PATCH_DIR
    h.update(b"series\0" + (directory / "series").read_bytes())
    for name, _ in parse(root):
        h.update(b"\0" + name.encode() + b"\0" + (directory / name).read_bytes())
    return h.hexdigest()


def describe(root):
    directory = root / PATCH_DIR
    return [{"name": name, "target": target,
             "sha256": hashlib.sha256((directory / name).read_bytes()).hexdigest()}
            for name, target in parse(root)]


def _patch(root, patch, *extra):
    return subprocess.run(["patch", "-p1", "--batch", "--forward", "--fuzz=0", *extra, "-i", str(patch)],
                          cwd=root, capture_output=True, text=True)


def apply(root):
    """Apply root patches once; verify task patches are wired up."""

    directory = root / PATCH_DIR
    for name, target in parse(root):
        patch = directory / name
        if target == "root":
            # Already applied (e.g. a resumed local build): reverse dry-run succeeds.
            if _patch(root, patch, "--reverse", "--dry-run").returncode == 0:
                print(f"series: {name} already applied")
                continue
            result = _patch(root, patch)
            if result.returncode != 0:
                raise SystemExit(f"series: {name} does not apply:\n{result.stdout}{result.stderr}")
            print(f"series: applied {name}")
        else:
            module = target.split(":", 1)[1]
            task_file = root / "tasks" / f"{module}.py"
            if f"renpylinter/{name}" not in task_file.read_text():
                raise SystemExit(f"series: tasks/{module}.py does not apply renpylinter/{name}")


def root_patch_paths(root):
    """Checkout paths modified by root patches (from their +++ headers)."""

    paths = set()
    for name, target in parse(root):
        if target != "root":
            continue
        for line in (root / PATCH_DIR / name).read_text().splitlines():
            if line.startswith("+++ "):
                paths.add(line[4:].split("\t")[0].split("/", 1)[1])
    return paths
