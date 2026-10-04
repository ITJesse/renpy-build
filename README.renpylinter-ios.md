# RenPyLinter iOS SDL3 build

Base: upstream renpy-build `master` at `16129650549f6ccef43d9eab9292701691844ca6`,
the renpy-build commit of the Ren'Py 8.6.0 nightlies. Ren'Py 8.6 links SDL
**3.4.8** (`tasks/sdl3.py`) with Ren'Py's `sdl3-opengl-ios.diff` and
`sdl3-metalangle.diff`. This branch keeps that version and those patches and
adds RenPyLinter's patches from `patches/renpylinter-sdl3/series`.

SDL3 is a separate library from the SDL2 layer (`renpylinter/sdl2`) used by
Ren'Py 8.5 and older. Both export `SDL_*` symbols with different ABIs, so one
application binary cannot statically link both.

## Orientation fix

`patches/renpylinter-sdl3/ios-scene-orientation.diff` adapts
[SDL #16355](https://github.com/libsdl-org/SDL/pull/16355)
(commit `114aca1727c755104ecfbd3bf4db986e0ff483e2`), which no `release-3.4.x`
tag contains up to 3.4.18, the same way `renpylinter/sdl2` adapted it:

- An existing SDL UIWindow's bounds are authoritative. The view frame is not
  rotated again using an interface orientation.
- Direction queries use the caller's UIWindowScene (or a foreground scene on
  the caller's screen), using effectiveGeometry on iOS 26+, interfaceOrientation
  on iOS 13–25, and the legacy status bar getter only before iOS 13. Unknown
  orientation never triggers a frame flip or display rotation.
- Each window refreshes its display modes from its own scene on layout, before
  its resize event; the scene delegate also follows effectiveGeometry changes.
- SDL3's UIKit driver already reports fullscreen dimensions itself
  (`VIDEO_DEVICE_CAPS_SENDS_FULLSCREEN_DIMENSIONS`), so SDL2's `SDL_video.c`
  change has no SDL3 counterpart.

## Build

On macOS with Xcode selected, CMake 3.28.3 and Ninja 1.11.1.4 (upstream's
versions) on `PATH`:

```sh
python3 tools/build_sdl3_ios.py
```

The script downloads the SDL 3.4.8 release tarball that `tasks/sdl3.py`
downloads and checks its sha256, applies upstream's two patches and then this
branch's series with `--fuzz=0`, and configures SDL exactly with
`tasks/sdl3.py`'s options (static, no camera, no tests). The flags mirror
renpy-build's iOS environment and the MetalANGLE annotator (`-DMETALANGLE`
against the checked-in MetalANGLE framework, which is not rebuilt), with
RenPyLinter's iOS 15.6 deployment target and `-Os` (`MinSizeRel`) in place of
upstream's iOS 13.0 and `-O3`. SDL's own subsystem selection is unchanged; the
enabled ones are recorded per slice in build-info.json.

Build intermediates are under `tmp/renpylinter-sdl3`; deliverables are under
`dist/renpylinter-sdl3`. The script refuses to overwrite a prior build.

The `Build SDL3 for RenPyLinter iOS` Actions workflow runs this script with
Xcode 26.6 on every push to `renpylinter/sdl3` (manual dispatch rebuilds a
selected revision) and publishes a GitHub Release tagged
`sdl3-ios-<run number>-<attempt>` at the exact source commit, containing
`renpylinter-sdl3-ios-arm64.tar.gz`, its SHA-256 checksum and build-info.json:

| Artifact | Platform |
| --- | --- |
| `release/libSDL3.a` | iOS device arm64 |
| `debug/libSDL3.a` | iOS Simulator arm64 |
| `build-info.json` | Source commit, toolchain, patch and input hashes, enabled subsystems |
| `SHA256SUMS` | Output file checksums |

## Required application verification

A successful build does not establish runtime compatibility. Before
distributing, run an 8.6 game on the target iPad simulator in landscape:
background/return, app switcher/return and repeated transitions, and check
that the UIWindow, SDL view, logical size, drawable and Ren'Py viewport stay
consistent. Older iOS must be tested separately.
