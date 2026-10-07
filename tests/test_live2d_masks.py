"""Exercise the Live2D mask patches with the code they add.

Standard library only. The geometry test evaluates the patch's own reverse
matrix and mask shader expression; the selection test runs the patch's own
drawable classification. Cubism, Cython and GL are covered by the engine
build and by the signed iPad-on-Mac app.

eval/exec run only expressions and lines taken from this branch's own patch
files, the code under test; no external input reaches them.
"""
import pathlib
import random
import re
import textwrap
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PATCHES = ROOT / 'patches/renpylinter'
CANVAS = PATCHES / 'renpy-live2d-canvas-masks.diff'
MULTI = PATCHES / 'renpy-live2d-multi-mask-size.diff'
SKIP = PATCHES / 'renpy-live2d-skip-unused-drawables.diff'


def patched_lines(patch, path):
    """The new-file side (context and additions) of one file's hunks."""
    lines, inside = [], False
    for line in patch.read_text().splitlines():
        if line.startswith('+++ '):
            inside = line.endswith(path)
        elif inside and line.startswith('@@'):
            lines.append(None)  # Hunk boundary.
        elif inside and line[:1] in (' ', '+'):
            lines.append(line[1:])
    return lines


def added(patch):
    return [line[1:] for line in patch.read_text().splitlines()
            if line.startswith('+') and not line.startswith('+++')]


def removed(patch):
    return [line[1:] for line in patch.read_text().splitlines()
            if line.startswith('-') and not line.startswith('---')]


class Matrix(object):
    """renpy.display.matrix.Matrix: row-major 4x4 applied to column vectors."""

    def __init__(self, m):
        self.m = list(m)

    @staticmethod
    def offset(x, y, z):
        return Matrix([1, 0, 0, x, 0, 1, 0, y, 0, 0, 1, z, 0, 0, 0, 1])

    @staticmethod
    def scale(x, y, z):
        return Matrix([x, 0, 0, 0, 0, y, 0, 0, 0, 0, z, 0, 0, 0, 0, 1])

    def __mul__(self, other):
        a, b = self.m, other.m
        return Matrix([sum(a[r * 4 + k] * b[k * 4 + c] for k in range(4))
                       for r in range(4) for c in range(4)])

    def transform(self, x, y):
        m = self.m
        return (m[0] * x + m[1] * y + m[3], m[4] * x + m[5] * y + m[7])


class Vec2(object):
    def __init__(self, x, y):
        self.x, self.y = float(x), float(y)

    @property
    def xy(self):
        return Vec2(self.x, self.y)

    def _op(self, other, f):
        if isinstance(other, Vec2):
            return Vec2(f(self.x, other.x), f(self.y, other.y))
        return Vec2(f(self.x, other), f(self.y, other))

    def __add__(self, other):
        return self._op(other, lambda a, b: a + b)

    def __mul__(self, other):
        return self._op(other, lambda a, b: a * b)

    def __truediv__(self, other):
        return self._op(other, lambda a, b: a / b)


@unittest.skipUnless(CANVAS.exists(), 'canvas mask backport not in this branch')
class CanvasMaskTests(unittest.TestCase):
    def test_square_renders_are_gone(self):
        self.assertTrue(any('Render(ppu * 2, ppu * 2)' in line for line in removed(CANVAS)))
        code = [line for line in added(CANVAS) if not line.strip().startswith('#')]
        self.assertFalse(any('ppu * 2' in line for line in code))
        self.assertTrue(any('rv.subpixel_blit(t[1], (0, 0))' in line for line in added(CANVAS)))

    def test_drawables_keep_position_and_masks_sample_the_same_point(self):
        code = added(CANVAS)
        offset_x = next(line for line in code if 'offset_x = ' in line).split('=', 1)[1].strip()
        offset_y = next(line for line in code if 'offset_y = ' in line).split('=', 1)[1].strip()
        reverse = next(line for line in code if 'reverse = ' in line).split('=', 1)[1].strip()
        shader = [line.strip().rstrip(';') for line in code if 'v_mask_coord' in line]
        # Both mask shaders (normal and inverted) get the same mapping.
        self.assertEqual(len(shader), 4)
        self.assertEqual(shader[:2], shader[2:])

        rng = random.Random(7383)
        for _ in range(200):
            ppu = rng.uniform(100, 4000)
            w = int(rng.uniform(.3, 2.5) * ppu)
            h = int(rng.uniform(.3, 2.5) * ppu)
            x, y = rng.uniform(-1.2, 1.2), rng.uniform(-1.2, 1.2)
            ns = {'Matrix': Matrix, 'w': w, 'h': h, 'ppu': ppu}
            ns['offset_x'] = eval(offset_x, ns)
            ns['offset_y'] = eval(offset_y, ns)
            new = eval(reverse, ns)

            # Before: a ppu * 2 square render, blitted at (w/2 - ppu, h/2 - ppu).
            old = Matrix([ppu, 0, 0, ppu, 0, -ppu, 0, ppu, 0, 0, 1, 0, 0, 0, 0, 1])
            ox, oy = old.transform(x, y)
            nx, ny = new.transform(x, y)
            self.assertAlmostEqual(nx, ox + w / 2.0 - ppu, places=6)
            self.assertAlmostEqual(ny, oy + h / 2.0 - ppu, places=6)

            # The mask render shares the drawable's matrix; the shader must
            # address the texel where the mask drew this model point.
            env = {'a_position': Vec2(x, y), 'u_live2d_ppu': ppu,
                   'u_live2d_offset': Vec2(ns['offset_x'], ns['offset_y']),
                   'u_model_size': Vec2(w, h)}
            name, expression = shader[0].split('=', 1)
            env['v_mask_coord'] = eval(expression, env)
            exec(shader[1], env)
            v = env['v_mask_coord']
            self.assertAlmostEqual(v.x * w, nx, places=6)
            self.assertAlmostEqual(v.y * h, ny, places=6)

            # The old square mapping satisfied the same invariant.
            self.assertAlmostEqual((x / 2.0 + .5) * 2 * ppu, ox, places=6)
            self.assertAlmostEqual((-y / 2.0 + .5) * 2 * ppu, oy, places=6)


