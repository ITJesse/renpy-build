"""Compile and exercise the actual row-packing code from the engine patch.

Run with Cython 0.29.37 and setuptools installed. No GPU or game modification.
The GPU/PBO integration is separately tested in the signed iPad-on-Mac app.
"""
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/renpylinter/renpy-pack-texture-upload.diff'


def build_probe():
    added = '\n'.join(line[1:] for line in PATCH.read_text().splitlines()
                      if line.startswith('+') and not line.startswith('+++'))
    pack = added[added.index('        upload_pixels ='):added.index('        # Both GL')]
    pack = textwrap.dedent(pack).replace('self.loader.max_texture_width', 'limit').replace('self.width', 'width').replace('self.height', 'height')
    source = '''
from libc.stdlib cimport malloc as real_malloc, free as real_free
from libc.string cimport memcpy
ctypedef unsigned char Uint8
cdef struct Surface:
    void *pixels
    int pitch
cdef int active = 0
cdef bint fail_alloc = False
cdef void *malloc(size_t n):
    global active
    if fail_alloc:
        return NULL
    cdef void *p = real_malloc(n)
    if p != NULL:
        active += 1
    return p
cdef void free(void *p):
    global active
    if p != NULL:
        active -= 1
    real_free(p)
def outstanding():
    return active
def run(bytearray data, int width, int height, int pitch, int offset,
        int limit=4096, bint fail=False, bint upload_error=False):
    global fail_alloc
    fail_alloc = fail
    cdef Surface surface
    cdef Surface *s = &surface
    cdef Uint8 *base = data
    s.pixels = base + offset
    s.pitch = pitch
    cdef Uint8 *packed_pixels = NULL
    cdef Uint8 *upload_pixels
    cdef size_t upload_pitch
    cdef int row
'''
    source += textwrap.indent(pack, '    ')
    source += '''
    try:
        if upload_error:
            raise RuntimeError("injected upload failure")
        rows = []
        for row in range(height):
            rows.append((<char *> (upload_pixels + row * upload_pitch))[:width * 4])
        return (b''.join(rows), upload_pitch, packed_pixels != NULL)
    finally:
        free(packed_pixels)
'''
    directory = pathlib.Path(tempfile.mkdtemp(prefix='rpl-texture-test-'))
    (directory / 'texture_probe.pyx').write_text(source)
    (directory / 'setup.py').write_text("from setuptools import setup\nfrom Cython.Build import cythonize\nsetup(ext_modules=cythonize('texture_probe.pyx', language_level=2))\n")
    subprocess.run([sys.executable, 'setup.py', 'build_ext', '--inplace'], cwd=directory, check=True, stdout=subprocess.DEVNULL)
    sys.path.insert(0, str(directory))
    import texture_probe
    return texture_probe


class TextureUpload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probe = build_probe()

    def check_crop(self, parent_width, parent_height, x, y, width, height, copied):
        pitch = parent_width * 4
        # Distinct RGB and alpha bytes, including zero alpha and nonzero hidden RGB.
        row = bytes((i * 73 + i // 251) & 255 for i in range(pitch))
        data = bytearray(row * parent_height)
        for r in range(parent_height):
            data[r * pitch + x * 4 + 3] = r & 255
        original = bytes(data)
        offset = y * pitch + x * 4
        expected = b''.join(data[offset + r * pitch:offset + r * pitch + width * 4] for r in range(height))
        start = time.perf_counter()
        actual, out_pitch, did_copy = self.probe.run(data, width, height, pitch, offset)
        elapsed = time.perf_counter() - start
        self.assertEqual(actual, expected)
        self.assertEqual(bytes(data), original)
        self.assertEqual(did_copy, copied)
        self.assertEqual(out_pitch, width * 4 if copied else pitch)
        self.assertEqual(self.probe.outstanding(), 0)
        print('crop', (parent_width, parent_height, x, y, width, height), 'pitch', out_pitch, 'copy', copied, 'seconds', round(elapsed, 4))

    def test_heat_source_first_tile(self):
        self.check_crop(30003, 1444, 2, 2, 3750, 1440, True)

    def test_heat_source_nonzero_tile_and_final_row(self):
        self.check_crop(30003, 1444, 26250, 4, 3751, 1440, True)

    def test_ordinary_padded_rgba(self):
        self.check_crop(644, 484, 2, 2, 640, 480, False)

    def test_exact_threshold(self):
        self.check_crop(4096, 8, 1, 1, 16, 7, False)
        self.check_crop(4097, 8, 1, 1, 16, 7, True)

    def test_allocation_failure(self):
        with self.assertRaises(MemoryError):
            self.probe.run(bytearray(20000), 4, 1, 20000, 0, fail=True)
        self.assertEqual(self.probe.outstanding(), 0)

    def test_upload_exception_releases_buffer(self):
        with self.assertRaises(RuntimeError):
            self.probe.run(bytearray(20000), 4, 1, 20000, 0, upload_error=True)
        self.assertEqual(self.probe.outstanding(), 0)


if __name__ == '__main__':
    unittest.main()
