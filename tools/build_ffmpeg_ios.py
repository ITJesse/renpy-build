#!/usr/bin/env python3
"""Build RenPyLinter's FFmpeg (with dav1d and libaom) for iOS on macOS, for size.

The app embeds one FFmpeg.framework that every engine (7.5.3 to 8.5.3) and the
app itself link; it is the only video and audio decoding pipeline of the app.
Beyond the formats Ren'Py supports (renpy-build's tasks/ffmpeg.py), it decodes
H.264, HEVC and AAC, uses VideoToolbox for the codecs a device has a hardware
decoder for, and decodes AV1 in software with dav1d. libaom (decoder only)
ships alongside for the deps layer's libavif (AVIF images), which links it from
FFmpeg.framework; FFmpeg itself does not use it. Built natively with Xcode at
-Os (dav1d: minsize, aom: MinSizeRel), for the device and the arm64 simulator.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'tmp' / 'renpylinter-ffmpeg'
OUT = ROOT / 'dist' / 'renpylinter-ffmpeg'

FFMPEG_VERSION = '9.0.2'
# ffmpeg-9.0.2.tar.xz from ffmpeg.org, signed by the FFmpeg release signing key
# FCF986EA15E6E293A5644F10B4322F04D67658D8 (signature checked when added).
FFMPEG_SHA256 = '8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e'
# The two patches tasks/ffmpeg.py applies to 4.3.1 are upstream fixes that
# 9.0.2 already has.
FFMPEG_PATCHES = []
AOM_URL = 'https://aomedia.googlesource.com/aom'
AOM_TAG = 'v3.5.0'
AOM_COMMIT = 'bcfe6fbfed315f83ee8a95465c654ee8078dbff9'
DAV1D_URL = 'https://code.videolan.org/videolan/dav1d.git'
DAV1D_TAG = '1.5.4'
DAV1D_COMMIT = '54706fc6bc0cdecab7e9593974a4039cc038fca7'

DEPLOYMENT_TARGET = '15.6'
OPTIMIZATION = '-Os'
SLICES = [('iphoneos', 'release', 'IOS', ''), ('iphonesimulator', 'debug', 'IOSSIMULATOR', '-simulator')]
# otool prints LC_BUILD_VERSION platforms by name or by number, depending on the Xcode.
PLATFORM_IDS = {'IOS': {'IOS', '2'}, 'IOSSIMULATOR': {'IOSSIMULATOR', '7'}}
LIBRARIES = ['libaom.a', 'libavcodec.a', 'libavformat.a', 'libavutil.a', 'libswresample.a', 'libswscale.a',
             'libdav1d.a']
# Headers installed but not shipped: dav1d's are internal to libavcodec.
UNSHIPPED_HEADERS = {'dav1d'}
# What VideoToolbox decoding links (FFmpeg's configure: videotoolbox_deps,
# videotoolbox_hwaccel_extralibs); FFmpeg.framework links the same.
FRAMEWORKS = ['VideoToolbox', 'CoreMedia', 'CoreVideo', 'CoreFoundation', 'QuartzCore']

# tasks/ffmpeg.py's iOS configuration, extended:
# * H.264, HEVC, AAC (and LATM AAC) decoders and parsers: games made for the
#   old mobile hardware video path may ship MP4 movies.
# * VideoToolbox hwaccels for every codec FFmpeg has one for; the decoders
#   fall back to software when the device has no hardware decoder.
# * AV1: dav1d for software decoding, the native av1 decoder (hwaccel only)
#   for VideoToolbox. libaom is not used by FFmpeg (see above).
# * --optflags (size instead of -O3), --disable-programs; no libavfilter (only
#   the ffmpeg program used it). libavresample no longer exists.
DEMUXERS = ['au', 'avi', 'flac', 'm4v', 'matroska', 'mov', 'mp3', 'mpegps', 'mpegts', 'mpegtsraw',
            'mpegvideo', 'ogg', 'wav', 'av1', 'aac']
DECODERS = ['flac', 'mp2', 'mp3', 'mp3on4', 'mpeg1video', 'mpeg2video', 'mpegvideo', 'msmpeg4v1',
            'msmpeg4v2', 'msmpeg4v3', 'mpeg4', 'h263', 'pcm_dvd', 'pcm_s16be', 'pcm_s16le', 'pcm_s8',
            'pcm_u16be', 'pcm_u16le', 'pcm_u8', 'theora', 'vorbis', 'opus', 'vp3', 'vp8', 'vp9',
            'h264', 'hevc', 'aac', 'aac_latm', 'av1', 'libdav1d']
PARSERS = ['mpegaudio', 'mpegvideo', 'mpeg4video', 'h263', 'vp3', 'vp8', 'vp9', 'av1', 'h264', 'hevc',
           'aac', 'aac_latm']
HWACCELS = ['h263_videotoolbox', 'h264_videotoolbox', 'hevc_videotoolbox', 'mpeg1_videotoolbox',
            'mpeg2_videotoolbox', 'mpeg4_videotoolbox', 'vp9_videotoolbox', 'av1_videotoolbox']
FFMPEG_OPTIONS = [
    '--enable-pic', '--enable-static', '--disable-all', '--disable-everything', '--enable-cross-compile',
    '--enable-runtime-cpudetect',
    '--enable-avcodec', '--enable-avformat', '--enable-swresample', '--enable-swscale',
    '--enable-libdav1d', '--enable-videotoolbox', '--disable-bzlib', '--disable-doc',
    *[f'--enable-demuxer={d}' for d in DEMUXERS], *[f'--enable-decoder={d}' for d in DECODERS],
    *[f'--enable-parser={p}' for p in PARSERS], *[f'--enable-hwaccel={h}' for h in HWACCELS],
    '--disable-iconv', '--disable-alsa', '--disable-libxcb', '--disable-lzma', '--disable-sndio',
    '--disable-xlib', '--disable-amf', '--disable-audiotoolbox', '--disable-cuda-llvm', '--disable-d3d11va',
    '--disable-dxva2', '--disable-ffnvcodec', '--disable-nvdec', '--disable-nvenc', '--disable-v4l2-m2m',
    '--disable-vaapi', '--disable-vdpau', '--disable-vulkan', '--disable-securetransport',
    f'--optflags={OPTIMIZATION}', '--disable-programs',
]
# tasks/aom.py, as MinSizeRel.
AOM_OPTIONS = ['-DCONFIG_AV1_ENCODER=0', '-DENABLE_EXAMPLES=0', '-DENABLE_TOOLS=0', '-DENABLE_TESTS=0',
               '-DENABLE_DOCS=0', '-DCONFIG_PIC=1', '-DCONFIG_RUNTIME_CPU_DETECT=0', '-DAOM_TARGET_CPU=arm64']
# dav1d: decoder library only, 8- and 10/12-bit, smallest code (its assembly
# does the work).
DAV1D_OPTIONS = ['--buildtype=minsize', '-Ddefault_library=static', '-Denable_tools=false',
                 '-Denable_tests=false', '-Denable_examples=false', '-Denable_docs=false',
                 "-Dbitdepths=['8','16']"]


def run(*args, cwd=ROOT, env=None):
    print('+', ' '.join(str(a) for a in args), flush=True)
    subprocess.run([str(a) for a in args], cwd=cwd, env=env, check=True)


def output(*args, cwd=ROOT):
    return subprocess.check_output([str(a) for a in args], text=True, cwd=cwd).strip()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_slices(archive, platform):
    """Every member: arm64 only, LC_BUILD_VERSION <platform> with minos DEPLOYMENT_TARGET."""

    if output('xcrun', 'lipo', '-archs', archive) != 'arm64':
        raise SystemExit(f'{archive}: not a single arm64 slice')
    text = output('xcrun', 'otool', '-l', archive)
    members = re.split(r'\n(?=\S.*\.o\):\n)', text)
    checked = 0
    for member in members:
        if '.o):' not in member.splitlines()[0]:
            continue
        found = re.findall(r'cmd LC_BUILD_VERSION\n.*?platform (\S+)\n\s+minos (\S+)', member, re.S)
        if not found or any(p not in PLATFORM_IDS[platform] or m != DEPLOYMENT_TARGET for p, m in found):
            raise SystemExit(f'{archive}: {member.splitlines()[0]} is {found}, expected {platform} {DEPLOYMENT_TARGET}')
        checked += 1
    if not checked:
        raise SystemExit(f'{archive}: no members found')
    return checked


def link_check(sdk, target, libdir, destination):
    """The archives resolve against each other and the SDK, as FFmpeg.framework links them."""

    sdkroot = output('xcrun', '--sdk', sdk, '--show-sdk-path')
    clang = output('xcrun', '--sdk', sdk, '--find', 'clang')
    args = [clang, '-target', target, '-isysroot', sdkroot, '-dynamiclib', '-install_name',
            '@rpath/FFmpeg.framework/FFmpeg']
    for name in LIBRARIES:
        args += ['-Wl,-force_load', libdir / name]
    for framework in FRAMEWORKS:
        args += ['-framework', framework]
    run(*args, '-lz', '-o', destination)
    return destination.stat().st_size


def build_aom(sdk, target, source, build, prefix):
    sdkroot = output('xcrun', '--sdk', sdk, '--show-sdk-path')
    run('cmake', '-S', source, '-B', build, '-G', 'Unix Makefiles', '-DCMAKE_BUILD_TYPE=MinSizeRel',
        '-DCMAKE_SYSTEM_NAME=iOS', f'-DCMAKE_OSX_SYSROOT={sdkroot}', '-DCMAKE_OSX_ARCHITECTURES=arm64',
        f'-DCMAKE_OSX_DEPLOYMENT_TARGET={DEPLOYMENT_TARGET}', f'-DCMAKE_C_FLAGS=-target {target}',
        f'-DCMAKE_CXX_FLAGS=-target {target}', f'-DCMAKE_ASM_FLAGS=-target {target}',
        f'-DCMAKE_INSTALL_PREFIX={prefix}', *AOM_OPTIONS)
    run('cmake', '--build', build, '-j', str(os.cpu_count() or 2))
    run('cmake', '--install', build)


def build_dav1d(sdk, target, source, build, prefix):
    sdkroot = output('xcrun', '--sdk', sdk, '--show-sdk-path')
    clang = output('xcrun', '--sdk', sdk, '--find', 'clang')
    flags = ['-target', target, '-isysroot', sdkroot]
    cross = build.with_suffix('.cross.ini')
    build.parent.mkdir(parents=True, exist_ok=True)
    cross.write_text(f"""[binaries]
