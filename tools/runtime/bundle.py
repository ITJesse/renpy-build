"""Release bundle helpers: checksums, relocation, deterministic tarballs."""

import gzip
import hashlib
import json
import os
import shutil
import tarfile
from pathlib import Path

PREFIX_PLACEHOLDER = "@RPL_INSTALL_PREFIX@"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def files(root):
    return sorted(p for p in Path(root).rglob("*") if p.is_file() and not p.is_symlink())


def is_text(path):
    data = path.read_bytes()
    return b"\0" not in data[:65536]


def copy_tree(src, dst, *, skip=lambda p: False):
    """Copy regular files (symlinks resolved to copies of their targets)."""

    src, dst = Path(src), Path(dst)
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if skip(rel) or path.is_dir():
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path.resolve(), target)


def relocate_out(root, prefix):
    """Replace the absolute install prefix in text files with a placeholder."""

    changed = []
    needle = str(prefix).encode()
    for path in files(root):
        if path.suffix == ".a" or not is_text(path):
            continue
        data = path.read_bytes()
        if needle in data:
            path.write_bytes(data.replace(needle, PREFIX_PLACEHOLDER.encode()))
            changed.append(str(path.relative_to(root)))
    return changed


def relocate_in(root, relative_paths, prefix):
    for rel in relative_paths:
        path = Path(root) / rel
        path.write_bytes(path.read_bytes().replace(PREFIX_PLACEHOLDER.encode(), str(prefix).encode()))


def write_sums(root):
    """SHA256SUMS over every file except itself; returns {path: sha256}."""

    root = Path(root)
    sums = {str(p.relative_to(root)): sha256(p) for p in files(root) if p.name != "SHA256SUMS"}
    (root / "SHA256SUMS").write_text("".join(f"{h}  {p}\n" for p, h in sorted(sums.items())))
    return sums


def read_sums(path):
    sums = {}
    for line in Path(path).read_text().splitlines():
        digest, name = line.split("  ", 1)
        sums[name] = digest
    return sums


def verify_sums(root):
    root = Path(root)
    sums = read_sums(root / "SHA256SUMS")
    present = {str(p.relative_to(root)) for p in files(root) if p.name != "SHA256SUMS"}
    if present != set(sums):
        raise SystemExit(f"SHA256SUMS does not list exactly the bundle files: "
                         f"missing {sorted(present - set(sums))}, extra {sorted(set(sums) - present)}")
    for name, digest in sums.items():
        if sha256(root / name) != digest:
            raise SystemExit(f"{name}: checksum mismatch")
    return sums


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def tarball(root, output, mtime):
    """Deterministic .tar.gz: sorted entries, fixed mtime/owner/mode, no gzip name."""

    root = Path(root)
    with open(output, "wb") as out, \
            gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0, compresslevel=9) as gz, \
            tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
        entries = sorted(root.rglob("*"))
        for path in entries:
            rel = path.relative_to(root).as_posix()
            info = tarfile.TarInfo(rel)
            info.mtime = mtime
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if path.is_dir():
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            else:
                info.size = path.stat().st_size
                info.mode = 0o755 if os.access(path, os.X_OK) else 0o644
                with open(path, "rb") as f:
                    tar.addfile(info, f)


def extract(tar_path, dest):
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path) as tar:
        tar.extractall(dest, filter="data")
