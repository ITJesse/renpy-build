#!/usr/bin/env python3
"""Build RenPyLinter's SDL2 with the upstream Ren'Py patch series on macOS."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'tmp' / 'renpylinter-sdl2'
OUT = ROOT / 'dist' / 'renpylinter-sdl2'
VERSION = '2.0.20'
SOURCE_SHA256 = 'c56aba1d7b5b0e7e999e4a7698c70b63a3394ff9704b5f6e1c57e0c16f04dd06'


def run(*args, cwd=ROOT, env=None):
    subprocess.run(args, cwd=cwd, env=env, check=True)


def output(*args):
    return subprocess.check_output(args, text=True).strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    if sys.platform != 'darwin':
        raise SystemExit('Run on macOS with Xcode and autoconf installed.')
    if not shutil.which('autoconf'):
        raise SystemExit('Missing autoconf: brew install autoconf')
    archive = ROOT / 'source' / f'SDL2-{VERSION}.tar.gz'
    if digest(archive) != SOURCE_SHA256:
        raise SystemExit('SDL source checksum mismatch')
    # Refuse to silently mix objects/configuration from an earlier build.
    if WORK.exists() or OUT.exists():
        raise SystemExit(f'Build outputs already exist. Review and remove {WORK} and {OUT} before rebuilding.')
    WORK.mkdir(parents=True)
    OUT.mkdir(parents=True)
    run('tar', '-xzf', str(archive), '-C', str(WORK))
    source = WORK / f'SDL2-{VERSION}'
    patch_dir = ROOT / 'patches' / f'SDL2-{VERSION}'
    patches = []
    for line in (patch_dir / 'series').read_text().splitlines():
        name = line.strip()
        if not name or name.startswith('#'):
            continue
        patch = patch_dir / name
        run('patch', '--batch', '--fuzz=0', '-p1', '-i', str(patch), cwd=source)
        patches.append({'name': name, 'sha256': digest(patch)})
    run('sh', 'autogen.sh', cwd=source)
    metadata = {'sdl': VERSION, 'source_sha256': SOURCE_SHA256,
                'commit': output('git', '-C', str(ROOT), 'rev-parse', 'HEAD'),
                'dirty': bool(output('git', '-C', str(ROOT), 'status', '--porcelain')),
                'xcode': output('xcodebuild', '-version'), 'patches': patches, 'slices': {}}
    for sdk, arch in [('iphoneos', 'arm64'), ('iphonesimulator', 'arm64')]:
        key = f'{sdk}-{arch}'
        build = WORK / key
        build.mkdir()
        metal_zip = ROOT / 'source' / ('MetalANGLE.framework.ios.zip' if sdk == 'iphoneos' else 'MetalANGLE.framework.ios.simulator.zip')
        run('unzip', '-q', str(metal_zip), '-d', str(build))
        sdkroot = output('xcrun', '--sdk', sdk, '--show-sdk-path')
        target = f'{arch}-apple-ios15.6' + ('-simulator' if sdk == 'iphonesimulator' else '')
        common = f'-target {target} -isysroot {sdkroot} -O2 -DSDL_MAIN_HANDLED -DRENPY_BUILD -DMETALANGLE -F{build}'
        env = os.environ.copy()
        env.update(CC=output('xcrun', '--sdk', sdk, '--find', 'clang'),
                   CXX=output('xcrun', '--sdk', sdk, '--find', 'clang++'),
                   CFLAGS=common + ' -fobjc-arc', CXXFLAGS=common + ' -fobjc-arc',
                   OBJCFLAGS=common + ' -fobjc-arc',
                   LDFLAGS=f'-target {target} -isysroot {sdkroot} -F{build} -framework MetalANGLE',
                   ac_cv_header_libunwind_h='no')
        host = 'arm-ios-darwin21'
        run(str(source / 'configure'), f'--host={host}', f'--build={output(str(source / "build-scripts/config.guess"))}',
            '--disable-shared', '--enable-static', '--disable-render-metal', '--disable-video-metal',
            '--disable-video-vulkan', '--disable-jack', '--disable-pipewire', '--disable-video-kmsdrm',
            '--disable-video-x11', '--disable-video-wayland', f'--prefix={build / "install"}', cwd=build, env=env)
        # Mirror tasks/sdl2.py. Native Metal/Vulkan are disabled: Ren'Py uses MetalANGLE.
        with (build / 'include/SDL_config.h').open('a') as f:
            f.write('\n#define SDL_POWER_UIKIT 1\n#define SDL_IPHONE_KEYBOARD 1\n'
                    '#define SDL_IPHONE_LAUNCHSCREEN 1\n#define SDL_IPHONE_MAX_GFORCE 5.0\n'
                    '#define SDL_FILESYSTEM_COCOA 1\n#undef SDL_JOYSTICK_MFI\n')
        run('make', '-j', str(os.cpu_count() or 2), 'build/libSDL2.la', cwd=build, env=env)
        library = build / 'build/.libs/libSDL2.a'
        if not library.is_file():
            raise SystemExit(f'Missing built library: {library}')
        metadata['slices'][key] = {'sdk': output('xcrun', '--sdk', sdk, '--show-sdk-version'),
                                  'target': target, 'metalangle_sha256': digest(metal_zip)}
    for folder, slices in [('release', ['iphoneos-arm64']), ('debug', ['iphonesimulator-arm64'])]:
        destination = OUT / folder
        destination.mkdir()
        run('xcrun', 'lipo', '-create', *(str(WORK / s / 'build/.libs/libSDL2.a') for s in slices),
            '-output', str(destination / 'libSDL2.a'))
        run('xcrun', 'lipo', '-info', str(destination / 'libSDL2.a'))
    shutil.copy2(source / 'LICENSE.txt', OUT / 'SDL-LICENSE.txt')
    (OUT / 'build-info.json').write_text(json.dumps(metadata, indent=2) + '\n')
    files = sorted(p for p in OUT.rglob('*') if p.is_file())
    (OUT / 'SHA256SUMS').write_text(''.join(f'{digest(p)}  {p.relative_to(OUT)}\n' for p in files))
    print(f'Artifacts: {OUT}')


if __name__ == '__main__':
    main()