@unittest.skipUnless(MULTI.exists(), 'multi-mask fix not in this branch')
class MultiMaskTests(unittest.TestCase):
    def test_multi_mask_render_is_canvas_sized(self):
        self.assertEqual([l.strip() for l in removed(MULTI)], ['m = renpy.display.render.Render(ppu * 2, ppu * 2)'])
        self.assertEqual([l.strip() for l in added(MULTI)], ['m = renpy.display.render.Render(w, h)'])


@unittest.skipUnless(SKIP.exists(), 'unused drawable skipping not in this branch')
class SkipUnusedTests(unittest.TestCase):
    VISIBLE = 1

    def classify(self, flags, opacities, masks):
        lines = patched_lines(SKIP, 'renpy/gl2/live2dmodel.pyx')
        start = next(i for i, l in enumerate(lines) if l and 'used_masks = set()' in l)
        end = next(i for i, l in enumerate(lines) if l and 'is_mask = i in used_masks' in l)
        block = lines[start:end + 1]
        self.assertNotIn(None, block)  # One contiguous hunk.
        indent = re.match(r' *', block[-1]).group()
        block.append(indent + 'result.append((is_visible, is_mask))')
        source = textwrap.dedent('\n'.join(block))
        source = re.sub(r'for 0 <= (\w+) < ([^:]+):', r'for \1 in range(\2):', source)

        class Model(object):
            pass

        model = Model()
        model.drawable_count = len(flags)
        model.drawable_dynamic_flags = flags
        model.drawable_opacities = opacities
        model.drawable_mask_counts = [len(m) for m in masks]
        model.drawable_masks = masks
        env = {'self': model, 'csmIsVisible': self.VISIBLE, 'result': []}
        exec(source, env)
        return [bool(v) or bool(m) for v, m in env['result']], env['result']

    def test_hidden_unused_drawables_get_no_render(self):
        # 0 visible, masked by 1; 1 hidden mask; 2 hidden, unused;
        # 3 visible but zero opacity, masked by 4; 4 hidden.
        needed, result = self.classify([1, 0, 0, 1, 0], [1.0, 1.0, 1.0, 0.0, 1.0],
                                       [[1], [], [], [4], []])
        self.assertEqual(needed, [True, True, False, False, False])
        self.assertTrue(result[0][0])
        self.assertFalse(result[1][0])
        self.assertTrue(result[1][1])
        self.assertFalse(result[3][0])  # Zero opacity draws nothing.

    def test_masks_of_masks(self):
        # 0 visible, masked by 1; 1 hidden, itself masked by 2; 2 hidden.
        needed, _ = self.classify([1, 0, 0], [1.0, 1.0, 1.0], [[1], [2], []])
        legacy = 'A 7.5/8.0 mask is the masking drawable' in SKIP.read_text()
        # 7.5/8.0 mask with the drawable's own Render (masks included);
        # later engines with a separate mask Render that carries none.
        self.assertEqual(needed, [True, True, legacy])

    def test_masked_loop_skips_missing_renders(self):
        self.assertIn('if r is None:', '\n'.join(added(SKIP)))


if __name__ == '__main__':
    unittest.main()
