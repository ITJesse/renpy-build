#!/usr/bin/env python3
"""A real, closed-symbol link; never permits dynamic_lookup or fabricated stubs."""
import argparse,hashlib,json,subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]

def link(core,dependencies,sdk,minimum,destination,framework_root=None,dependency_overrides=None):
    sdkroot=subprocess.check_output(['xcrun','--sdk',sdk,'--show-sdk-path'],text=True).strip()
    target='arm64-apple-ios'+minimum+('-simulator' if sdk=='iphonesimulator' else '')
    # Modern Apple ld rejects undefined symbols by default; its explicit
    # -undefined error spelling is deprecated. Verify that default with an
    # intentionally unresolved symbol, rather than relaxing diagnostics.
    destination.parent.mkdir(parents=True,exist_ok=True)
    probe=destination.with_suffix('.undefined-probe.c')
    probe.write_text('extern int rpl_deliberately_missing_symbol(void);\nint rpl_link_probe(void) { return rpl_deliberately_missing_symbol(); }\n')
    negative=subprocess.run(['xcrun','--sdk',sdk,'clang','-target',target,'-isysroot',sdkroot,
                             '-dynamiclib',str(probe),'-o',str(destination.with_suffix('.undefined-probe.dylib'))],capture_output=True,text=True)
    if negative.returncode==0 or 'rpl_deliberately_missing_symbol' not in negative.stderr:
        raise RuntimeError('Linker did not prove rejection of undefined symbols: '+negative.stderr)
    libraries=sorted(core.glob('*.a'))
    expected={'librenpy.a','librenpython.a'}
    if len(libraries)!=3 or not expected<={p.name for p in libraries} or sum(p.name.startswith('libpython') for p in libraries)!=1:
        raise ValueError('Expected exactly the three newly built engine libraries')
    command=['xcrun','--sdk',sdk,'clang++','-target',target,'-isysroot',sdkroot,'-dynamiclib',
             '-Wl,-fatal_warnings','-o',str(destination)]
    for p in libraries: command+=['-Wl,-force_load,'+str(p)]
    deps=[p for p in sorted(dependencies.glob('*.a')) if not p.name.startswith(('libpython','librenpy')) and p.name not in {'libSDL2main.a','libSDL2_test.a'}]
    dep_map={p.name:p for p in deps}
    dep_map.update(dependency_overrides or {})
    if any(n.startswith(('libpython','librenpy')) for n in dep_map): raise ValueError('Host core archive cannot be used as a dependency')
    deps=[dep_map[n] for n in sorted(dep_map)]
    command+=list(map(str,deps))
    # zlib/bzip2 are the pinned archives above; clang++ supplies libc++.
    command+=['-liconv','-lresolv','-lobjc']
    for name in ['Foundation','UIKit','CoreFoundation','CoreGraphics','QuartzCore','OpenGLES','AudioToolbox','CoreAudio','CoreVideo','AVFoundation','GameController','CoreHaptics','CoreMotion','SystemConfiguration','Security','Accelerate']:
        command+=['-framework',name]
    command+=['-F',str(framework_root or dependencies.parent),'-framework','MetalANGLE']
    destination.parent.mkdir(parents=True,exist_ok=True)
    log=destination.with_suffix('.link.log')
    with log.open('w') as f:
        f.write(repr(command)+'\n');f.flush()
        result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        f.write(result.stdout)
        print(result.stdout,flush=True)
        result.check_returncode()
    return {'type':'closed-symbol-dylib-link','undefined_symbol_negative_test':'passed','sdk':sdk,'target':target,'command':command,
            'dependency_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in deps},
            'executable_sha256':hashlib.sha256(destination.read_bytes()).hexdigest(), 'executed':False}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--core',type=Path,required=True);p.add_argument('--dependencies',type=Path,required=True)
    p.add_argument('--sdk',choices=['iphoneos','iphonesimulator'],required=True);p.add_argument('--minimum',default='15.6');p.add_argument('--output',type=Path,required=True);p.add_argument('--framework-root',type=Path)
    a=p.parse_args();print(json.dumps(link(a.core,a.dependencies,a.sdk,a.minimum,a.output,a.framework_root),indent=2))