c = {[clang, *flags]!r}
ar = {output('xcrun', '-f', 'ar')!r}
strip = {output('xcrun', '-f', 'strip')!r}
pkg-config = 'pkg-config'

[host_machine]
system = 'darwin'
subsystem = 'ios'
cpu_family = 'aarch64'
cpu = 'aarch64'
endian = 'little'
""")
    run('meson', 'setup', build, source, f'--cross-file={cross}', f'--prefix={prefix}', '--libdir=lib',
        *DAV1D_OPTIONS)
    run('ninja', '-C', build)
    run('ninja', '-C', build, 'install')


def build_ffmpeg(sdk, target, source, build, prefix):
    sdkroot = output('xcrun', '--sdk', sdk, '--show-sdk-path')
    cc = f"{output('xcrun', '--sdk', sdk, '--find', 'clang')} -target {target} -isysroot {sdkroot}"
    env = os.environ.copy()
    env['PKG_CONFIG_PATH'] = str(prefix / 'lib' / 'pkgconfig')
    env['PKG_CONFIG_LIBDIR'] = str(prefix / 'lib' / 'pkgconfig')
    build.mkdir(parents=True)
    run(source / 'configure', f'--prefix={prefix}', '--arch=aarch64', '--target-os=darwin',
        f'--cc={cc}', f'--cxx={cc}', f'--ld={cc}', f"--ar={output('xcrun', '-f', 'ar')}",
        f"--ranlib={output('xcrun', '-f', 'ranlib')}", f"--strip={output('xcrun', '-f', 'strip')}",
        f"--nm={output('xcrun', '-f', 'nm')}", '--pkg-config=pkg-config', '--pkg-config-flags=--static',
        *FFMPEG_OPTIONS, cwd=build, env=env)
    run('make', '-j', str(os.cpu_count() or 2), cwd=build, env=env)
    run('make', 'install', cwd=build, env=env)


def main():
    if sys.platform != 'darwin':
        raise SystemExit('Run on macOS with Xcode, CMake and pkg-config installed.')
    for tool in ('cmake', 'meson', 'ninja', 'pkg-config', 'git'):
        if not shutil.which(tool):
            raise SystemExit(f'Missing {tool}')
    archive = ROOT / 'source' / f'ffmpeg-{FFMPEG_VERSION}.tar.xz'
    if digest(archive) != FFMPEG_SHA256:
        raise SystemExit('FFmpeg source checksum mismatch')
    # Refuse to silently mix objects/configuration from an earlier build.
    if WORK.exists() or OUT.exists():
        raise SystemExit(f'Build outputs already exist. Review and remove {WORK} and {OUT} before rebuilding.')
    WORK.mkdir(parents=True)
    OUT.mkdir(parents=True)

    aom = WORK / 'aom'
    run('git', 'clone', '--quiet', '--branch', AOM_TAG, '--depth', '1', AOM_URL, aom)
    if output('git', 'rev-parse', 'HEAD', cwd=aom) != AOM_COMMIT:
        raise SystemExit(f'aom {AOM_TAG} is not {AOM_COMMIT}')
    dav1d = WORK / 'dav1d'
    run('git', 'clone', '--quiet', '--branch', DAV1D_TAG, '--depth', '1', DAV1D_URL, dav1d)
    if output('git', 'rev-parse', 'HEAD', cwd=dav1d) != DAV1D_COMMIT:
        raise SystemExit(f'dav1d {DAV1D_TAG} is not {DAV1D_COMMIT}')

    run('tar', '-xJf', archive, '-C', WORK)
    ffmpeg = WORK / f'ffmpeg-{FFMPEG_VERSION}'
    patches = []
    for name in FFMPEG_PATCHES:
        patch = ROOT / 'patches' / name
        run('patch', '--batch', '--fuzz=0', '-p1', '-i', patch, cwd=ffmpeg)
        patches.append({'name': name, 'sha256': digest(patch)})

    metadata = {'ffmpeg': FFMPEG_VERSION, 'ffmpeg_sha256': FFMPEG_SHA256, 'patches': patches,
                'aom': {'tag': AOM_TAG, 'commit': AOM_COMMIT, 'options': AOM_OPTIONS, 'build_type': 'MinSizeRel'},
                'dav1d': {'tag': DAV1D_TAG, 'commit': DAV1D_COMMIT, 'options': DAV1D_OPTIONS},
                'frameworks': FRAMEWORKS,
                'ffmpeg_options': FFMPEG_OPTIONS, 'optimization': OPTIMIZATION,
                'deployment_target': DEPLOYMENT_TARGET,
                'commit': output('git', 'rev-parse', 'HEAD'),
                'dirty': bool(output('git', 'status', '--porcelain')),
                'xcode': output('xcodebuild', '-version'), 'cmake': output('cmake', '--version').splitlines()[0],
                'meson': output('meson', '--version'),
                'slices': {}}
    for sdk, folder, platform, suffix in SLICES:
        target = f'arm64-apple-ios{DEPLOYMENT_TARGET}{suffix}'
        prefix = WORK / f'install-{sdk}'
        build_aom(sdk, target, aom, WORK / f'aom-{sdk}', prefix)
        build_dav1d(sdk, target, dav1d, WORK / f'dav1d-{sdk}', prefix)
        build_ffmpeg(sdk, target, ffmpeg, WORK / f'ffmpeg-{sdk}', prefix)

        destination = OUT / folder
        destination.mkdir()
        members = {}
        for name in LIBRARIES:
            shutil.copy2(prefix / 'lib' / name, destination / name)
            members[name] = check_slices(destination / name, platform)
        if folder == 'release':
            # Public headers of the device build (the app's RPLAudioBridge uses
            # them; the simulator headers are identical), for the shipped
            # libraries only.
            shutil.copytree(prefix / 'include', OUT / 'include',
                            ignore=lambda folder, names: [n for n in names if n in UNSHIPPED_HEADERS])
        size = link_check(sdk, target, destination, WORK / f'FFmpeg-{sdk}.dylib')
        metadata['slices'][folder] = {'sdk': output('xcrun', '--sdk', sdk, '--show-sdk-version'),
                                      'target': target, 'members': members, 'linked_dylib_bytes': size}

    licenses = OUT / 'LICENSES'
    licenses.mkdir()
    for name in ('COPYING.LGPLv2.1', 'LICENSE.md'):
        shutil.copy2(ffmpeg / name, licenses / f'ffmpeg-{name}')
    for name in ('LICENSE', 'PATENTS'):
        shutil.copy2(aom / name, licenses / f'aom-{name}')
    shutil.copy2(dav1d / 'COPYING', licenses / 'dav1d-COPYING')
    (OUT / 'build-info.json').write_text(json.dumps(metadata, indent=2) + '\n')
    files = sorted(p for p in OUT.rglob('*') if p.is_file())
    (OUT / 'SHA256SUMS').write_text(''.join(f'{digest(p)}  {p.relative_to(OUT)}\n' for p in files))
    print(f'Artifacts: {OUT}')


if __name__ == '__main__':
    main()
