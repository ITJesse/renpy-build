"""Packaging regression: a fork's common script must beat SDK bytecode."""
import tempfile
import unittest
from pathlib import Path
import bundle


class CompiledScriptsTests(unittest.TestCase):
    def test_patched_source_is_not_shadowed_by_sdk_bytecode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / 'sdk'
            out = root / 'out'
            (sdk / 'common').mkdir(parents=True)
            (out / 'common').mkdir(parents=True)
            compiled = {}
            for source, bytecode in [('00compat.rpy', '00compat.rpyc'), ('other.rpy', 'other.rpyc'), ('layout.rpym', 'layout.rpymc')]:
                (out / 'common' / source).write_text('patched or original source')
                path = sdk / 'common' / bytecode
                path.write_bytes(b'pristine SDK bytecode')
                compiled['common/' + bytecode] = path
            result = bundle.copy_compiled_scripts(compiled, out, ['renpy/common/00compat.rpy', 'renpy/common/layout.rpym'])
            self.assertEqual(result, ['common/00compat.rpy', 'common/layout.rpym'])
            self.assertFalse((out / 'common/00compat.rpyc').exists())
            self.assertFalse((out / 'common/layout.rpymc').exists())
            self.assertEqual((out / 'common/other.rpyc').read_bytes(), b'pristine SDK bytecode')
            (out / 'common/00compat.rpy').unlink()
            with self.assertRaisesRegex(SystemExit, 'source missing'):
                bundle.copy_compiled_scripts(compiled, out, ['renpy/common/00compat.rpy'])


if __name__ == '__main__':
    unittest.main()
