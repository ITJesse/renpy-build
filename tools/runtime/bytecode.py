"""Optimized, deterministic bytecode for the engine bundle.

Every Python file an engine bundle ships is compiled with docstrings and
asserts removed (-OO, optimize=2), the way upstream already builds Ren'Py 7's
.pyo files. The bundle's standard library comes from the pythonlib task, whose
output differs between renpy-build versions: older branches copy the bytecode
`make install` left in __pycache__ (timestamp headers, absolute build paths,
optimize 0), newer ones recompile through `Context.compile` (unchecked-hash
headers, `lib/pythonX.Y/...` paths, still optimize 0 on Python 3).
`recompile_stdlib` therefore rebuilds that tree from source:

* the source of each bundled module is not guessed from its name: every
  candidate file is compiled at the optimization levels the tasks use and
  must produce a code object equal to the bundled one;
* the module is recompiled from that source at optimize=2, with the display
  path `lib/<pythonver>/<module path>.py` that `Context.compile` uses;
* Python 3 writes unchecked-hash pycs (the header holds a hash of the source),
  Python 2 writes .pyo files with a fixed timestamp. A sourceless module is
  loaded without looking at either, and the same module then has the same
  bytes in every engine of a Python version, which the app's bundle
  de-duplication relies on.

Files named like bytecode that are not bytecode (upstream's pythonlib copies
certifi's `.pem` files under a `.pyc` name in some versions) are left alone
and reported.
"""

import json
import subprocess
from pathlib import Path

# Written into Python 2 .pyo headers. Sourceless imports never compare it.
PY2_PYO_MTIME = 0

OPTIMIZE = 2

# Runs under the engine's host Python (2.7 or 3.x). stdin: JSON
# {"mode": "verify"|"write", "items": [...]}; stdout: JSON.
#   verify: items = [[bundled, [candidate, ...]], ...]
#           -> {bundled: {"magic": bool, "matches": [candidate, ...]}}
#           Python 3 compares at optimize 0, 1 and 2 in one process; Python 2
#           compares at the optimization level of the process (-O/-OO flags).
#   write:  items = [[source, destination, dfile], ...] -> {"written": n}
HELPER = r'''
# marshal only reads bytecode this build produced; nothing it loads is executed.
import json, marshal, sys
PY2 = sys.version_info[0] == 2
if PY2:
    import imp
    MAGIC = imp.get_magic()
    HEADER = 8
else:
    import importlib.util, py_compile
    MAGIC = importlib.util.MAGIC_NUMBER
    HEADER = 16

def read_source(path):
    if PY2:
        with open(path, "U") as f:
            text = f.read()
        if text and text[-1] not in "\r\n":
            text += "\n"
        return text
    with open(path, "rb") as f:
        return f.read()

def compiled(path, level):
    try:
        source = read_source(path)
        if PY2:
            return compile(source, path, "exec")
        return compile(source, path, "exec", dont_inherit=True, optimize=level)
    except (SyntaxError, ValueError, UnicodeDecodeError, TypeError):
        return None

request = json.load(sys.stdin)
if request["mode"] == "verify":
    result = {}
    levels = [None] if PY2 else [0, 1, 2]
    for bundled, candidates in request["items"]:
        with open(bundled, "rb") as f:
            data = f.read()
        if data[:4] != MAGIC:
            result[bundled] = {"magic": False, "matches": []}
            continue
        old = marshal.loads(data[HEADER:])
        matches = []
        for candidate in candidates:
            for level in levels:
                code = compiled(candidate, level)
                if code is not None and code == old:
                    matches.append(candidate)
                    break
        result[bundled] = {"magic": True, "matches": matches}
    json.dump(result, sys.stdout)
else:
    import struct
    mtime = request["py2_mtime"]
    for source, destination, dfile in request["items"]:
        if PY2:
            code = compile(read_source(source), dfile, "exec")
            with open(destination, "wb") as f:
                f.write(MAGIC + struct.pack("<I", mtime) + marshal.dumps(code))
        else:
            py_compile.compile(source, cfile=destination, dfile=dfile, doraise=True,
                               optimize=request["optimize"],
                               invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
    json.dump({"written": len(request["items"])}, sys.stdout)
'''


