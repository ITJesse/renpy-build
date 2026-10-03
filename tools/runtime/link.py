#!/usr/bin/env python3
"""A real, closed-symbol link; never permits dynamic_lookup or fabricated stubs."""
import argparse,hashlib,json,subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]

def link(core,dependencies,sdk,minimum,destination,framework_root=None):
    sdkroot=subprocess.check_output(['xcrun','--sdk',sdk,'--show-sdk-path'],text=True).strip()
    target='arm64-apple-ios'+minimum+('-simulator' if sdk=='iphonesimulator' else '')
    libraries=sorted(core.glob('*.a'))
    expected={'librenpy.a','librenpython.a'}
    if not expected<={p.name for p in libraries}: raise ValueError('Missing engine libraries')
    command=['xcrun','--sdk',sdk,'clang++','-target',target,'-isysroot',sdkroot,'-dynamiclib',
             '-Wl,-undefined,error','-Wl,-fatal_warnings','-o',str(destination)]
    for p in libraries: command+=['-Wl,-force_load,'+str(p)]
    deps=[p for p in sorted(dependencies.glob('*.a')) if p.name not in {x.name for x in libraries} and p.name not in {'libSDL2main.a','libSDL2_test.a'}]
    command+=list(map(str,deps))
    command+=['-lz','-lbz2','-liconv','-lresolv','-lobjc','-lc++']
    for name in ['Foundation','UIKit','CoreFoundation','CoreGraphics','QuartzCore','OpenGLES','AudioToolbox','CoreAudio','CoreVideo','AVFoundation','GameController','CoreHaptics','CoreMotion','SystemConfiguration','Security','Accelerate']:
        command+=['-framework',name]
    command+=['-F',str(framework_root or dependencies.parent),'-framework','MetalANGLE']
    destination.parent.mkdir(parents=True,exist_ok=True)
    log=destination.with_suffix('.link.log')
    with log.open('w') as f:
        f.write(repr(command)+'\n');f.flush()
        subprocess.run(command,stdout=f,stderr=subprocess.STDOUT,check=True)
    return {'type':'closed-symbol-dylib-link','sdk':sdk,'target':target,'command':command,
            'dependency_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in deps},
            'executable_sha256':hashlib.sha256(destination.read_bytes()).hexdigest(), 'executed':False}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--core',type=Path,required=True);p.add_argument('--dependencies',type=Path,required=True)
    p.add_argument('--sdk',choices=['iphoneos','iphonesimulator'],required=True);p.add_argument('--minimum',default='15.6');p.add_argument('--output',type=Path,required=True);p.add_argument('--framework-root',type=Path)
    a=p.parse_args();print(json.dumps(link(a.core,a.dependencies,a.sdk,a.minimum,a.output,a.framework_root),indent=2))
