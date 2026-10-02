# RenPyLinter iOS SDL2 build

Base: `renpy-8.5.3.26051504` (`7bfab40c1174f622f644b24669afd5fb167fbb79`).
The application currently links SDL **2.0.20**, including Ren'Py's MetalANGLE
changes. This branch keeps that version and the original patch series.

## Geometry fix

`patches/SDL2-2.0.20/ios-scene-geometry.diff` adapts
[SDL #16355](https://github.com/libsdl-org/SDL/pull/16355)
(commit `114aca1727c755104ecfbd3bf4db986e0ff483e2`) to SDL2.

- An existing SDL UIWindow's bounds are authoritative. The view frame must not
  be rotated again using UIApplication's status bar orientation.
- Direction queries use the matching UIWindowScene, using effectiveGeometry on
  iOS 26+, interfaceOrientation on iOS 13–25, and the legacy getter only before
  iOS 13. Unknown orientation does not trigger a frame flip or display rotation.
- `ios-window-size-events.diff` refreshes SDL display modes from the window scene
  on layout and prevents generic fullscreen code from overriding UIKit resize
  events with cached display dimensions.
- The existing layout path remains responsible for SDL resize events and drawable
  resizing. There is no foreground glViewport override or UIApplication swizzle.

## Build

On macOS with Xcode selected and `autoconf` installed:

```sh
python3 tools/build_sdl2_ios.py
```

The script uses the checked-in SDL source archive and **all** patches from
`patches/SDL2-2.0.20/series`. It uses Apple's native toolchain instead of setting
up the upstream Linux cross-compilation environment. MetalANGLE is taken from
this repository's existing source archives; it is not rebuilt. SDL's independent
Metal and Vulkan backends are disabled, matching the app's OpenGL/MetalANGLE path.
The deployment target is iOS 15.6, matching RenPyLinter.

Build intermediates are under `tmp/renpylinter-sdl2`; deliverables are under
`dist/renpylinter-sdl2`. The script refuses to overwrite a prior build. Review
and remove those generated directories explicitly before repeating a build.
No Xcode or compiler cache paths are changed. Only the `build/libSDL2.la`
target is compiled; SDL2main, SDL2_test, SDL_image and Ren'Py are excluded.

The `Build SDL2 for RenPyLinter iOS` Actions workflow runs this same script
automatically on every push to `renpylinter-ios`. Manual dispatch remains
available for rebuilding a selected revision. After a successful build, it publishes a GitHub Release tagged
`sdl2-ios-<run number>-<attempt>`, targeting the exact source commit. The release
contains `renpylinter-sdl2-ios-arm64.tar.gz`, its SHA-256 checksum, and build-info.json.
Actions artifacts are also retained. Archive contents:

| Artifact | Platform |
| --- | --- |
| `release/libSDL2.a` | iOS device arm64 |
| `debug/libSDL2.a` | iOS Simulator arm64 |
| `build-info.json` | Source commit, dirty state, SDK/toolchain, patch and input hashes |
| `SHA256SUMS` | Output file checksums |

The folder names follow the app's existing convention: platform-specific Xcode
link settings select these files independently of Debug/Release configuration.

## Required application verification

A successful build does not establish runtime compatibility. Before distributing
these libraries, run The Question on the target iPad simulator in landscape:
background/return, app switcher/return, and repeated transitions. Check that the
UIWindow, SDL view, logical size, drawable and Ren'Py viewport remain consistent.
Older iOS must be tested separately; a deployment target and availability guards
alone do not establish device compatibility. Other bundled engine versions also
share this SDL library and need launch regression coverage.
