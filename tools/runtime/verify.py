#!/usr/bin/env python3
"""Fail closed on hashes, architecture, platform, deployment target and entry points."""
import hashlib,json,re,subprocess,sys
from pathlib import Path

def command(*args):
    return subprocess.check_output(args,text=True)

def verify(root):
    info=json.loads((root/'build-info.json').read_text())
    listed={}
    for line in (root/'SHA256SUMS').read_text().splitlines():
        digest,name=line.split('  ',1)
        p=root/name
        if p.resolve().is_relative_to(root.resolve()) is False: raise ValueError('Unsafe checksum path')
        if hashlib.sha256(p.read_bytes()).hexdigest()!=digest: raise ValueError('Checksum mismatch: '+name)
        listed[name]=digest
    actual={str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() and p.name!='SHA256SUMS'}
    if actual!=set(listed): raise ValueError('Manifest does not cover every file')
    py='python'+'.'.join(info['python_version'].split('.')[:2])
    if set(p.name for p in (root/'platforms').iterdir())!={'iphoneos-arm64','iphonesimulator-arm64'}: raise ValueError('Unexpected platform directory')
    if list((root/'resources').rglob('*.so')) or list((root/'resources').rglob('*.dylib')): raise ValueError('Host native module leaked into resources')
    for platform,expected in [('iphoneos-arm64','IOS'),('iphonesimulator-arm64','IOSSIMULATOR')]:
        if set(p.name for p in (root/'platforms'/platform).glob('*.a'))!={'librenpython.a','lib'+py+'.a','librenpy.a'}: raise ValueError('Unexpected archive set')
        for name,symbol in [('librenpython.a','_launcher_main'),('lib'+py+'.a','_Py_Initialize'),('librenpy.a','_init_librenpy')]:
            lib=root/'platforms'/platform/name
            if command('xcrun','lipo','-archs',str(lib)).strip()!='arm64': raise ValueError('Not arm64-only: '+str(lib))
            loads=command('xcrun','otool','-l',str(lib))
            platforms=re.findall(r'^\s*platform\s+(\w+)',loads,re.M)
            wanted={expected, '2' if expected=='IOS' else '7'}
            if not platforms or not set(platforms)<=wanted: raise ValueError('Wrong platform: '+str(lib))
            mins=re.findall(r'^\s*minos\s+([\d.]+)',loads,re.M)
            if not mins or set(mins)!={info['minimum_ios']}: raise ValueError('Wrong deployment target: '+str(lib)+' '+str(set(mins)))
            symbols=command('xcrun','nm','-gU',str(lib))
            if not re.search(r'\b'+re.escape(symbol)+r'$',symbols,re.M): raise ValueError('Missing '+symbol)
        python_symbols=command('xcrun','nm','-gU',str(root/'platforms'/platform/('lib'+py+'.a')))
        prefix='_PyInit_' if info['python_version'].startswith('3') else '_init'
        for module in ['_ssl','_hashlib','_ctypes','_socket','zlib']:
            if not re.search(r'\b'+re.escape(prefix+module)+r'$',python_symbols,re.M): raise ValueError('Missing required Python module '+module)
        if info['python_version'].startswith('3'):
            launcher_symbols=command('xcrun','nm','-gU',str(root/'platforms'/platform/'librenpython.a'))
            if not re.search(r'\b_launcher_main_wide$',launcher_symbols,re.M): raise ValueError('Missing wide launcher entry point')
            if b'RenPyPythonSession' not in (root/'platforms'/platform/'librenpython.a').read_bytes(): raise ValueError('Missing allocator patch')
        if not (root/'platforms'/platform/'include'/py/'pyconfig.h').is_file(): raise ValueError('Missing target pyconfig.h')
    for name in ['main.py','renpy/__init__.py','renpy/common/00start.rpy','lib/'+py+'/encodings/__init__.py','lib/'+py+'/site.py','lib/'+py+'/pyobjus/__init__.py','lib/'+py+'/certifi/cacert.pem']:
        if not (root/'resources'/name).is_file(): raise ValueError('Missing resource: '+name)
    if list((root/'resources').rglob('*.pyc')) or list((root/'resources').rglob('*.rpyc')): raise ValueError('Unexpected bytecode')
    print('PASS: checksums, arm64-only, platform, minimum OS, exports, headers and resources')
if __name__=='__main__': verify(Path(sys.argv[1]))
