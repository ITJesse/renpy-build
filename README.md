# RenPyLinter iOS runtime pipeline

This is the default branch of `ITJesse/renpy-build`. It contains no renpy-build
sources: only the workflows and scripts that build RenPyLinter's iOS runtime
from the `renpylinter/<version>` branches, gate the result and publish
immutable GitHub Releases.

## Layers

| Layer | Contents | Release |
| --- | --- | --- |
| Global | SDL2 (up to 8.5) or SDL3 (8.6), FFmpeg (6 libraries) + aom, MetalANGLE | `sdl2-ios-*` (branch `renpylinter/sdl2`), `sdl3-ios-*` (branch `renpylinter/sdl3`); FFmpeg and MetalANGLE are not rebuilt here |
| Family deps | OpenSSL, FreeType, HarfBuzz (+subset, +cairo stub), FriBidi, libpng, libjpeg-turbo, libwebp (+demux, +mux, sharpyuv), libavif, Brotli, bzip2, xz, zlib, libffi, libyuv, assimp (modern, sdl3), SDL2_image + SDL2main or SDL3_image, mockrt | `deps-<family>-r<N>` |
| Engine | libpython, librenpy, librenpython, standard library bytecode, Ren'Py bytecode and common | `renpy-<version>-r<N>` |

`families.json` maps engine versions to families and records each family's
baseline branch, its dependency recipe hash (`tools/runtime/recipe.py`), the
task modules of both layers, the expected archive list and the reviewed
system symbols (see Gates).

