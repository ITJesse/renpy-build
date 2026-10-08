"""Optimized, deterministic bytecode for the engine bundle.

Every Python file an engine bundle ships is compiled with docstrings and
asserts removed (-OO, optimize=2), the way upstream already builds Ren'Py 7's
.pyo files. The bundle's standard library comes from the pythonlib task, whose
output differs between renpy-build versions: older branches copy the bytecode
`make install` left in __pycache__ (timestamp headers, absolute build paths,
optimize 0), newer ones recompile through `Context.compile` (unchecked-hash
headers, `lib/pythonX.Y/...` paths, still optimize 0 on Python 3).
`recompile_stdlib` therefore rebuilds that tree from source:

* each bundled module's source is found where the pythonlib task finds
  modules: `<base>/<module path>.py` for the task's search bases, a source the
  task generated in the lib tree and deleted after compiling it (kept by
  run_tasks.py), and, for top-level modules, the runtime/*.py files the task
  copies there under another name;
* a candidate is accepted only if it compiles, at an optimization level the
  tasks use, to a code object equal to the bundled one. Among several
  accepted candidates with different contents the choice follows the bundled
  module's absolute co_filename when it has one, then a generated source,
  then the task's own precedence (Python 3: the last search base wins,
  Python 2: the first);
* the module is recompiled from that source at optimize=2, with the display
  path `lib/<pythonver>/<module path>.py` that `Context.compile` uses;
* Python 3 writes unchecked-hash pycs (the header holds a hash of the source),
  Python 2 writes .pyo files with a fixed timestamp. A sourceless module is
  loaded without looking at either, and the same module then has the same
  bytes in every engine of a Python version, which the app's bundle
  de-duplication relies on.

A bundled module without an accepted source stops the build. Files named like
bytecode that are not bytecode (upstream's pythonlib copies certifi's `.pem`
files under a `.pyc` name in some versions) are left alone and reported.
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
#           -> {bundled: {"magic": bool, "filename": co_filename, "matches": [candidate, ...]}}
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
            result[bundled] = {"magic": False, "filename": None, "matches": []}
            continue
        old = marshal.loads(data[HEADER:])
        matches = []
        for candidate in candidates:
            for level in levels:
                code = compiled(candidate, level)
                if code is not None and code == old:
                    matches.append(candidate)
                    break
        result[bundled] = {"magic": True, "filename": old.co_filename, "matches": matches}
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


def candidates_for(rel, bases, generated, runtime):
    """Candidate sources of the bundled module `rel` (a path ending in .pyc/.pyo).

    Returns (source path, origin) pairs: origin is the index of the search
    base, "generated" or "runtime".
    """

    source = rel.with_suffix(".py")
    found = [(Path(base) / source, i) for i, base in enumerate(bases) if (Path(base) / source).is_file()]
    if generated is not None and (Path(generated) / source).is_file():
        found.append((Path(generated) / source, "generated"))
    if len(source.parts) == 1:
        found += [(Path(p), "runtime") for p in sorted(runtime)]
    return found


def choose(python_major, filename, matches):
    """Pick one of the accepted (path, origin) candidates; None if they disagree without a rule."""

    if len({p.read_bytes() for p, _ in matches}) == 1:
        return matches[0][0]
    for path, _ in matches:
        if filename and Path(filename).is_absolute() and Path(filename) == path:
            return path
    generated = [p for p, origin in matches if origin == "generated"]
    if generated:
        return generated[0]
    from_bases = [(origin, p) for p, origin in matches if isinstance(origin, int)]
    if from_bases:
        from_bases.sort()
        return from_bases[-1][1] if python_major == "3" else from_bases[0][1]
    return None


def write_bytecode(hostpython, python_major, items, optimize=OPTIMIZE):
    """items: [(source, destination, dfile)] compiled at `optimize` (Python 2: the -O flag count)."""

    if not items:
        return
    flags = ["-" + "O" * optimize] if python_major == "2" and optimize else []
    _helper(hostpython, flags, {"mode": "write", "optimize": optimize, "py2_mtime": PY2_PYO_MTIME,
                                "items": [[str(s), str(d), dfile] for s, d, dfile in items]})


def recompile_stdlib(hostpython, python_major, stdlib, pythonver, bases, generated, runtime):
    """Recompile the bundled standard library tree `stdlib` in place; returns a report.

    bases: the pythonlib task's search bases, in its order. generated: the
    directory holding sources the task generated and deleted, laid out like
    `stdlib` (or None). runtime: the runtime/*.py files.
    """

    stdlib = Path(stdlib)
    suffix = ".pyo" if python_major == "2" else ".pyc"
    bundled = sorted(p for p in stdlib.rglob("*" + suffix) if p.is_file())
    candidates = {str(p): candidates_for(p.relative_to(stdlib), bases, generated, runtime) for p in bundled}
    origins = {str(p): {str(c): o for c, o in candidates[str(p)]} for p in bundled}

    # Python 2 cannot choose the optimization level per compile() call.
    passes = [["-OO"], ["-O"], []] if python_major == "2" else [[]]
    verdicts = {}
    for flags in passes:
        pending = [[b, [str(c) for c, _ in candidates[b]]] for b in candidates
                   if not verdicts.get(b, {}).get("matches")]
        if not pending:
            break
        verdicts.update(_helper(hostpython, flags, {"mode": "verify", "items": pending}))

    not_bytecode, unmatched, ambiguous, items, by_rule = [], [], [], [], []
    for path in bundled:
        rel = path.relative_to(stdlib)
        verdict = verdicts[str(path)]
        if not verdict["magic"]:
            not_bytecode.append(str(rel))
            continue
        matches = [(Path(m), origins[str(path)][m]) for m in verdict["matches"]]
        if not matches:
            unmatched.append({"module": str(rel), "candidates": [str(c) for c, _ in candidates[str(path)]]})
            continue
        source = choose(python_major, verdict["filename"], matches)
        if source is None:
            ambiguous.append({"module": str(rel), "sources": [str(m) for m, _ in matches]})
            continue
        if len({m.read_bytes() for m, _ in matches}) > 1:
            by_rule.append({"module": str(rel), "source": str(source)})
        items.append((source, path, f"lib/{pythonver}/{rel.with_suffix('.py')}"))

    if unmatched or ambiguous:
        raise SystemExit("standard library bytecode without a unique source:\n"
                         + json.dumps({"unmatched": unmatched, "ambiguous": ambiguous}, indent=1))

    before = sum(p.stat().st_size for p in bundled)
    write_bytecode(hostpython, python_major, items)
    after = sum(p.stat().st_size for p in bundled)
    return {"recompiled": len(items), "chosen_among_differing_sources": by_rule,
            "not_bytecode": not_bytecode, "bytes_before": before, "bytes_after": after}
