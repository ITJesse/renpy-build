#!/usr/bin/env python3
"""Dependency-layer recipe hash of a renpy-build checkout.

The hash covers, for every module in a family's ``deps_modules``:

* the task file ``tasks/<module>.py`` (configure flags, git tags/commits),
* every ``source/`` input it unpacks, resolved through its ``version``,
* every ``patches/`` file or directory it applies,

which includes the branch's RenPyLinter patches applied by those tasks.
Root patches (runtime/ sources) belong to the engine layer and are not
part of the hash. Two
checkouts with the same hash build the same dependency layer, which is what
``families.json`` groups engines by.

Usage: recipe.py SRC MODULE...    (prints JSON)
"""

import hashlib
import json
import re
import sys
from pathlib import Path

SOURCE_REF = re.compile(r"\{\{\s*source\s*\}\}/([^\s\"']+)")
PATCH_REF = re.compile(r"c\.patch(?:dir)?\(\s*[\"']([^\"']+)[\"']")
VERSION = re.compile(r"^(\w*version)\s*=\s*[\"']([^\"']+)[\"']", re.M)


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree(path):
    return {str(p.relative_to(path)): _sha(p) for p in sorted(path.rglob("*")) if p.is_file()}


def module_inputs(root, module):
    task = root / "tasks" / f"{module}.py"
    text = task.read_text()
    versions = dict(VERSION.findall(text))

    def expand(ref):
        def sub(m):
            name = m.group(1)
            if name not in versions:
                raise SystemExit(f"tasks/{module}.py: unresolved {{{{ {name} }}}} in {ref}")
            return versions[name]
        return re.sub(r"\{\{\s*(\w*version)\s*\}\}", sub, ref)

    inputs = {"task": _sha(task), "source": {}, "patches": {}}

    for ref in SOURCE_REF.findall(text):
        ref = expand(ref).rstrip(")\"'")
        path = root / "source" / ref
        if path.is_file():
            inputs["source"][ref] = _sha(path)
        elif path.is_dir():
            inputs["source"][ref] = _tree(path)
        else:
            raise SystemExit(f"tasks/{module}.py references missing source/{ref}")

    for ref in PATCH_REF.findall(text):
        ref = expand(ref)
        path = root / "patches" / ref
        if path.is_file():
            inputs["patches"][ref] = _sha(path)
        elif path.is_dir():
            inputs["patches"][ref] = _tree(path)
        else:
            raise SystemExit(f"tasks/{module}.py references missing patches/{ref}")

    return inputs


def compute(root, modules):
    root = Path(root).resolve()
    detail = {m: module_inputs(root, m) for m in modules}
    canonical = json.dumps(detail, sort_keys=True, separators=(",", ":")).encode()
    return {"recipe_sha256": hashlib.sha256(canonical).hexdigest(), "modules": detail}


if __name__ == "__main__":
    result = compute(sys.argv[1], sys.argv[2:])
    print(json.dumps(result, indent=2, sort_keys=True))