| Family | Baseline | Engines |
| --- | --- | --- |
| sdl3 | `renpylinter/8.6.0` (Ren'Py nightly) | 8.6.0 |
| modern | `renpylinter/8.5.3` | 8.4.1, 8.5.3 |
| legacy | `renpylinter/8.3.7` | 7.8.2, 8.1.3, 8.2.3, 8.3.7, and 7.5.3, 8.0.3 (ancient, see below) |

The legacy layer was built from 7.8.2 and from 8.3.7 and compared: apart from
SDL's `SDL_revision.h` (the enclosing git commit; SDL is not shipped) and
OpenSSL's build timestamp in `cversion.o`, all 1143 files were identical. The
ancient engines (7.5.3, 8.0.3) link against the legacy layer and pass every
gate, so no separate ancient layer exists.

## Branches

* `renpylinter/tooling` (this branch): `.github/workflows/{deps,engine,nightly}-ios.yml`,
  `tools/runtime/`, `families.json`.
* `renpylinter/<version>`: the upstream renpy-build tag plus
  `patches/renpylinter/` (with `series`), `renpylinter.lock.json` and minimal
  `tasks/`/`source/` changes. Each commit says why its change is needed.
* `renpylinter/sdl2`: the global SDL2 build (Ren'Py up to 8.5).
* `renpylinter/sdl3`: the global SDL3 build (Ren'Py 8.6).

The engine branch's lock names its global SDL layer: `sdl` (`library`,
`release`, `asset`, `sha256`), or `sdl2` in locks up to 8.5. SDL2 and SDL3
both export `SDL_*` symbols, so an application links one of them.

### Nightly engines (8.6.0)

Ren'Py 8.6.0 is not released. `renpylinter/8.6.0` is based on the
renpy-build master commit a Ren'Py nightly was built from, and is rebased
onto the newest nightly for every build. Its lock has `renpy_build_ref` and
`renpy_build_commit` instead of `renpy_build_tag`, `renpy_version` (the
nightly's vc_version) instead of `renpy_tag`, and a `prerelease` block; its
releases are GitHub prereleases. Differences from released engines:

* Ren'Py no longer tracks `uv.lock`; the branch carries a pinned one
  (`renpy_uv_lock`), which `prepare` installs as `renpy/uv.lock`. Tasks run
  `uv` with `UV_FROZEN=1`, so they never re-lock it.
* Nightly SDKs ship no `LICENSE.txt`; `renpy_sdk.license` names the Ren'Py
  file it is made from (`sphinx/source/license.rst`).
* The `sdl3` family has no fixed `recipe_sha256` in families.json. The engine
  build instead requires the deps release's recipe to equal its checkout's.
* Upstream master downloads some sources at build time (`tmp/tars`); their
  sha256 values are recorded in build-info.json (`downloads`).

`nightly-ios.yml` does a whole nightly round:

```sh
gh workflow run nightly-ios.yml -f version=8.6.0            # the newest nightly
gh workflow run nightly-ios.yml -f version=8.6.0 -f build=8.6.0.26100401+nightly.dirty
```

1. `tools/runtime/nightly.py bump` finds the newest nightly that has an SDK
   and an iOS build, rebases the branch's own commits onto the renpy-build
   commit it was built from (a conflict stops the round), locks the
   nightly's SDK, renios and Ren'Py commit, regenerates the pinned uv.lock
   (as of the Ren'Py commit, with the package versions the nightly SDK
   ships) and commits; the branch is force-pushed with a lease.
2. If the locked deps release was built from another recipe, `deps-ios.yml`
   rebuilds and publishes the layer and `nightly.py link-deps` commits it
   to the lock.
3. `engine-ios.yml` builds that exact commit and publishes the prerelease,
   unless an engine release already points at it.

Earlier engine releases keep their tags, which hold the commits they were
built from after the branch moves on.

`series` lists every patch with where it applies: `root` (applied to the
checkout by the driver, e.g. the librenpython session zone) or
`task:<module>` (applied by that task to its unpacked source; the driver
checks the task really references it).

## Running

Both workflows are manual (`workflow_dispatch`) and run from this branch:

```sh
gh workflow run deps-ios.yml   -f family=modern
gh workflow run engine-ios.yml -f version=8.5.3
```

`deps-ios.yml` builds the family's baseline branch. With `source_version` it
builds the layer from another member's branch for comparison only; such runs
are never published. `engine-ios.yml` builds `renpylinter/<version>` against
the deps release named in that branch's lock file.

Each build runs on `macos-26` with Xcode 26.6 (pinned in every lock file) and
a ccache cache, uploads the bundle as an artifact, and a Linux job re-verifies
the checksums, creates an `actions/attest-build-provenance` attestation and
publishes the next revision. Tags point at the engine-branch commit that was
built and are never moved or reused. Releases built with another Xcode or as
trials are refused.

Inputs an engine build fetches, all pinned by sha256 in the lock:

* the deps release and the `sdl2-ios-*` or `sdl3-ios-*` release;
* the official Ren'Py SDK (`renpy-<v>-sdk.tar.bz2`, or a nightly's): its
  `renpy/` Python sources must equal the tag's (or commit's), and it provides
  the compiled common scripts (`.rpyc/.rpymc`), `vc_version.py` and the
  license text. Python sources the SDK ships but git does not track are
  generated by Ren'Py's `setup.py` (8.6: `renpy/styledata/stylesets.py`); the
  engine build must generate each of them identically and bundles them;
* upstream's official `renpy-<v>-renios.zip`, only as the baseline of the
  system symbol gate;
* `Live2DCubismCore.h` (Cubism SDK 5-r.5) from the `LIVE2D_CUBISM_CORE_H`
  repository secret, for compiling `renpy.gl2.live2dmodel`. It is proprietary
  and never bundled.

Local trial build (any Xcode; bundles are marked `trial` and never released):

```sh
python3 tools/runtime/driver.py prepare --src SRC
python3 tools/runtime/driver.py deps --family modern --src SRC --out out/deps \
    --sdl renpylinter-sdl2-ios-arm64.tar.gz --upstream-renios renpy-8.5.3-renios.zip --trial
python3 tools/runtime/driver.py engine --version 8.5.3 --src SRC --out out/engine \
    --deps deps-modern-ios.tar.gz --sdl renpylinter-sdl2-ios-arm64.tar.gz \
    --sdk renpy-8.5.3-sdk.tar.bz2 --live2d-header Live2DCubismCore.h \
    --upstream-renios renpy-8.5.3-renios.zip --trial
```

## How renpy-build runs on macOS

Upstream cross-compiles iOS on Ubuntu with clang/lld and SDK tarballs.
`tools/runtime/run_tasks.py` imports the branch's own `renpybuild`
(`renpybuild.task` for 7.8/8.1+, `renpybuild.model` for 7.5/8.0) and
`tasks`, runs only the selected task modules for `ios-arm64` and
`ios-sim-arm64`, and installs `xcode_toolchain.py` on every context. That
keeps upstream's toolchain shape (`ccache clang -fuse-ld=lld
-Wno-unused-command-line-argument`) with Xcode's clang, `ar`/`ranlib` and
SDKs, a `-target ...ios15.6` triple instead of upstream's 13.0, and the SDK
tarball step replaced by a link to Xcode's SDK.

Every version is optimised for size: `xcode_toolchain.py` replaces upstream's
`-O3` (in `CFLAGS`/`LDFLAGS`, or in `CC`/`CXX` for 7.5 and 8.0) with `-Os` in
`CFLAGS`, `CXXFLAGS` and `LDFLAGS`, and builds CMake projects as `MinSizeRel`
instead of `Release`, whose `-O3` would follow `CFLAGS`. Python 2.7's
Makefile puts its `OPT` (`-O3`) after `CFLAGS`, so `-Os` is also passed in
`EXTRA_CFLAGS`, which CPython appends last. FFmpeg keeps its own `-O3`; its
archives only serve the link gates.
Upstream 8.6 archives with llvm-ar as `ar --format=darwin`; `AR` is
`tools/runtime/ar-darwin`, which drops that option (the Darwin format is the
only one Apple's ar writes) and runs Xcode's ar. 8.6 also sets `OBJC`,
`OBJCFLAGS` and `INSTALL_NAME_TOOL`, which get Xcode's tools, the iOS 15.6
target and `-Os` like their C counterparts.
Apple's clang, unlike upstream's, passes the last `-O` option on to the linker,
and `ld64.lld` rejects `-Os`; the driver therefore puts a small `ld64.lld`
wrapper on the task `PATH` that drops the levels lld cannot parse (lld's `-O`
only affects bind opcode compaction in linked images, not archived objects).

The build host reproduces upstream's Ubuntu host where it matters:

* pinned tools (`build-tools.txt`, `autotools.json`): CMake 3.28.3 (upstream's
  `tools/cmake_build_variables.cmake` changes flags above 3.29), GNU make 4.3
  (Apple's make 3.81 compares whole seconds and skips CPython's makesetup),
  GNU sed 4.9, m4 1.4.19, autoconf 2.71 (CPython 3.12 requires it), automake
  1.16.5, libtool 2.4.7, autoconf-archive 2022.09.03, plus Ubuntu's
  `config.sub` and `pkg.m4`, all from verified sources;
* tasks see only the build venv, the pinned tools, pkg-config/ccache/lld and
  the system directories; host `PKG_CONFIG_PATH`, `CPATH`, `CFLAGS` and the
  like are removed, and target pkg-config only sees the target install tree;
* the host Python (a build tool) gets Homebrew's xz, OpenSSL and libffi in
  place of Ubuntu's `liblzma-dev`, `libssl-dev`, `libffi-dev`.

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
python/lib/pythonX.Y/   standard library bytecode (pythonlib task)
renpy/                  the tag's renpy/: .py compiled (.pyc, or .pyo for Python 2),
                        other files as in the tag, common .rpyc/.rpymc and
                        vc_version from the official SDK
build-info.json  SHA256SUMS  LICENSES/
```

## Gates

A failing gate stops the build job; nothing is published.

Dependency layer:

* the archive set equals `families.json` (archives of the global layer,
  `libSDL2*.a`/`libSDL3*.a` and FFmpeg, are excluded);
* every archive is a single arm64 slice and every member's
  `LC_BUILD_VERSION` is iOS (device) or iOS Simulator with minos 15.6;
* every archive is `-force_load`ed into an empty executable for both SDKs with
  Apple's linker, resolving against the other archives, the SDL2 release,
  `link-check/` and system frameworks;
* exported symbols are compared with the previous release of the family;
  any removal fails;
* system symbols (strong and weak) the archives need must also be needed by
  upstream's official renios build, or be listed in `reviewed_system_imports`
  with evidence that they exist on iOS 15.6 (configure link tests ignore the
  deployment target; this caught `dup3`/`pipe2`);
* `__TEXT`/`__DATA`/`__bss` sizes per archive are recorded in build-info.

Engine:

* the deps tarball sha256 equals the lock; every deps archive in the build
  tree and in the bundle equals the deps `SHA256SUMS`;
* entry points (`launcher_main`, `launcher_main_wide`, `renpython_main`,
  `renpython_main_wide`; Python 2: the first and third);
* Python 3: `RenPyPythonSession` is present; in each entry point the Python
  main call is followed by three `PyMem_SetAllocator`,
  `malloc_zone_pressure_relief` and `malloc_destroy_zone`; and the zone is
  created after `Py_PreInitialize*` and before Python initializes (calls into
  the archive's own functions are followed);
* `Live2DCubismCore.h` matches the declarations `live2dmodel` uses (Cubism 4
  values, static assertions);
* the set of `renpy/**/*.pyc` (or `.pyo`) equals the tag's `renpy/**/*.py`
  plus `vc_version`; the SDK's Python sources equal the tag's;
* the engine archives are `-force_load`ed into an empty executable that calls
  `launcher_main`, for both SDKs;
* the system symbol gate as for the dependency layer, over the whole bundle.
