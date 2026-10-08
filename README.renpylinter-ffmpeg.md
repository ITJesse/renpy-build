# RenPyLinter iOS FFmpeg build

Base: `renpy-8.5.3.26051504` (`7bfab40c1174f622f644b24669afd5fb167fbb79`), the
same renpy-build commit as `renpylinter/sdl2`.

RenPyLinter embeds one `FFmpeg.framework`. It is the app's only video and audio
decoding pipeline: all eight engines (7.5.3 to 8.5.3) play movies and sound
through it (Ren'Py's `ffmedia.c`), and the app's asset library previews and
audio code use it directly. The deps layer's libavif links libaom from it.

## Configuration

FFmpeg 9.0.2, extending renpy-build's `tasks/ffmpeg.py` (FFmpeg 4.3.1):

* Everything Ren'Py supports: WebM/Matroska, Ogg, MP4/MOV, AVI, MPEG-PS/TS,
  FLAC, MP3, WAV, AU; VP9, VP8, Theora, MPEG-1/2/4 part 2, MS-MPEG4, AV1;
  Vorbis, Opus, MP3, MP2, FLAC, PCM.
* Also H.264, HEVC, H.263 and AAC (with LATM): games made for Ren'Py's old
  mobile hardware video path may ship MP4 movies.
* VideoToolbox hwaccels for H.263, H.264, HEVC, MPEG-1/2/4, VP9 and AV1. A
  decoder uses one when the device has a hardware decoder for the stream and
  falls back to software otherwise (the engines choose, see their
  `ffmedia.c` patch).
* AV1 in software with dav1d 1.5.4 (minsize); the native `av1` decoder only
  drives VideoToolbox. libaom v3.5.0 (decoder only, MinSizeRel) is built and
  shipped for libavif, not enabled in FFmpeg.
* `--optflags=-Os`, `--disable-programs`; no libavfilter (only the `ffmpeg`
  program used it); libavresample no longer exists.

The two patches `tasks/ffmpeg.py` applies to 4.3.1 are upstream fixes 9.0.2
already has. The source tarball is `source/ffmpeg-9.0.2.tar.xz` from
ffmpeg.org, whose signature by the FFmpeg release signing key
(`FCF986EA15E6E293A5644F10B4322F04D67658D8`) was checked when it was added;
the build checks its sha256.

## Build

On macOS with Xcode, CMake, Meson, Ninja, pkg-config and git:

```sh
python3 tools/build_ffmpeg_ios.py
```

Both for `arm64-apple-ios15.6` (device) and `arm64-apple-ios15.6-simulator`.

Gates (the build stops on failure): every archive is a single arm64 slice
whose members all carry `LC_BUILD_VERSION` for the right platform with minos
15.6, and the seven archives force-loaded into a dylib link against the SDK
and the frameworks VideoToolbox decoding needs (VideoToolbox, CoreMedia,
CoreVideo, CoreFoundation, QuartzCore), as `FFmpeg.framework` does.

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
| `release/lib{aom,avcodec,avformat,avutil,dav1d,swresample,swscale}.a` | iOS device arm64 |
| `debug/…` | iOS Simulator arm64 |
| `include/` | public headers of FFmpeg and libaom (device build) |
| `LICENSES/` | FFmpeg (LGPL 2.1), libaom and dav1d licenses |
| `build-info.json` | source commit, dirty state, Xcode, versions, options, frameworks, per-slice member counts |
| `SHA256SUMS` | every other file |

The app pins the release in `scripts/engines.lock.json` (`ffmpeg`) and
`scripts/fetch_engines.py` installs it into `FFmpeg/Libraries`. The deps and
engine builds of `renpylinter/tooling` install the same release (pinned in
each engine branch's `renpylinter.lock.json`) instead of building FFmpeg.
