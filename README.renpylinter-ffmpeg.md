# RenPyLinter iOS FFmpeg build

Base: `renpy-8.5.3.26051504` (`7bfab40c1174f622f644b24669afd5fb167fbb79`), the
same renpy-build commit as `renpylinter/sdl2`.

RenPyLinter embeds one `FFmpeg.framework`. All eight engines (7.5.3 to 8.5.3)
and the app's own audio code link it. Every engine branch builds the same
FFmpeg 4.3.1 and aom v3.5.0 (`tasks/ffmpeg.py`, `tasks/aom.py`); the app
previously shipped a copy of upstream Ren'Py's renios build of them, compiled
at `-O3`. This branch builds that configuration from source with Xcode,
optimized for size like the rest of the runtime.

## Build

On macOS with Xcode, CMake, pkg-config and git:

```sh
python3 tools/build_ffmpeg_ios.py
```

* FFmpeg: `source/ffmpeg-4.3.1.tar.gz` (sha256 checked), the patches
  `tasks/ffmpeg.py` applies (`ffmpeg-4.3.1-sse.diff`,
  `ffmpeg-4.3.1-ff_seek_frame_binary.diff`), and its iOS configure options
  with two changes: `--optflags=-Os` instead of FFmpeg's default `-O3`, and
  `--disable-programs` (the `ffmpeg`/`ffplay` executables are not shipped).
  `--enable-small` is not used: it also changes behavior (no long codec
  names, slower code paths).
* aom: tag `v3.5.0`, checked to be commit `bcfe6fbf`, with `tasks/aom.py`'s
  options (decoder only, no runtime CPU detection on iOS), built as CMake
  `MinSizeRel`.
* Both for `arm64-apple-ios15.6` (device) and
  `arm64-apple-ios15.6-simulator`.

Gates (the build stops on failure): every archive is a single arm64 slice
whose members all carry `LC_BUILD_VERSION` for the right platform with minos
15.6, and the six archives force-loaded into a dylib link against the SDK
alone (as `FFmpeg.framework` does).

Build intermediates are under `tmp/renpylinter-ffmpeg`; deliverables under
`dist/renpylinter-ffmpeg`. The script refuses to overwrite a prior build.

## Release

`.github/workflows/ffmpeg-ios.yml` runs the script on every push to
`renpylinter/ffmpeg` (and on manual dispatch) on `macos-26` with Xcode 26.6,
creates a build provenance attestation for the archive and publishes
`ffmpeg-ios-<run number>-<attempt>` at the built commit, containing
`renpylinter-ffmpeg-ios-arm64.tar.gz`:

| Path | Content |
| --- | --- |
| `release/lib{aom,avcodec,avformat,avutil,swresample,swscale}.a` | iOS device arm64 |
| `debug/…` | iOS Simulator arm64 |
| `include/` | public headers (device build) |
| `LICENSES/` | FFmpeg (LGPL 2.1) and aom licenses |
| `build-info.json` | source commit, dirty state, Xcode, versions, options, patches, per-slice member counts |
| `SHA256SUMS` | every other file |

The app pins the release in `scripts/engines.lock.json` (`ffmpeg`) and
`scripts/fetch_engines.py` installs it into `FFmpeg/Libraries`.
