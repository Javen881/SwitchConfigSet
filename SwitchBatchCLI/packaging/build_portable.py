"""Assemble downloaded official Windows runtime and binary wheels; compile launcher separately."""
from pathlib import Path
import json
import shutil
import zipfile
import hashlib

ROOT=Path(__file__).resolve().parents[1]
BUILD=ROOT/'build/windows'
DEST=ROOT/'dist/SwitchBatchCLI-Windows-x64'
RUNTIME=DEST/'runtime'
APP=DEST/'application'
if DEST.exists():
    raise SystemExit('Output already exists. Use a new output directory or explicitly remove the old build.')
RUNTIME.mkdir(parents=True)
APP.mkdir()
with zipfile.ZipFile(BUILD/'python-3.12.10-embed-amd64.zip') as z:
    z.extractall(RUNTIME)
site=RUNTIME/'Lib/site-packages';site.mkdir(parents=True)
for wheel in sorted((BUILD/'wheels').glob('*.whl')):
    with zipfile.ZipFile(wheel) as z:
        for name in z.namelist():
            if name.endswith('/'): continue
            parts=Path(name).parts
            if Path(name).is_absolute() or '..' in parts: raise ValueError(name)
            if len(parts)>=3 and parts[0].endswith('.data'):
                if parts[1] not in ('purelib','platlib'): continue
                name='/'.join(parts[2:])
            target=site/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(z.read(name))
(RUNTIME/'python312._pth').write_text('python312.zip\n.\nLib/site-packages\n../application\nimport site\n')
shutil.copytree(ROOT/'switchbatch', APP/'switchbatch',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
shutil.copy2(ROOT/'app.py',APP/'app.py')
shutil.copy2(ROOT/'packaging/windows_entry.py',APP/'windows_entry.py')
for name in ['USAGE.md','VALIDATION.md','README.md']:
    shutil.copy2(ROOT/name,DEST/name)
(DEST/'打开说明.txt').write_text('SwitchBatchCLI Windows x64 便携版\n\n1. 完整解压ZIP，不能在压缩包内直接运行。\n2. 双击 SwitchBatchCLI.exe，无需安装Python。\n3. 不要单独移动EXE；runtime和application目录必须保留。\n4. 默认模拟模式，不连接真实设备。\n5. 任务/日志/导出保存于 %LOCALAPPDATA%\\SwitchBatchCLI。\n6. 启动异常请查看该目录 startup.log。\n7. 本包完成结构与PE架构检查，尚未在Windows实机验证。\n\n目标：Windows 10/11 64位；未数字签名。\n详细操作见 USAGE.md。\n',encoding='utf-8-sig')
(DEST/'THIRD_PARTY.md').write_text('# Bundled components\n\nPython 3.12.10: runtime/LICENSE.txt\n\nQt/PySide6/Shiboken and other dependency licenses are retained in runtime/Lib/site-packages/*.dist-info and package license directories.\n\nThis is an app-local portable distribution. PySide6 is dynamically loaded and distributed unmodified; bundled Python source and DLLs remain replaceable.\n',encoding='utf-8')
sources=[]
for p in [BUILD/'python-3.12.10-embed-amd64.zip',*sorted((BUILD/'wheels').glob('*.whl'))]:
    sources.append({'filename':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'size':p.stat().st_size})
(DEST/'build-inputs.json').write_text(json.dumps(sources,indent=2))
print(DEST)
