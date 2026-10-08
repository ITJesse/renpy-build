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
# The same code as MODULE with a different comment: compiles to an equal code object.
MODULE_COPY = MODULE.replace('return x\n', 'return x  # vendored copy\n')
OTHER = 'def f(x):\n    return x + 1\n'
SITE = '"""site replacement."""\nVALUE = 1\n'
PYVER = f"python{sys.version_info[0]}.{sys.version_info[1]}"


def py3_pyc(source, destination, optimize=0, dfile=None):
    destination.parent.mkdir(parents=True, exist_ok=True)
    py_compile.compile(str(source), cfile=str(destination), dfile=dfile, doraise=True, optimize=optimize)


def consts(code):
    for c in code.co_consts:
        yield c
        if hasattr(c, "co_consts"):
            yield from consts(c)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class Python3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.lib = self.root / "src" / "install" / "lib" / PYVER
        self.site_packages = self.lib / "site-packages"
        self.pytmp = self.root / "src" / "pytmp" / "pyobjus"
        self.runtime = self.root / "src" / "runtime"
        self.generated = self.root / "src" / "generated" / PYVER
        write(self.site_packages / "pkg" / "mod.py", MODULE)
        # A vendored copy under site-packages/pip/_vendor is not a search base.
        write(self.site_packages / "pip" / "_vendor" / "pkg" / "mod.py", MODULE_COPY)
        write(self.runtime / "site3.py", SITE)
        write(self.lib / "site.py", OTHER)
        # A source the task generated from runtime/site3.py and deleted.
        write(self.generated / "sitecustomize.py", SITE + "import site\n")

    def tearDown(self):
        self.tmp.cleanup()

    def bases(self):
        return [self.lib, self.site_packages, self.pytmp]

    def bundle(self, name):
        stdlib = self.root / name / "lib" / PYVER
        py3_pyc(self.site_packages / "pkg" / "mod.py", stdlib / "pkg" / "mod.pyc")
        py3_pyc(self.runtime / "site3.py", stdlib / "site.pyc")
        py3_pyc(self.generated / "sitecustomize.py", stdlib / "sitecustomize.pyc")
        (stdlib / "certifi").mkdir(parents=True)
        (stdlib / "certifi" / "cacert.pem.pyc").write_bytes(b"-----BEGIN CERTIFICATE-----\n")
        return stdlib

    def recompile(self, stdlib, generated=True):
        return pybytecode.recompile_stdlib(sys.executable, "3", stdlib, PYVER, bases=self.bases(),
                                           generated=self.generated if generated else None,
                                           runtime=sorted(self.runtime.glob("*.py")))

    def test_identifies_source_and_optimizes(self):
        stdlib = self.bundle("a")
        report = self.recompile(stdlib)
        self.assertEqual(report["recompiled"], 3)
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
        self.assertIn("VALUE", site.co_names)  # from runtime/site3.py, not lib/site.py
        custom = marshal.loads((stdlib / "sitecustomize.pyc").read_bytes()[16:])
        self.assertIn("site", custom.co_names)  # from the kept generated source
        self.assertEqual((stdlib / "certifi" / "cacert.pem.pyc").read_bytes(), b"-----BEGIN CERTIFICATE-----\n")

    def test_same_module_is_byte_identical_across_bundles(self):
        a, b = self.bundle("a"), self.bundle("b")
        os.utime(self.site_packages / "pkg" / "mod.py", (1, 1))  # a different source mtime must not matter
        py3_pyc(self.site_packages / "pkg" / "mod.py", b / "pkg" / "mod.pyc")
        self.recompile(a)
        self.recompile(b)
        self.assertEqual((a / "pkg" / "mod.pyc").read_bytes(), (b / "pkg" / "mod.pyc").read_bytes())

    def test_differing_sources_follow_the_absolute_co_filename(self):
        # The same module in two search bases, equal code but different bytes;
        # the bundled pyc names the second one (make install bytecode does).
        write(self.lib / "pkg" / "mod.py", MODULE_COPY)
        stdlib = self.bundle("a")
        target = self.lib / "pkg" / "mod.py"
        py3_pyc(target, stdlib / "pkg" / "mod.pyc", dfile=str(target))
        report = self.recompile(stdlib)
        self.assertEqual(report["chosen_among_differing_sources"],
                         [{"module": "pkg/mod.pyc", "source": str(target)}])

    def test_differing_sources_follow_the_task_order(self):
        # Without an absolute co_filename, Python 3's pythonlib keeps the last base's copy.
        write(self.lib / "pkg" / "mod.py", MODULE_COPY)
        stdlib = self.bundle("a")
        report = self.recompile(stdlib)
        self.assertEqual(report["chosen_among_differing_sources"],
                         [{"module": "pkg/mod.pyc", "source": str(self.site_packages / "pkg" / "mod.py")}])

    def test_module_without_a_source_fails(self):
        stdlib = self.bundle("a")
        with self.assertRaisesRegex(SystemExit, "sitecustomize.pyc"):
            self.recompile(stdlib, generated=False)
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
            lib = root / "src" / "install" / "lib" / "python2.7"
            source = lib / "mod.py"
            write(source, MODULE)
            stdlib = root / "bundle" / "lib" / "python2.7"
            stdlib.mkdir(parents=True)
            subprocess.run([py2, "-O", "-c", "import py_compile, sys; py_compile.compile(sys.argv[1], sys.argv[2])",
                            str(source), str(stdlib / "mod.pyo")], check=True)
            before = (stdlib / "mod.pyo").read_bytes()
            report = pybytecode.recompile_stdlib(py2, "2", stdlib, "python2.7", bases=[lib], generated=None,
                                                 runtime=[])
            self.assertEqual(report["recompiled"], 1)
            after = (stdlib / "mod.pyo").read_bytes()
            self.assertEqual(after[:4], before[:4])
            self.assertEqual(after[4:8], pybytecode.PY2_PYO_MTIME.to_bytes(4, "little"))
            check = subprocess.run([py2, "-c", "import marshal, sys; c = marshal.loads(open(sys.argv[1], 'rb').read()[8:]);"
                                    "print(c.co_filename); print('Function docstring.' in repr(c.co_consts) or"
                                    " any('Function docstring.' in repr(x.co_consts) for x in c.co_consts if hasattr(x, 'co_consts')))",
                                    str(stdlib / "mod.pyo")], check=True, capture_output=True, text=True)
            self.assertEqual(check.stdout.split(), ["lib/python2.7/mod.py", "False"])


class KeepDeletedSourcesTests(unittest.TestCase):
    def test_unlink_keeps_python_sources_of_the_lib_tree(self):
        import run_tasks

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            class Context:
                def path(self, fn):
                    return root / fn

                def unlink(self, fn):
                    (root / fn).unlink()

            run_tasks.keep_deleted_sources(Context, root)
            write(root / "renpy" / "lib" / PYVER / "sitecustomize.py", SITE)
            write(root / "elsewhere.py", OTHER)
            Context().unlink(f"renpy/lib/{PYVER}/sitecustomize.py")
            Context().unlink("elsewhere.py")
            kept = root / run_tasks.GENERATED_SOURCES
            self.assertEqual((kept / PYVER / "sitecustomize.py").read_text(), SITE)
            self.assertFalse((root / "renpy" / "lib" / PYVER / "sitecustomize.py").exists())
            self.assertEqual([p.name for p in kept.rglob("*.py")], ["sitecustomize.py"])


if __name__ == "__main__":
    unittest.main()
