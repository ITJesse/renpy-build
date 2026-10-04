#!/usr/bin/env python3
"""Build RenPyLinter's SDL3 with the upstream Ren'Py patch series on macOS.

The configuration mirrors what renpy-build produces for ios-arm64 and
ios-sim-arm64 (tasks/sdl3.py, the MetalANGLE annotator in tasks/metalangle.py
and renpybuild/run.py), with RenPyLinter's iOS 15.6 deployment target and
size optimisation, and with Xcode's toolchain instead of upstream's Linux
cross compilers.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'tmp' / 'renpylinter-sdl3'
OUT = ROOT / 'dist' / 'renpylinter-sdl3'
MINIMUM_IOS = '15.6'
SOURCE_SHA256 = 'e9fff7467fb60f037e6708da18b25560649e4c63edc2a69bb871b960d9cbfbba'

# Upstream's patches, in the order tasks/sdl3.py applies them, then ours.
UPSTREAM_PATCHES = ['sdl3-opengl-ios.diff', 'sdl3-metalangle.diff']
RENPYLINTER_PATCHES = ROOT / 'patches' / 'renpylinter-sdl3'

# Exactly the options tasks/sdl3.py passes; nothing else is switched off.
SDL_OPTIONS = ['-DSDL_STATIC=ON', '-DSDL_SHARED=OFF', '-DSDL_CAMERA=OFF', '-DSDL_DEPS_SHARED=ON',
               '-DSDL_TESTS=OFF']

SLICES = {
    # key: (Xcode SDK, clang target, MetalANGLE archive, output folder)
    'iphoneos-arm64': ('iphoneos', f'arm64-apple-ios{MINIMUM_IOS}', 'MetalANGLE.framework.ios.zip', 'release'),
    'iphonesimulator-arm64': ('iphonesimulator', f'arm64-apple-ios{MINIMUM_IOS}-simulator',
                              'MetalANGLE.framework.ios.simulator.zip', 'debug'),
}


def run(*args, cwd=ROOT, env=None):
    print('$ ' + ' '.join(shlex.quote(str(a)) for a in args), flush=True)
    subprocess.run([str(a) for a in args], cwd=cwd, env=env, check=True)


def output(*args):
    return subprocess.check_output([str(a) for a in args], text=True).strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sdl_version():
    task = (ROOT / 'tasks' / 'sdl3.py').read_text()
    return re.search(r'^version\s*=\s*"([^"]+)"', task, re.M).group(1)


def series(directory):
    names = []
    for line in (directory / 'series').read_text().splitlines():
        name = line.split('#', 1)[0].strip()
        if name:
            names.append(name)
    listed = set(names)
    present = {p.name for p in directory.iterdir() if p.is_file() and p.name != 'series'}
    if present != listed:
        raise SystemExit(f'{directory}/series lists {sorted(listed)}, directory has {sorted(present)}')
    return [directory / n for n in names]


def fetch_source(version):
    """The tarball tasks/sdl3.py downloads, pinned by its sha256."""

    archive = WORK / f'SDL3-{version}.tar.gz'
    url = f'https://github.com/libsdl-org/SDL/releases/download/release-{version}/SDL3-{version}.tar.gz'
    print(f'fetching {url}', flush=True)
    with urllib.request.urlopen(url) as r, open(archive, 'wb') as f:
        shutil.copyfileobj(r, f)
    if digest(archive) != SOURCE_SHA256:
        raise SystemExit(f'{archive.name}: sha256 {digest(archive)}, expected {SOURCE_SHA256}')
    return url, archive


def main():
    if sys.platform != 'darwin':
        raise SystemExit('Run on macOS with Xcode selected.')
    for tool in ('cmake', 'ninja'):
        if not shutil.which(tool):
            raise SystemExit(f'Missing {tool}: pip install cmake==3.28.3 ninja==1.11.1.4')
    # Refuse to silently mix objects/configuration from an earlier build.
    if WORK.exists() or OUT.exists():
        raise SystemExit(f'Build outputs already exist. Review and remove {WORK} and {OUT} before rebuilding.')

    version = sdl_version()
    WORK.mkdir(parents=True)
    OUT.mkdir(parents=True)
    url, archive = fetch_source(version)
    run('tar', '-xzf', archive, '-C', WORK)
    source = WORK / f'SDL3-{version}'
    patches = []
    for patch in [ROOT / 'patches' / n for n in UPSTREAM_PATCHES] + series(RENPYLINTER_PATCHES):
        run('patch', '--batch', '--fuzz=0', '-p1', '-i', patch, cwd=source)
        patches.append({'name': str(patch.relative_to(ROOT)), 'sha256': digest(patch)})

    metadata = {'sdl': version, 'source_url': url, 'source_sha256': SOURCE_SHA256,
                'optimization': '-Os (CMAKE_BUILD_TYPE=MinSizeRel)', 'minimum_ios': MINIMUM_IOS,
                'sdl_options': SDL_OPTIONS,
                'commit': output('git', '-C', ROOT, 'rev-parse', 'HEAD'),
                'dirty': bool(output('git', '-C', ROOT, 'status', '--porcelain')),
                'xcode': output('xcodebuild', '-version'), 'cmake': output('cmake', '--version').splitlines()[0],
                'ninja': output('ninja', '--version'), 'patches': patches, 'slices': {}}

    for key, (sdk, target, metal_zip_name, folder) in SLICES.items():
        build = WORK / key
        build.mkdir()
        metal_zip = ROOT / 'source' / metal_zip_name
        frameworks = build / 'frameworks'
        run('unzip', '-q', metal_zip, '-d', frameworks)
        install = build / 'install'
        sdkroot = output('xcrun', '--sdk', sdk, '--show-sdk-path')
        clang = output('xcrun', '--sdk', sdk, '--find', 'clang')
        clangxx = output('xcrun', '--sdk', sdk, '--find', 'clang++')
        target_args = f'-target {target} -isysroot {shlex.quote(sdkroot)}'
        subframeworks = f'-F{shlex.quote(sdkroot)}/System/Library/SubFrameworks'
        # renpybuild/run.py, then the MetalANGLE annotator for the sdl3 task.
        metalangle = f'-F {shlex.quote(str(frameworks))} -DMETALANGLE -Wno-unused-command-line-argument'
        cflags = (f'-Os -I{install}/include -DSDL_MAIN_HANDLED {subframeworks} '
                  f'-DRENPY_BUILD -DCYTHON_NO_PYINIT_EXPORT {metalangle}')
        env = {k: v for k, v in os.environ.items()
               if k not in ('CFLAGS', 'CXXFLAGS', 'OBJCFLAGS', 'CPPFLAGS', 'LDFLAGS', 'SDKROOT',
                            'IPHONEOS_DEPLOYMENT_TARGET', 'MACOSX_DEPLOYMENT_TARGET', 'CPATH',
                            'LIBRARY_PATH', 'PKG_CONFIG_PATH')}
        env.update(CC=f'{clang} {target_args}', CXX=f'{clangxx} {target_args} -stdlib=libc++',
                   OBJC=f'{clang} {target_args}',
                   CFLAGS=cflags, CXXFLAGS=cflags,
                   OBJCFLAGS=f'{subframeworks} {metalangle}',
                   CPPFLAGS=f'-I{install}/include  -DSDL_MAIN_HANDLED',
                   LDFLAGS=f'-Os -L{install}/lib',
                   PKG_CONFIG_LIBDIR=f'{install}/lib/pkgconfig',
                   ZERO_AR_DATE='1')
        run('cmake', '-G', 'Ninja', '-S', source, '-B', build / 'cmake',
            '-DCMAKE_SYSTEM_NAME=iOS', '-DCMAKE_SYSTEM_PROCESSOR=aarch64',
            f'-DCMAKE_OSX_SYSROOT={sdkroot}', '-DCMAKE_OSX_ARCHITECTURES=arm64',
            f'-DCMAKE_OSX_DEPLOYMENT_TARGET={MINIMUM_IOS}',
            f'-DCMAKE_FIND_ROOT_PATH={install};{sdkroot}',
            '-DCMAKE_FIND_ROOT_PATH_MODE_PROGRAM=NEVER', '-DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY',
            '-DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=ONLY', '-DCMAKE_FIND_ROOT_PATH_MODE_PACKAGE=ONLY',
            f'-DCMAKE_AR={output("xcrun", "--sdk", sdk, "--find", "ar")}',
            f'-DCMAKE_RANLIB={output("xcrun", "--sdk", sdk, "--find", "ranlib")}',
            '-DCMAKE_BUILD_TYPE=MinSizeRel', f'-DCMAKE_INSTALL_PREFIX={install}',
            *SDL_OPTIONS, env=env)
        run('cmake', '--build', build / 'cmake', env=env)
        run('cmake', '--install', build / 'cmake', env=env)
        library = install / 'lib' / 'libSDL3.a'
        if not library.is_file():
            raise SystemExit(f'Missing built library: {library}')
        destination = OUT / folder
        destination.mkdir()
        shutil.copy2(library, destination / 'libSDL3.a')
        run('xcrun', 'lipo', '-info', destination / 'libSDL3.a')
        config = next((build / 'cmake').glob('include-config-*/build_config/SDL_build_config.h')).read_text()
        metadata['slices'][key] = {
            'folder': folder, 'sdk': output('xcrun', '--sdk', sdk, '--show-sdk-version'), 'target': target,
            'metalangle_sha256': digest(metal_zip),
            # The subsystems SDL's configuration enabled, for review against upstream.
            'enabled': sorted(re.findall(r'^#define (SDL_(?:VIDEO|RENDER|GPU|AUDIO_DRIVER|JOYSTICK|HAPTIC)_\w+) 1$',
                                         config, re.M)),
        }

    shutil.copy2(source / 'LICENSE.txt', OUT / 'SDL-LICENSE.txt')
    (OUT / 'build-info.json').write_text(json.dumps(metadata, indent=2) + '\n')
    files = sorted(p for p in OUT.rglob('*') if p.is_file())
    (OUT / 'SHA256SUMS').write_text(''.join(f'{digest(p)}  {p.relative_to(OUT)}\n' for p in files))
    print(f'Artifacts: {OUT}')


if __name__ == '__main__':
    main()
