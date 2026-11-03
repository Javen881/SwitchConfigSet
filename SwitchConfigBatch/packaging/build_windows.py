"""Repackage the already verified app-local Windows runtime with this independent tool."""
from pathlib import Path
import shutil
import subprocess

root=Path(__file__).resolve().parents[1]
source=root.parent/'SwitchBatchCLI'
output=root/'dist/SwitchConfigBatch-Windows-x64'
if output.exists():raise SystemExit('Output exists; explicitly choose a new build directory before rebuilding.')
output.mkdir(parents=True)
shutil.copytree(source/'dist/SwitchBatchCLI-Windows-x64/runtime',output/'runtime')
app=output/'application';app.mkdir()
for name in ('switchbatch','configbatch'):
    shutil.copytree(root/name,app/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
shutil.copy2(root/'app.py',app/'app.py')
entry=(source/'packaging/windows_entry.py').read_text().replace('SwitchBatchCLI','SwitchConfigBatch')
(app/'windows_entry.py').write_text(entry)
for name in ('README.md','VALIDATION.md'):shutil.copy2(root/name,output/name)
shutil.copytree(root/'examples',output/'examples')
shutil.copy2(source/'dist/SwitchBatchCLI-Windows-x64/THIRD_PARTY.md',output/'THIRD_PARTY.md')
shutil.copy2(source/'dist/SwitchBatchCLI-Windows-x64/build-inputs.json',output/'runtime-build-inputs.json')
(output/'打开说明.txt').write_text('SwitchConfigBatch Windows x64\n\n完整解压后双击 SwitchConfigBatch.exe。无需安装Python。\n不要单独移走EXE，runtime和application目录必须保留。\n默认Telnet，模板为空。点击“载入离线演示”可先体验完整流程，不连接网络。\n实际设备需填写正确CLI命令及查询规则；详细操作见README.md。\n数据目录：%LOCALAPPDATA%\\SwitchConfigBatch\n启动异常：该目录startup.log\nWindows实机启动尚未验证。\n',encoding='utf-8-sig')
for name in ('launcher.c','launcher.rc','launcher.manifest'):
    (root/'packaging'/name).write_text((source/'packaging'/name).read_text().replace('SwitchBatchCLI','SwitchConfigBatch'))
(root/'build/windows').mkdir(parents=True,exist_ok=True)
print(output)
