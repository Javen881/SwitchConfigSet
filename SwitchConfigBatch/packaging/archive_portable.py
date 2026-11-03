from pathlib import Path
import hashlib
import json
import struct
import zipfile

root=Path(__file__).resolve().parents[1]
folder=root/'dist/SwitchConfigBatch-Windows-x64'
required=['SwitchConfigBatch.exe','runtime/pythonw.exe','runtime/python312.dll','runtime/python312._pth',
          'runtime/Lib/site-packages/PySide6/QtWidgets.pyd',
          'runtime/Lib/site-packages/PySide6/plugins/platforms/qwindows.dll',
          'application/windows_entry.py','application/app.py']
for name in required:
    assert (folder/name).is_file(),name
pe_count=0
for p in folder.rglob('*'):
    if p.suffix.lower() in ('.exe','.dll','.pyd'):
        with p.open('rb') as f:
            header=f.read(64)
            assert header[:2]==b'MZ',p
            offset=struct.unpack_from('<I',header,60)[0]
            f.seek(offset)
            sig=f.read(6)
            assert sig[:4]==b'PE\x00\x00' and struct.unpack_from('<H',sig,4)[0]==0x8664,p
            pe_count+=1
manifest=[]
for p in sorted(folder.rglob('*')):
    if p.is_file() and p.name!='package-manifest.json':
        manifest.append({'path':p.relative_to(folder).as_posix(),'size':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
(folder/'package-manifest.json').write_text(json.dumps({'architecture':'Windows x64','windows_runtime_tested':False,'pe_files_checked':pe_count,'files':manifest},ensure_ascii=False,indent=2))
output=root/'dist/SwitchConfigBatch-Windows-x64.zip'
with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
    for p in sorted(folder.rglob('*')):
        if p.is_file():z.write(p,f'{folder.name}/{p.relative_to(folder).as_posix()}')
with zipfile.ZipFile(output) as z:
    assert z.testzip() is None
    for entry in manifest:
        data=z.read(f'{folder.name}/{entry["path"]}')
        assert len(data)==entry['size'] and hashlib.sha256(data).hexdigest()==entry['sha256']
digest=hashlib.sha256(output.read_bytes()).hexdigest()
output.with_suffix('.zip.sha256').write_text(f'{digest}  {output.name}\n')
print(f'{pe_count} x64 PE files validated; {len(manifest)} files hashed')
print(f'{output}: {output.stat().st_size/1024/1024:.1f} MiB')
print('SHA256',digest)
