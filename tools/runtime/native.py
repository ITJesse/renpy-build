#!/usr/bin/env python3
"""macOS adapter for the pinned upstream tasks; keeps their tmp/build/install paths."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile
import tarfile
import shlex
import threading
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
LOCK = json.loads((ROOT / 'tools/runtime/lock.json').read_text())
COMMANDS=[]

def run(*args, cwd=ROOT, env=None):
    print('+', *map(str, args), flush=True)
    subprocess.run(list(map(str, args)), cwd=cwd, env=env, check=True)

def output(*args):
    return subprocess.check_output(list(map(str,args)), text=True).strip()

def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def fetch(url, commit, path):
    if path.exists():
        if output('git','-C',path,'rev-parse','HEAD') != commit:
            raise RuntimeError('Existing checkout does not match lock: '+str(path))
        return
    path.mkdir(parents=True)
    run('git','init',path)
    run('git','-C',path,'fetch','--depth','1',url,commit)
    run('git','-C',path,'checkout','--detach',commit)


def native_environment(c):
    for key in ['CPATH','C_INCLUDE_PATH','CPLUS_INCLUDE_PATH','OBJC_INCLUDE_PATH','SDKROOT']:
        c.environ.pop(key,None)
    host = c.kind in ('host','host-python','cross')
    sdk = 'macosx' if host else ('iphonesimulator' if c.arch == 'sim-arm64' else 'iphoneos')
    sdkroot = output('xcrun','--sdk',sdk,'--show-sdk-path')
    c.var('make', 'make -j '+str(min(os.cpu_count() or 2, 8)))
    c.var('configure', './configure')
    c.var('cmake_configure','cmake')
    c.var('lipo','xcrun lipo')
    build = output('sh',ROOT/'tools/runtime/config.guess')
    c.var('build_platform',build)
    c.var('host_platform','arm-apple-darwin')
    c.var('sdl_host_platform','arm-ios-darwin21')
    c.var('ffi_host_platform','aarch64-ios-darwin21')
    for key,prefix in [('cross_config','arm-apple-darwin'),('sdl_cross_config','arm-ios-darwin21'),('ffi_cross_config','aarch64-ios-darwin21')]:
        c.var(key, '' if host else '--host='+prefix+' --build='+build)
    c.var('configure_cross','')
    flags = '-O2 -fPIC -ffile-prefix-map='+str(ROOT)+'=/renpy-build -DRENPY_BUILD -I'+str(c.install/'include')
    target = '' if host else 'arm64-apple-ios'+LOCK['minimum_ios']+('-simulator' if c.arch == 'sim-arm64' else '')
    if target:
        flags += ' -target '+target+' -isysroot '+sdkroot+' -DSDL_MAIN_HANDLED'
    else:
        flags += ' -isysroot '+sdkroot
    for key,tool in [('CC','clang'),('CXX','clang++'),('AR','ar'),('RANLIB','ranlib'),('NM','nm'),('STRIP','strip')]:
        c.env(key,output('xcrun','--sdk',sdk,'--find',tool))
    c.env('CPP',c.environ['CC']+' -E -isysroot '+sdkroot+(' -target '+target if target else ''))
    c.env('CFLAGS',flags)
    c.env('CXXFLAGS',flags+' -std=c++17')
    c.env('CPPFLAGS','-I'+str(c.install/'include'))
    c.env('LDFLAGS',('-target '+target+' ' if target else '')+'-isysroot '+sdkroot+' -L'+str(c.install/'lib'))
    c.env('PATH','{{ host }}/bin:{{ PATH }}')
    c.env('PKG_CONFIG','pkg-config --static')
    c.env('PKG_CONFIG_PATH',str(c.install/'lib/pkgconfig'))
    c.env('PKG_CONFIG_LIBDIR',str(c.install/'lib/pkgconfig'))
    # Target deployment is already explicit in -target and CMake. An ambient
    # IPHONEOS_DEPLOYMENT_TARGET also retargets nested host tools such as
    # FreeType's apinames, even when they invoke the native compiler.
    c.environ.pop('IPHONEOS_DEPLOYMENT_TARGET',None)
    c.environ.pop('MACOSX_DEPLOYMENT_TARGET',None)
    build_sdk=output('xcrun','--sdk','macosx','--show-sdk-path')
    build_cc=output('xcrun','--sdk','macosx','--find','clang')
    for key in ['CC_BUILD','CC_FOR_BUILD','BUILD_CC']:
        c.env(key,build_cc+' -target arm64-apple-macos11.0 -isysroot '+build_sdk)
    cmake = '-G Ninja -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX='+str(c.install)
    if not host:
        cmake += (' -DCMAKE_SYSTEM_NAME=iOS -DCMAKE_OSX_ARCHITECTURES=arm64 -DCMAKE_OSX_SYSROOT='+sdkroot+
                  ' -DCMAKE_OSX_DEPLOYMENT_TARGET='+LOCK['minimum_ios']+' -DCMAKE_MACOSX_BUNDLE=OFF'+
                  ' -DCMAKE_FIND_ROOT_PATH='+str(c.install)+' -DCMAKE_SYSTEM_PROCESSOR=aarch64 -DCMAKE_FIND_ROOT_PATH_MODE_PROGRAM=NEVER'
                  ' -DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY -DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=ONLY -DCMAKE_FIND_ROOT_PATH_MODE_PACKAGE=ONLY')
    c.var('cmake_args',cmake)
    c.env('CMAKE_BUILD_PARALLEL_LEVEL',str(min(os.cpu_count() or 2,8)))


def host_python(c):
    import tasks.hostpython3 as hp
    c.chdir('Python-'+hp.version)
    c.run('{{configure}} --prefix="{{ host }}" --with-ensurepip=install')
    c.run('{{ make }}')
    c.run('{{ make }} install')
    wheelhouse=ROOT/'tmp/tars/python-build-tools'
    wheelhouse.mkdir(parents=True,exist_ok=True)
    run(sys.executable,'-m','pip','download','--only-binary=:all:','--no-deps','--dest',wheelhouse,
        '--require-hashes','-r',ROOT/'tools/runtime/build-tool-requirements.txt')
    c.run('{{ host }}/bin/python3 -m pip install --no-index --find-links='+str(wheelhouse)+' --require-hashes -r '+str(ROOT/'tools/runtime/build-tool-requirements.txt'))


def python_post(c):
    import tasks.python3 as p
    c.generate('{{ source }}/Python-{{ version }}-Setup.stdlib','Modules/Setup.stdlib')
    c.generate('{{ source }}/Python-{{ version }}-Setup.stdlib','Modules/Setup')
    c.run('{{ make }} libpython'+'.'.join(p.version.split('.')[:2])+'.a')
    include=c.install/'include'/('python'+'.'.join(p.version.split('.')[:2]))
    include.mkdir(parents=True,exist_ok=True)
    shutil.copytree(c.cwd/'Include',include,dirs_exist_ok=True)
    shutil.copy2(c.cwd/'pyconfig.h',include/'pyconfig.h')
    (c.install/'lib').mkdir(exist_ok=True)
    shutil.copy2(c.cwd/('libpython'+'.'.join(p.version.split('.')[:2])+'.a'),c.install/'lib')
    (c.install/'bin').mkdir(exist_ok=True)
    shutil.copy2(ROOT/'tmp/host/bin/python3',c.install/'bin/hostpython3')


def gen_static(c):
    c.chdir('{{ renpy }}')
    c.env('RENPY_STATIC','1')
    c.env('RENPY_REGENERATE_CYTHON','1')
    c.env('CUBISM',str(c.install)) # dynamic Live2D declarations live in Ren'Py source.
    c.run('{{ host }}/bin/python3 setup.py generate')


def package():
    out=ROOT/'dist'/('renpy-runtime-'+LOCK['engine'])
    if out.exists():
        raise RuntimeError('Refusing to replace existing bundle: '+str(out))
    out.mkdir(parents=True)
    py='python'+'.'.join(LOCK['python'].split('.')[:2])
    for arch,platform in [('arm64','iphoneos-arm64'),('sim-arm64','iphonesimulator-arm64')]:
        source=ROOT/'tmp'/('install.ios-'+arch)
        dest=out/'platforms'/platform
        dest.mkdir(parents=True)
        for name in ['librenpython.a','lib'+py+'.a','librenpy.a']:
            shutil.copy2(source/'lib'/name,dest/name)
        shutil.copytree(source/'include'/py,dest/'include'/py)
    from link import link
    link_results=[]
    for arch,sdk in [('arm64','iphoneos'),('sim-arm64','iphonesimulator')]:
        install=ROOT/'tmp'/('install.ios-'+arch)
        link_results.append(link(out/'platforms'/(sdk+'-arm64'),install/'lib',sdk,LOCK['minimum_ios'],
                            ROOT/'tmp'/('runtime-link-'+sdk+'.dylib')))
    resources=out/'resources'
    resources.mkdir()
    shutil.copytree(ROOT/'renpy/renpy',resources/'renpy',ignore=shutil.ignore_patterns('*.pyc','*.rpyc','*.pyx','*.pxd','__pycache__'))
    shutil.copy2(ROOT/'renpy/renpy.py',resources/'main.py')
    (resources/'renpy/vc_version.py').write_text("version = %r\nversion_name = 'RenPyLinter reproducible runtime'\nofficial = False\nnightly = False\nbranch = 'fix'\n" % LOCK['tag'])
    stdlib=resources/'lib'/py
    shutil.copytree(ROOT/'tmp/build/python3.ios-arm64-py3'/('Python-'+LOCK['python'])/'Lib',stdlib,
                    ignore=shutil.ignore_patterns('test','tests','idlelib','tkinter','ensurepip','__pycache__','*.pyc'))
    # Source-only distribution avoids bytecode from the build host leaking into the bundle.
    run(sys.executable,'-m','pip','install','--python-version',LOCK['python'],'--no-compile','--no-deps','--only-binary=:all:',
        '--platform','any','--abi','none','--implementation','py','--require-hashes',
        '--target',stdlib,'-r',ROOT/'tools/runtime/resources-requirements.txt')
    for name,item in LOCK['resource_sources'].items():
        archive=ROOT/'tmp'/item['filename']
        if not archive.exists(): run('curl','--fail','--location',item['url'],'--output',archive)
        if sha(archive)!=item['sha256']: raise RuntimeError('Resource source checksum mismatch: '+name)
        with tarfile.open(archive) as t:
            for member in t.getmembers():
                path=Path(member.name)
                relative=Path(*path.parts[1:])
                if member.isfile() and relative.name.upper().startswith(('LICENSE','COPYING')):
                    (stdlib/(name+'-'+relative.name)).write_bytes(t.extractfile(member).read())
                if member.isfile() and relative.suffix=='.py' and (str(relative)=='pefile.py' or str(relative).startswith('ordlookup/')):
                    dest=stdlib/relative;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(t.extractfile(member).read())
    for fn in ['__init__.py','dylib_manager.py','objc_py_types.py','protocols.py']:
        d=stdlib/'pyobjus'; d.mkdir(exist_ok=True)
        shutil.copy2(ROOT/'tmp/build/pyobjus.ios-arm64-py3/pyobjus/pyobjus'/fn,d/fn)
    for name,source in [('iossupport.py',ROOT/'runtime/iossupport.py'),('sitecustomize.py',ROOT/'runtime/site3.py'),('sysconfig.py',ROOT/'runtime/sysconfig.py'),('brotli.py',ROOT/'source/brotli/brotli.py')]:
        shutil.copy2(source,stdlib/name)
    (stdlib/'lib-dynload').mkdir(exist_ok=True)
    (stdlib/'lib-dynload/README').write_text('Native extension modules are statically registered by init_librenpy.\n')
    run(ROOT/'tmp/host/bin/python3','-c',
        'import pathlib,sys; root=pathlib.Path(sys.argv[1]); [compile(p.read_bytes(),str(p),"exec") for p in root.rglob("*.py")] ',resources)
    licenses=out/'licenses'; licenses.mkdir()
    shutil.copy2(ROOT/'tmp/build/python3.ios-arm64-py3'/('Python-'+LOCK['python'])/'LICENSE',licenses/'Python.txt')
    shutil.copy2(ROOT/'renpy/sphinx/source/license.rst',licenses/'RenPy.rst')
    shutil.copy2(ROOT/'renpy/src/libhydrogen/LICENSE',licenses/'libhydrogen.txt')
    with zipfile.ZipFile(ROOT/'tars'/LOCK['cubism']['filename']) as z:
        (licenses/'Cubism-Core.md').write_bytes(z.read(LOCK['cubism']['directory']+'/Core/LICENSE.md'))
    for project,base in [('pyobjus',ROOT/'tmp/build/pyobjus.ios-arm64-py3/pyobjus'),('brotli',ROOT/'tmp/build/brotli.ios-arm64/brotli-1.1.0')]:
        found=False
        for pattern in ['LICENSE*','COPYING*']:
            for p in base.glob(pattern):
                if p.is_file(): shutil.copy2(p,licenses/(project+'-'+p.name));found=True
        if not found: raise RuntimeError('Missing license: '+project)
    shutil.copy2(ROOT/'docs/runtime-audit.md',out/'HOST-INTEGRATION.md')
    shutil.copy2(ROOT/'tools/runtime/host-reference.json',out/'host-reference.json')
    info={'schema_version':1,'engine_version':LOCK['engine'],'python_version':LOCK['python'],
          'source_commit':output('git','-C',ROOT,'rev-parse','HEAD'),'source_lock':LOCK,'resource_requirements_sha256':sha(ROOT/'tools/runtime/resources-requirements.txt'),
          'build_tool_requirements_sha256':sha(ROOT/'tools/runtime/build-tool-requirements.txt'),
          'patches':{str(p.relative_to(ROOT)):sha(p) for p in [ROOT/'runtime/librenpython3.c',*sorted((ROOT/'tools/runtime').glob('*.py'))]},
          'toolchain':{'xcode':output('xcodebuild','-version'),'clang':output('xcrun','clang','--version'),
                       'autoconf':output('autoconf','--version').splitlines()[0], 'cmake':output('cmake','--version').splitlines()[0]},
          'platforms':['iphoneos-arm64','iphonesimulator-arm64'],'minimum_ios':LOCK['minimum_ios'],
          'resource_format':'source-only','host_adaptations':'main.py and helper_tool.rpy remain host-owned; see HOST-INTEGRATION.md',
          'validation':{'built':True,'link_checks':link_results,'game_execution':False,'relaunch_fixed':False}}
    import re
    info['dynamic_dependencies']={'Live2D':{'required_symbols':sorted(set(re.findall(r'load_live2d_function\(object, "([^"]+)"\)',(ROOT/'renpy/renpy/gl2/live2dcsm.pxi').read_text()))),'binding':'runtime-dlsym','provided_by':'host application'}}
    (out/'build-commands.json').write_text(json.dumps(COMMANDS,indent=2)+'\n')
    info['compiler_commands']='build-commands.json'
    (out/'build-info.json').write_text(json.dumps(info,indent=2)+'\n')
    (out/'SHA256SUMS').write_text(''.join(sha(p)+'  '+str(p.relative_to(out))+'\n' for p in sorted(out.rglob('*')) if p.is_file()))
    run(sys.executable,ROOT/'tools/runtime/verify.py',out)



def preflight(Context):
    args=SimpleNamespace(sdl=False,nostrip=True)
    for arch in ['host','arm64','sim-arm64']:
        c=Context('ios',arch,'3',ROOT,args)
        c.set_names('host' if arch=='host' else 'python','preflight','runtime-preflight')
        expected='TARGET_OS_OSX && !TARGET_OS_IPHONE' if arch=='host' else ('TARGET_OS_IPHONE && '+('TARGET_OS_SIMULATOR' if arch=='sim-arm64' else '!TARGET_OS_SIMULATOR'))
        (c.cwd/'probe.c').write_text('#include <sys/types.h>\n#include <TargetConditionals.h>\n#if !defined(__arm64__) || !('+expected+')\n#error Wrong compilation platform\n#endif\nint main(void) { return 0; }\n')
        c.run('{{ CPP }} {{ CPPFLAGS }} probe.c -o probe.i')
        if arch=='host':
            c.run('{{ CC }} {{ CFLAGS }} {{ LDFLAGS }} probe.c -o probe')
            c.run('./probe')
        else:
            c.run('{{ CC }} {{ CFLAGS }} -c probe.c -o probe.o')
            loads=output('xcrun','otool','-l',c.cwd/'probe.o')
            import re
            platforms=set(re.findall(r'^\s*platform\s+(\w+)',loads,re.M))
            expected_platform={'7','IOSSIMULATOR'} if arch=='sim-arm64' else {'2','IOS'}
            if not platforms or not platforms<=expected_platform: raise RuntimeError('Incorrect preflight Mach-O platform')
            if set(re.findall(r'^\s*minos\s+([\d.]+)',loads,re.M))!={LOCK['minimum_ios']}: raise RuntimeError('Incorrect preflight deployment target')


def main():
    if sys.platform != 'darwin': raise SystemExit('macOS required')
    for sdk in ['iphoneos','iphonesimulator']:
        if output('xcrun','--sdk',sdk,'--show-sdk-version') != LOCK['sdk']:
            raise SystemExit('SDK mismatch: requires '+LOCK['sdk'])
    if output('xcodebuild','-version').splitlines()[0] != 'Xcode '+LOCK['xcode']:
        raise SystemExit('Xcode mismatch')
    os.environ['ZERO_AR_DATE']='1'
    os.environ['SOURCE_DATE_EPOCH']=str(LOCK['source_date_epoch'])
    tool_commands={'autoconf':'autoconf','automake':'automake','libtool':'glibtool','pkgconf':'pkg-config','cmake':'cmake','ninja':'ninja'}
    for name,version in LOCK['build_tools'].items():
        observed=output(tool_commands[name],'--version').splitlines()[0]
        if version not in observed: raise RuntimeError('Build tool drift: '+name+' expected '+version+' got '+observed)
    for name,digest in LOCK['inputs'].items():
        if sha(ROOT/name) != digest: raise SystemExit('Source/patch checksum mismatch: '+name)
    if output('git','-C',ROOT,'status','--porcelain'):
        raise RuntimeError('Build requires a committed, clean source checkout; preserve unrelated changes before building')
    fingerprint={'commit':output('git','-C',ROOT,'rev-parse','HEAD'),'lock_sha256':sha(ROOT/'tools/runtime/lock.json'),
                 'xcode':output('xcodebuild','-version')}
    stamp=ROOT/'tmp/runtime-build-state.json'
    if stamp.exists():
        if json.loads(stamp.read_text())!=fingerprint: raise RuntimeError('Existing build belongs to different inputs; review generated files before an explicit clean')
    elif (ROOT/'tmp/complete').exists() and any((ROOT/'tmp/complete').iterdir()):
        raise RuntimeError('Existing upstream build has no runtime provenance stamp; refusing to reuse it')
    stamp.parent.mkdir(parents=True,exist_ok=True)
    stamp.write_text(json.dumps(fingerprint,indent=2)+'\n')
    fetch('https://github.com/renpy/renpy',LOCK['renpy_commit'],ROOT/'renpy')
    cubism = LOCK['cubism']
    archive = ROOT/'tars'/cubism['filename']
    if not archive.exists(): run('curl','--fail','--location','--retry','2',cubism['url'],'--output',archive)
    if sha(archive) != cubism['sha256']: raise RuntimeError('Cubism checksum mismatch')
    with zipfile.ZipFile(archive) as z:
        for arch in ['arm64','sim-arm64']:
            dest=ROOT/'tmp'/('install.ios-'+arch)/'include'
            dest.mkdir(parents=True,exist_ok=True)
            (dest/'Live2DCubismCore.h').write_bytes(z.read(cubism['directory']+'/Core/include/Live2DCubismCore.h'))
    for name,(url,commit) in LOCK['git_sources'].items():
        fetch(url,commit,ROOT/'tmp/source'/name)
    # Upstream downloads are replaced by immutable checkouts; their patches still apply.
    for name,patch in [('assimp','assimp.diff'),('libyuv','libyuv-noshared.diff')]:
        marker=ROOT/'tmp/source'/name/'.rpl-patched'
        if not marker.exists():
            run('patch','--batch','--fuzz=0','-p1','-i',ROOT/'patches'/patch,cwd=marker.parent)
            marker.write_text(sha(ROOT/'patches'/patch))
    import renpybuild.run as runner
    import renpybuild.task as registry
    from renpybuild.context import Context
    import tasks
    import tasks.python3
    runner.build_environment=native_environment
    upstream_run=runner.run
    def recorded_run(command, context, verbose=False, quiet=False):
        COMMANDS.append({'task':context.task_name,'cwd':str(context.cwd),'argv':shlex.split(command),
                         'compiler_environment':{k:context.environ.get(k) for k in ['CC','CXX','CFLAGS','CXXFLAGS','CPPFLAGS','LDFLAGS','AR','RANLIB']}})
        return upstream_run(command,context,verbose,quiet)
    runner.run=recorded_run
    upstream_group_command=runner.RunCommand
    compiler_slots=threading.Semaphore(min(os.cpu_count() or 2,3))
    class RecordedGroupCommand(upstream_group_command):
        def run(self):
            with compiler_slots:
                super().run()
        def __init__(self,command,context):
            COMMANDS.append({'task':context.task_name,'cwd':str(context.cwd),'argv':shlex.split(context.expand(command)),
                             'compiler_environment':{k:context.environ.get(k) for k in ['CC','CXX','CFLAGS','CXXFLAGS','CPPFLAGS','LDFLAGS','AR','RANLIB']}})
            super().__init__(command,context)
    runner.RunCommand=RecordedGroupCommand
    preflight(Context)
    tasks.python3.common_post=python_post
    selected={'metalangle','zlib','bzip2','xz','brotli','openssl','libffi','libpng','libjpeg_turbo','libwebp',
              'libyuv','aom','libavif','hostpython3','python3','pyobjus','sdl2','sdl2_image','ffmpeg',
              'assimp','fribidi','freetype','harfbuzz','freetypehb','librenpy','renpython'}
    skip={('hostpython3','build_host'),('librenpy','gen_static3'),('python3','pip')}
    for task in registry.tasks:
        if task.name not in selected or task.task == 'download': continue
        if (task.name,task.task) == ('hostpython3','build_host'): task.function=host_python
        if (task.name,task.task) == ('librenpy','gen_static3'): task.function=gen_static
        if (task.name,task.task) == ('python3','pip'): continue
        for arch in ['arm64','sim-arm64']:
            task.run(Context('ios',arch,'3',ROOT,SimpleNamespace(sdl=False,nostrip=True)))
    package()

if __name__=='__main__': main()
