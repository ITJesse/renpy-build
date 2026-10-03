# RenPyLinter iOS runtime core

Each `renpylinter/<version>` branch starts at its corresponding official
`renpy/renpy-build` release tag. `tools/runtime/versions.json` records the
verified build-system and engine commits, historical environment declarations,
and version-specific dependencies. The SDL2 workflow remains on `renpylinter/sdl2`.

The runtime builder is a native macOS adaptation of the original Linux tasks.
It is not a claim of byte-for-byte identity with the official Ren'Py binaries.
The historical SDK 14.0 input is not available in this checkout. See
[the source/environment audit](docs/runtime-audit.md) for the exact differences.

## Build the selected version branch

The current 8.5.3 profile requires Xcode 16.4 (iOS SDK 18.5), Python 3.12.8,
and the tool versions in `tools/runtime/lock.json`. Homebrew commands below
install prerequisites; the builder rejects version drift rather than silently
accepting a different toolchain. Run on an arm64 Mac with a clean, committed checkout.

```sh
export DEVELOPER_DIR=/Applications/Xcode_16.4.app/Contents/Developer
brew install autoconf autoconf-archive automake libtool pkg-config
python3.12 -m venv tmp/runtime-tools
tmp/runtime-tools/bin/pip install Jinja2==3.1.4 MarkupSafe==2.1.5 requests==2.32.3 Cython==3.1.4 cmake==3.31.6 ninja==1.11.1.3
export PATH="$PWD/tmp/runtime-tools/bin:$PATH"
python3 tools/runtime/native.py
```

The builder uses the original `tmp/build`, `tmp/host`, `tmp/source`, and
`tmp/install.ios-*` locations. It does not change compiler/Xcode cache paths.
It refuses to reuse another commit's build intermediates or replace an
existing output bundle. Review generated files before any explicit cleanup.
No archive from the reference application is used as a newly built engine.

## Package contract

```text
dist/renpy-runtime-<version>/
  platforms/
    iphoneos-arm64/{librenpython.a,libpythonX.Y.a,librenpy.a,include/}
    iphonesimulator-arm64/{librenpython.a,libpythonX.Y.a,librenpy.a,include/}
  resources/
    main.py
    renpy/                   # including common scripts, fonts and assets
    lib/pythonX.Y/           # source-only stdlib and pinned companion modules
  licenses/
  build-info.json
  build-commands.json
  host-reference.json
  HOST-INTEGRATION.md
  SHA256SUMS
```

Device and simulator each contain only arm64. Target Python headers and
pyconfig.h are produced by that target's build. Resources contain no pyc/rpyc
from another interpreter. Native extension registration follows the tagged
upstream recipes; desktop-only optional CPython extensions are not promised.

`main.py` is the tagged upstream entry script. The application's adapted
main.py and helper_tool.rpy remain host-owned and must be deliberately retained
when implementing future Release consumption. The application is read-only in
this work; resource/native upgrades and rollbacks should use one complete bundle.

## Verification and publication

```sh
python3 tools/runtime/verify.py dist/renpy-runtime-8.5.3
```

Verification checks complete manifest coverage, arm64-only archives, platform
and minimum-OS commands, launcher/Python/module exports, target headers, and
required source resources. A separate closed-symbol dynamic-library link uses
the rebuilt dependencies, with a negative test proving undefined symbols fail.
Reference application dependency checks are recorded separately. None of these
checks establishes successful game execution or fixes restart lifecycle issues.

The Actions workflow supports branch pushes and manual dispatch on a version
branch. Publication is deliberately gated during initial validation. Once
enabled, the release job allocates a new `renpy-runtime-<version>-r<N>` tag,
creates it at the exact source commit, and refuses tag collisions. It publishes
the complete archive, outer SHA-256, build-info.json and the internal manifest.
Old tags and assets are never replaced.