def _helper(hostpython, flags, request):
    out = subprocess.run([str(hostpython), *flags, "-c", HELPER], input=json.dumps(request),
                         capture_output=True, text=True)
    if out.returncode:
        raise SystemExit(f"bytecode helper ({hostpython} {' '.join(flags)}) failed:\n{out.stderr}")
    return json.loads(out.stdout)


def index_sources(roots):
    """file name -> [paths] for every .py under roots (symlinks not followed)."""

    found = {}
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if path.is_file() and not path.is_symlink():
                found.setdefault(path.name, []).append(path)
    return found


def candidates_for(rel, by_name, extra):
    """Sources whose path ends with the module's path, plus `extra` for top-level modules."""

    want = rel.with_suffix(".py")
    tail = want.parts
    found = [p for p in by_name.get(want.name, []) if p.parts[-len(tail):] == tail]
    if len(tail) == 1:
        found += [p for p in extra if p not in found]
    return found


def write_bytecode(hostpython, python_major, items, optimize=OPTIMIZE):
    """items: [(source, destination, dfile)] compiled at `optimize` (Python 2: the -O flag count)."""

    if not items:
        return
    flags = ["-" + "O" * optimize] if python_major == "2" and optimize else []
    _helper(hostpython, flags, {"mode": "write", "optimize": optimize, "py2_mtime": PY2_PYO_MTIME,
                                "items": [[str(s), str(d), dfile] for s, d, dfile in items]})


def recompile_stdlib(hostpython, python_major, stdlib, pythonver, source_roots, extra_sources):
    """Recompile the bundled standard library tree `stdlib` in place; returns a report."""

    stdlib = Path(stdlib)
    suffix = ".pyo" if python_major == "2" else ".pyc"
    by_name = index_sources(source_roots)
    extra = sorted(Path(p) for p in extra_sources)

    bundled = sorted(p for p in stdlib.rglob("*" + suffix) if p.is_file())
    requests = []
    for path in bundled:
        rel = path.relative_to(stdlib)
        requests.append([str(path), [str(c) for c in candidates_for(rel, by_name, extra)]])

    # Python 2 cannot choose the optimization level per compile() call.
    passes = [["-OO"], ["-O"], []] if python_major == "2" else [[]]
    verdicts = {}
    for flags in passes:
        pending = [r for r in requests if not verdicts.get(r[0], {}).get("matches")]
        if not pending:
            break
        for bundled_path, verdict in _helper(hostpython, flags, {"mode": "verify", "items": pending}).items():
            verdicts[bundled_path] = verdict

    not_bytecode, unmatched, ambiguous, items = [], [], [], []
    for path in bundled:
        rel = path.relative_to(stdlib)
        verdict = verdicts[str(path)]
        if not verdict["magic"]:
            not_bytecode.append(str(rel))
            continue
        matches = verdict["matches"]
        if not matches:
            unmatched.append(str(rel))
            continue
        contents = {Path(m).read_bytes() for m in matches}
        if len(contents) > 1:
            ambiguous.append({"module": str(rel), "sources": matches})
            continue
        items.append((Path(matches[0]), path, f"lib/{pythonver}/{rel.with_suffix('.py')}"))

    if unmatched or ambiguous:
        raise SystemExit("standard library bytecode without a unique source:\n"
                         + json.dumps({"unmatched": unmatched, "ambiguous": ambiguous}, indent=1))

    before = sum(p.stat().st_size for p in bundled)
    write_bytecode(hostpython, python_major, items)
    after = sum(p.stat().st_size for p in bundled)
    return {"recompiled": len(items), "not_bytecode": not_bytecode, "bytes_before": before,
            "bytes_after": after}
