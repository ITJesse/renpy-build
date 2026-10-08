"""pybytecode.recompile_stdlib: source identification, optimization, determinism.

Python 3 runs with this interpreter. Set PYTHON2 to a Python 2.7 to also test
the .pyo path:  PYTHON2=/path/to/python2.7 python3 test_pybytecode.py
"""
import importlib.util
import marshal
import os
import py_compile
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pybytecode

MODULE = '"""Module docstring."""\n\ndef f(x):\n    """Function docstring."""\n    assert x\n    return x\n'
OTHER = 'def f(x):\n    return x + 1\n'
SITE = '"""site replacement."""\nVALUE = 1\n'
PYVER = f"python{sys.version_info[0]}.{sys.version_info[1]}"


def py3_pyc(source, destination, optimize=0):
    destination.parent.mkdir(parents=True, exist_ok=True)
    py_compile.compile(str(source), cfile=str(destination), doraise=True, optimize=optimize)


def consts(code):
    for c in code.co_consts:
        yield c
        if hasattr(c, "co_consts"):
            yield from consts(c)


class Python3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.lib = self.root / "src" / "install" / "lib" / PYVER
        self.runtime = self.root / "src" / "runtime"
        self.other = self.root / "src" / "pytmp" / "pkg"
        for path, text in [(self.lib / "pkg" / "mod.py", MODULE), (self.other / "mod.py", OTHER),
                           (self.runtime / "site3.py", SITE), (self.lib / "site.py", OTHER)]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def tearDown(self):
        self.tmp.cleanup()

    def bundle(self, name):
        stdlib = self.root / name / "lib" / PYVER
        py3_pyc(self.lib / "pkg" / "mod.py", stdlib / "pkg" / "mod.pyc")
        py3_pyc(self.runtime / "site3.py", stdlib / "site.pyc")
        (stdlib / "certifi").mkdir(parents=True)
        (stdlib / "certifi" / "cacert.pem.pyc").write_bytes(b"-----BEGIN CERTIFICATE-----\n")
        return stdlib

    def recompile(self, stdlib):
        return pybytecode.recompile_stdlib(sys.executable, "3", stdlib, PYVER,
                                         source_roots=[self.root / "src"],
                                         extra_sources=sorted(self.runtime.glob("*.py")))

    def test_identifies_source_and_optimizes(self):
        stdlib = self.bundle("a")
        report = self.recompile(stdlib)
        self.assertEqual(report["recompiled"], 2)
        self.assertEqual(report["not_bytecode"], ["certifi/cacert.pem.pyc"])
        self.assertLess(report["bytes_after"], report["bytes_before"])

        data = (stdlib / "pkg" / "mod.pyc").read_bytes()
        self.assertEqual(data[:4], importlib.util.MAGIC_NUMBER)
        self.assertEqual(int.from_bytes(data[4:8], "little"), 0b01)  # hash-based, unchecked
        code = marshal.loads(data[16:])
        self.assertEqual(code.co_filename, f"lib/{PYVER}/pkg/mod.py")
        texts = [c for c in consts(code) if isinstance(c, str)]
        self.assertNotIn("Module docstring.", texts)
        self.assertNotIn("Function docstring.", texts)
        names = [n for c in [code, *[c for c in consts(code) if hasattr(c, "co_names")]] for n in c.co_names]
        self.assertNotIn("AssertionError", names)

        site = marshal.loads((stdlib / "site.pyc").read_bytes()[16:])
        self.assertEqual(site.co_filename, f"lib/{PYVER}/site.py")
        self.assertIn("VALUE", site.co_names)  # compiled from runtime/site3.py, not lib/site.py
        self.assertNotIn("site replacement.", list(consts(site)))
        self.assertEqual((stdlib / "certifi" / "cacert.pem.pyc").read_bytes(), b"-----BEGIN CERTIFICATE-----\n")

    def test_same_module_is_byte_identical_across_bundles(self):
        a, b = self.bundle("a"), self.bundle("b")
        os.utime(self.lib / "pkg" / "mod.py", (1, 1))  # a different source mtime must not matter
        py3_pyc(self.lib / "pkg" / "mod.py", b / "pkg" / "mod.pyc")
        self.recompile(a)
        self.recompile(b)
        self.assertEqual((a / "pkg" / "mod.pyc").read_bytes(), (b / "pkg" / "mod.pyc").read_bytes())

    def test_module_without_a_source_fails(self):
        stdlib = self.bundle("a")
        orphan = self.root / "orphan.py"
        orphan.write_text("ORPHAN = True\n")
        py3_pyc(orphan, stdlib / "orphan.pyc")
        with self.assertRaisesRegex(SystemExit, "orphan.pyc"):
            self.recompile(stdlib)


@unittest.skipUnless(os.environ.get("PYTHON2"), "PYTHON2 not set")
class Python2Tests(unittest.TestCase):
    def test_pyo_from_O_build_is_recompiled_with_OO(self):
        py2 = os.environ["PYTHON2"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src" / "install" / "lib" / "python2.7" / "mod.py"
            source.parent.mkdir(parents=True)
            source.write_text(MODULE)
            stdlib = root / "bundle" / "lib" / "python2.7"
            stdlib.mkdir(parents=True)
            subprocess.run([py2, "-O", "-c", "import py_compile, sys; py_compile.compile(sys.argv[1], sys.argv[2])",
                            str(source), str(stdlib / "mod.pyo")], check=True)
            before = (stdlib / "mod.pyo").read_bytes()
            report = pybytecode.recompile_stdlib(py2, "2", stdlib, "python2.7",
                                               source_roots=[root / "src"], extra_sources=[])
            self.assertEqual(report["recompiled"], 1)
            after = (stdlib / "mod.pyo").read_bytes()
            self.assertEqual(after[:4], before[:4])
            self.assertEqual(after[4:8], pybytecode.PY2_PYO_MTIME.to_bytes(4, "little"))
            check = subprocess.run([py2, "-c", "import marshal, sys; c = marshal.loads(open(sys.argv[1], 'rb').read()[8:]);"
                                    "print(c.co_filename); print('Function docstring.' in repr(c.co_consts) or"
                                    " any('Function docstring.' in repr(x.co_consts) for x in c.co_consts if hasattr(x, 'co_consts')))",
                                    str(stdlib / "mod.pyo")], check=True, capture_output=True, text=True)
            self.assertEqual(check.stdout.split(), ["lib/python2.7/mod.py", "False"])


if __name__ == "__main__":
    unittest.main()
