# RenPyLinter iOS runtime pipeline

This is the default branch of `ITJesse/renpy-build`. It contains no renpy-build
sources: only the workflows and scripts that build RenPyLinter's iOS runtime
from the `renpylinter/<version>` branches, gate the result and publish
immutable GitHub Releases.

## Layers

| Layer | Contents | Release |
| --- | --- | --- |
| Global | SDL2, FFmpeg (6 libraries) + aom, MetalANGLE | `sdl2-ios-*` (branch `renpylinter/sdl2`); FFmpeg and MetalANGLE are not rebuilt here |
| Family deps | OpenSSL, FreeType, HarfBuzz, FriBidi, libpng, libjpeg-turbo, libwebp, libavif, Brotli, bzip2, xz, zlib, libffi, libyuv, assimp, SDL2_image, SDL2main, mockrt | `deps-<family>-r<N>` |
| Engine | libpython, librenpy, librenpython, standard library pyc, Ren'Py pyc and common | `renpy-<version>-r<N>` |

`families.json` maps engine versions to families and records each family's
baseline branch, its dependency recipe hash (`tools/runtime/recipe.py`), the
task modules of both layers and the expected archive list.

## Branches

* `renpylinter/tooling` (this branch): `.github/workflows/{deps,engine}-ios.yml`,
  `tools/runtime/`, `families.json`.
* `renpylinter/<version>`: the upstream renpy-build tag plus
  `patches/renpylinter/` (with `series`), `renpylinter.lock.json` and minimal
  `tasks/` changes. Each branch's commits say why every change is needed.
* `renpylinter/sdl2`: the global SDL2 build.

## Running

Both workflows are manual (`workflow_dispatch`) and run from this branch:

```sh
gh workflow run deps-ios.yml   -f family=modern
gh workflow run engine-ios.yml -f version=8.5.3
```

`deps-ios.yml` builds the family's baseline branch. With `source_version` it
builds the layer from another member's branch for comparison only; such runs
never publish. `engine-ios.yml` builds `renpylinter/<version>` against the
deps release named in that branch's lock file.

Each build runs on `macos-26` with Xcode 26.6 (pinned in every lock file),
uploads the bundle as an artifact, and a Linux job re-verifies checksums,
creates `actions/attest-build-provenance` attestations and publishes the next
revision. Tags point at the engine-branch commit that was built and are never
moved or reused.

Local trial build of an engine branch checkout `SRC` (any Xcode, never
published):

```sh
python3 tools/runtime/driver.py prepare --src SRC
python3 tools/runtime/driver.py deps --family modern --src SRC --out out/deps \
    --sdl2 renpylinter-sdl2-ios-arm64.tar.gz --allow-xcode-mismatch
```

## How renpy-build runs on macOS

Upstream cross-compiles iOS on Ubuntu with clang/lld and SDK tarballs.
`tools/runtime/run_tasks.py` imports the branch's own `renpybuild` and
`tasks`, runs only the selected task modules for `ios-arm64` and
`ios-sim-arm64`, and installs `xcode_toolchain.py` on every context. That
keeps upstream's toolchain shape (`ccache clang -fuse-ld=lld
-Wno-unused-command-line-argument`) but with Xcode's
clang, `ar`/`ranlib` and SDKs, a `-target ...ios15.6` triple instead of
upstream's 13.0, and the SDK tarball step replaced by a link to Xcode's SDK.

The build host tools reproduce upstream's Ubuntu 24.04 host:

* CMake 3.28.3 and Ninja (`build-tools.txt`). Upstream's
  `tools/cmake_build_variables.cmake` changes compiler flags for CMake > 3.29.
* m4 1.4.19, autoconf 2.71, automake 1.16.5, libtool 2.4.7 and
  autoconf-archive 2022.09.03 built from verified GNU sources
  (`autotools.json`); tasks regenerate configure scripts and CPython 3.12
  requires autoconf 2.71.
* `config.sub` from Ubuntu's `autotools-dev` 20220109.1, which tasks copied
  from `/usr/share/misc/config.sub`.
* Host `PKG_CONFIG_PATH`, `CPATH`, `CFLAGS` and similar variables are removed
  so nothing from Homebrew reaches a target build.

## Bundles

`deps-<family>-ios.tar.gz`:

```
ios-arm64/{lib,include}/       device archives, headers (incl. SDL2/FFmpeg headers for engine builds)
ios-sim-arm64/{lib,include}/   simulator
link-check/<target>/           FFmpeg + aom from the same tree, used only by link gates
done-markers/                  renpy-build completion markers restored by engine builds
exports/<target>.txt           exported symbols per archive, for the no-removal gate
build-info.json  SHA256SUMS  LICENSES/
```

Absolute install paths in text files (pkg-config, headers) are replaced with
`@RPL_INSTALL_PREFIX@` and restored when an engine build unpacks the bundle.

`renpy-runtime-<version>-ios.tar.gz`:

```
lib/release/            device: libpython, librenpy, librenpython + the deps archives
lib/debug/              simulator, same set
python/lib/pythonX.Y/   standard library pyc (pythonlib task)
renpy/                  Ren'Py .py compiled to pyc, other files as in the tag,
                        common .rpyc/.rpymc from the official SDK
build-info.json  SHA256SUMS  LICENSES/
```

## Gates

A failing gate stops the build job; nothing is published.

Dependency layer:

* the archive set equals `families.json`;
* every archive is a single arm64 slice and every member's
  `LC_BUILD_VERSION` is iOS (device) or iOS Simulator with minos 15.6;
* every archive is `-force_load`ed into an empty executable for both SDKs with
  Apple's linker, resolving against the other archives, the SDL2 release,
  `link-check/` and system frameworks;
* exported symbols are compared with the previous release of the family;
  any removal fails;
* `__TEXT`/`__DATA`/`__bss` sizes per archive are recorded in build-info.

Engine:

* the deps tarball sha256 equals the lock; every deps archive in the build
  tree and in the bundle equals the deps `SHA256SUMS`;
* entry points (`launcher_main`, `launcher_main_wide`, `renpython_main`,
  `renpython_main_wide`; Python 2: the first and third);
* Python 3: `RenPyPythonSession` is present and, in each entry point, the
  Python main call is followed by three `PyMem_SetAllocator`,
  `malloc_zone_pressure_relief` and `malloc_destroy_zone`, with the allocator
  installed between `Py_PreInitialize*` and `Py_InitializeFromConfig`;
* the set of `renpy/**/*.pyc` equals the tag's `renpy/**/*.py`;
* the SDK's Python sources equal the tag's and its `vc_version` names the tag;
* the engine archives are `-force_load`ed into an empty executable that calls
  `launcher_main`, for both SDKs.
