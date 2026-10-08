#!/usr/bin/env python3
"""Build RenPyLinter's FFmpeg (with libaom) for iOS on macOS, optimized for size.

The app embeds one FFmpeg.framework that every engine (7.5.3 to 8.5.3) and the
app itself link. Every engine branch of renpy-build builds FFmpeg 4.3.1 and
aom v3.5.0 the same way (tasks/ffmpeg.py, tasks/aom.py); this script builds
that configuration natively with Xcode, at -Os (aom: MinSizeRel) instead of
upstream's -O3 / Release, for the device and the arm64 simulator.
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

FFMPEG_VERSION = '4.3.1'
FFMPEG_SHA256 = '45035f15d6f192772de2309c846e1d60472694f479679354a39c699719e53772'
# tasks/ffmpeg.py applies these, in this order.
FFMPEG_PATCHES = ['ffmpeg-4.3.1-sse.diff', 'ffmpeg-4.3.1-ff_seek_frame_binary.diff']
AOM_URL = 'https://aomedia.googlesource.com/aom'
AOM_TAG = 'v3.5.0'
AOM_COMMIT = 'bcfe6fbfed315f83ee8a95465c654ee8078dbff9'

DEPLOYMENT_TARGET = '15.6'
OPTIMIZATION = '-Os'
SLICES = [('iphoneos', 'release', 'IOS', ''), ('iphonesimulator', 'debug', 'IOSSIMULATOR', '-simulator')]
# otool prints LC_BUILD_VERSION platforms by name or by number, depending on the Xcode.
PLATFORM_IDS = {'IOS': {'IOS', '2'}, 'IOSSIMULATOR': {'IOSSIMULATOR', '7'}}
LIBRARIES = ['libaom.a', 'libavcodec.a', 'libavformat.a', 'libavutil.a', 'libswresample.a', 'libswscale.a']
# Built like upstream, but not shipped (only the ffmpeg program would use them).
UNSHIPPED_HEADERS = {'libavfilter', 'libavresample'}

# tasks/ffmpeg.py's iOS configuration. Differences: --optflags (size instead of
# -O3) and --disable-programs (the ffmpeg/ffplay executables are not shipped;
# libavfilter and libavresample, which only they would need, are still built
# as upstream does but not shipped either).
DEMUXERS = ['au', 'avi', 'flac', 'm4v', 'matroska', 'mov', 'mp3', 'mpegps', 'mpegts', 'mpegtsraw',
            'mpegvideo', 'ogg', 'wav', 'av1']
DECODERS = ['flac', 'mp2', 'mp3', 'mp3on4', 'mpeg1video', 'mpeg2video', 'mpegvideo', 'msmpeg4v1',
            'msmpeg4v2', 'msmpeg4v3', 'mpeg4', 'pcm_dvd', 'pcm_s16be', 'pcm_s16le', 'pcm_s8', 'pcm_u16be',
            'pcm_u16le', 'pcm_u8', 'theora', 'vorbis', 'opus', 'vp3', 'vp8', 'vp9', 'libaom_av1']
PARSERS = ['mpegaudio', 'mpegvideo', 'mpeg4video', 'vp3', 'vp8', 'vp9', 'av1']
FFMPEG_OPTIONS = [
    '--enable-pic', '--enable-static', '--disable-all', '--disable-everything', '--enable-cross-compile',
    '--enable-runtime-cpudetect', '--disable-mmx', '--disable-mmxext',
    '--enable-avcodec', '--enable-avformat', '--enable-swresample', '--enable-swscale',
    '--enable-avfilter', '--enable-avresample', '--enable-libaom', '--disable-bzlib', '--disable-doc',
    *[f'--enable-demuxer={d}' for d in DEMUXERS], *[f'--enable-decoder={d}' for d in DECODERS],
    *[f'--enable-parser={p}' for p in PARSERS],
    '--disable-iconv', '--disable-alsa', '--disable-libxcb', '--disable-lzma', '--disable-sndio',
    '--disable-xlib', '--disable-amf', '--disable-audiotoolbox', '--disable-cuda-llvm', '--disable-d3d11va',
    '--disable-dxva2', '--disable-ffnvcodec', '--disable-nvdec', '--disable-nvenc', '--disable-v4l2-m2m',
    '--disable-vaapi', '--disable-vdpau', '--disable-videotoolbox',
    f'--optflags={OPTIMIZATION}', '--disable-programs',
]
# tasks/aom.py, as MinSizeRel.
AOM_OPTIONS = ['-DCONFIG_AV1_ENCODER=0', '-DENABLE_EXAMPLES=0', '-DENABLE_TOOLS=0', '-DENABLE_TESTS=0',
               '-DENABLE_DOCS=0', '-DCONFIG_PIC=1', '-DCONFIG_RUNTIME_CPU_DETECT=0', '-DAOM_TARGET_CPU=arm64']


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
    for tool in ('cmake', 'pkg-config', 'git'):
        if not shutil.which(tool):
            raise SystemExit(f'Missing {tool}')
    archive = ROOT / 'source' / f'ffmpeg-{FFMPEG_VERSION}.tar.gz'
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

    run('tar', '-xzf', archive, '-C', WORK)
    ffmpeg = WORK / f'ffmpeg-{FFMPEG_VERSION}'
    patches = []
    for name in FFMPEG_PATCHES:
        patch = ROOT / 'patches' / name
        run('patch', '--batch', '--fuzz=0', '-p1', '-i', patch, cwd=ffmpeg)
        patches.append({'name': name, 'sha256': digest(patch)})

    metadata = {'ffmpeg': FFMPEG_VERSION, 'ffmpeg_sha256': FFMPEG_SHA256, 'patches': patches,
                'aom': {'tag': AOM_TAG, 'commit': AOM_COMMIT, 'options': AOM_OPTIONS, 'build_type': 'MinSizeRel'},
                'ffmpeg_options': FFMPEG_OPTIONS, 'optimization': OPTIMIZATION,
                'deployment_target': DEPLOYMENT_TARGET,
                'commit': output('git', 'rev-parse', 'HEAD'),
                'dirty': bool(output('git', 'status', '--porcelain')),
                'xcode': output('xcodebuild', '-version'), 'cmake': output('cmake', '--version').splitlines()[0],
                'slices': {}}
    for sdk, folder, platform, suffix in SLICES:
        target = f'arm64-apple-ios{DEPLOYMENT_TARGET}{suffix}'
        prefix = WORK / f'install-{sdk}'
        build_aom(sdk, target, aom, WORK / f'aom-{sdk}', prefix)
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
    (OUT / 'build-info.json').write_text(json.dumps(metadata, indent=2) + '\n')
    files = sorted(p for p in OUT.rglob('*') if p.is_file())
    (OUT / 'SHA256SUMS').write_text(''.join(f'{digest(p)}  {p.relative_to(OUT)}\n' for p in files))
    print(f'Artifacts: {OUT}')


if __name__ == '__main__':
    main()
