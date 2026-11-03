import csv
import codecs
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import threading
from datetime import datetime
from uuid import uuid4
import zipfile
from dataclasses import asdict


def now():
    return datetime.now().astimezone().isoformat(timespec='milliseconds')


def append(path, data):
    with open(path, 'ab') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def atomic_json(path, value):
    tmp = path.with_suffix('.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def jsonline(value):
    return (json.dumps(value, ensure_ascii=False) + '\n').encode('utf-8')


def csvsafe(value):
    value = str(value)
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value


class Archive:
    def __init__(self, base):
        self.base = Path(base).resolve()
        self.base.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.base / 'data').mkdir(exist_ok=True, mode=0o700)
        self.db = self.base / 'data/tasks.sqlite3'
        self.lock = threading.RLock()
        with sqlite3.connect(self.db) as con:
            con.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, path TEXT, state TEXT, created TEXT)')

    def index(self, task):
        with self.lock, sqlite3.connect(self.db) as con:
            con.execute('INSERT OR REPLACE INTO tasks VALUES (?,?,?,?)',
                        (task['task_id'], task['path'], task['status'], task['created_at']))

    def history(self):
        with sqlite3.connect(self.db) as con:
            return con.execute('SELECT id,path,state,created FROM tasks ORDER BY created DESC').fetchall()

    def recover(self):
        for path in (self.base / 'logs').glob('*/*/task.json'):
            task = json.loads(path.read_text('utf-8'))
            if task['status'] in ('排队', '运行'):
                task['status'] = '中断/待核对'
                task['ended_at'] = now()
                task['error'] = '上次进程未正常结束；设备执行结果未知，禁止自动续发'
                for d in task['devices']:
                    if d['status'] in ('排队', '运行'):
                        d['status'] = '未知'
                    for c in d['commands']:
                        if c['status'] == '运行':
                            c.update(status='未知', output_complete=False,
                                     incomplete_reason='进程中断，已接收回显见原始流，设备执行情况待核对')
                        elif c['status'] == '排队':
                            c['status'] = '未执行'
                        if c['status'] == '未知' and c.get('rx_start') is not None:
                            raw = path.parent / d['log_path'] / 'session.rx.bin'
                            c['rx_end'] = raw.stat().st_size
                            decoder = codecs.getincrementaldecoder(task['settings']['encoding'])(errors='replace')
                            output = path.parent / c['output_path']
                            with raw.open('rb') as src, output.open('w', encoding='utf-8') as dst:
                                src.seek(c['rx_start'])
                                for block in iter(lambda: src.read(65536), b''):
                                    dst.write(decoder.decode(block))
                                dst.write(decoder.decode(b'', final=True))
                            if c['kind'] == '查询':
                                import shutil
                                collected = path.parent / d['log_path'] / 'collected' / output.name
                                shutil.copyfile(output, collected)
                                c['collected_path'] = str(collected.relative_to(path.parent))
                results = path.parent / 'command_results.jsonl'
                if results.exists():
                    results.rename(path.parent / 'command_results.before-recovery.jsonl')
                with results.open('wb') as f:
                    for d in task['devices']:
                        for c in d['commands']:
                            f.write(jsonline(c))
                    f.flush()
                    os.fsync(f.fileno())
                atomic_json(path, task)
                write_summary(path.parent, task)
            self.index(task)

    def export(self, task_path):
        root = Path(task_path).resolve()
        if not root.is_relative_to(self.base / 'logs'):
            raise ValueError('不是本工具的任务目录')
        task = json.loads((root / 'task.json').read_text('utf-8'))
        if task['status'] in ('运行', '排队'):
            raise ValueError('请等待任务结束再导出')
        files = [p for p in root.rglob('*') if p.is_file() and not p.is_symlink() and p.suffix != '.tmp']
        manifest = {'task_id': task['task_id'], 'exported_at': now(), 'status': task['status'], 'files': []}
        out = self.base / 'exports'
        out.mkdir(exist_ok=True, mode=0o700)
        zip_path = out / f"{task['task_id']}_{uuid4().hex[:8]}.zip"
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
            for p in files:
                name = p.relative_to(root).as_posix()
                digest = hashlib.sha256()
                with p.open('rb') as src, z.open(name, 'w') as dst:
                    for block in iter(lambda: src.read(1024 * 1024), b''):
                        digest.update(block)
                        dst.write(block)
                manifest['files'].append({'path': name, 'size': p.stat().st_size, 'sha256': digest.hexdigest()})
            z.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        with zipfile.ZipFile(zip_path) as z:
            if z.testzip():
                raise IOError('ZIP校验失败')
            for item in manifest['files']:
                digest = hashlib.sha256()
                with z.open(item['path']) as f:
                    for block in iter(lambda: f.read(1024 * 1024), b''):
                        digest.update(block)
                if digest.hexdigest() != item['sha256'] or z.getinfo(item['path']).file_size != item['size']:
                    raise IOError('导出清单校验失败')
        return zip_path


def write_summary(root, task):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(['IP', '协议', '状态', '完成', '失败或不完整', '未执行', '业务验证', '错误'])
    for d in task['devices']:
        states = [c['status'] for c in d['commands']]
        writer.writerow([csvsafe(d['ip']), d['protocol'], d['status'], states.count('完成'),
                         sum(s not in ('完成', '排队', '未执行') for s in states),
                         states.count('未执行') + states.count('排队'), '未验证', csvsafe(d.get('error', ''))])
    (root / 'summary.csv').write_text('\ufeff' + stream.getvalue(), encoding='utf-8')


class TaskStore:
    def __init__(self, archive, targets, commands, settings, input_snapshot, callback=None):
        self.archive = archive
        self.lock = threading.RLock()
        self.callback = callback or (lambda event: None)
        self.seq = 0
        self.failed = threading.Event()
        task_id = datetime.now().strftime('%Y%m%d_%H%M%S_') + uuid4().hex[:10]
        self.root = archive.base / 'logs' / datetime.now().strftime('%Y-%m-%d') / task_id
        self.root.mkdir(parents=True, mode=0o700)
        self.task = dict(task_id=task_id, created_at=now(), started_at=None, ended_at=None,
                         timezone=str(datetime.now().astimezone().tzinfo), status='排队',
                         path=str(self.root), input_snapshot=input_snapshot, settings=asdict(settings),
                         commands=[asdict(c) for c in commands], devices=[], business_validation='未验证')
        for target in targets:
            did = uuid4().hex
            folder = self.root / 'devices' / did
            (folder / 'commands').mkdir(parents=True, mode=0o700)
            (folder / 'collected').mkdir(mode=0o700)
            (folder / 'session.rx.bin').touch(mode=0o600)
            d = dict(asdict(target), device_run_id=did, task_id=task_id, status='排队',
                     authentication='未尝试', started_at=None, ended_at=None, error='',
                     log_path=str(folder.relative_to(self.root)), commands=[])
            for i, c in enumerate(commands, 1):
                d['commands'].append(dict(asdict(c), command_run_id=uuid4().hex, device_run_id=did,
                    sequence=i, status='排队', sent_at=None, ended_at=None, duration=None,
                    output_complete=False, incomplete_reason='尚未执行', error='', basis='',
                    rx_start=None, rx_end=None, collected_path=None, output_path=f'{d["log_path"]}/commands/{i:06d}.txt'))
            self.task['devices'].append(d)
        (self.root / 'commands.txt').write_text('\n'.join(c.text for c in commands) + '\n', encoding='utf-8')
        self.save()

    def save(self):
        with self.lock:
            try:
                atomic_json(self.root / 'task.json', self.task)
                self.archive.index(self.task)
            except Exception:
                self.failed.set()
                raise

    def event(self, device, command, direction, data=b'', text='', auxiliary=False):
        with self.lock:
            if self.failed.is_set():
                raise IOError('日志写入已失败，禁止继续发送')
            event = dict(sequence=self.seq + 1, time=now(), task_id=self.task['task_id'],
                         device_run_id=device['device_run_id'],
                         command_run_id=command['command_run_id'] if command else None,
                         direction=direction, auxiliary=auxiliary, text=text)
            self.seq += 1
            folder = self.root / device['log_path']
            try:
                if direction == 'RX':
                    raw = folder / 'session.rx.bin'
                    event.update(offset=raw.stat().st_size, length=len(data))
                    append(raw, data)
                elif data:
                    # TX is reversible as hex; authentication writes never call this method.
                    event.update(length=len(data), data_hex=data.hex())
                append(self.root / 'events.jsonl', jsonline(event))
                readable = f"[{event['time']}] [{direction}] {text}\n".encode('utf-8')
                append(folder / 'transcript.txt', readable)
            except Exception:
                self.failed.set()
                raise
        self.callback(event)

    def result(self, d, c):
        with self.lock:
            try:
                append(self.root / 'command_results.jsonl', jsonline(c))
                self.save()
            except Exception:
                self.failed.set()
                raise
        self.callback(dict(direction='STATUS', device_run_id=d['device_run_id'],
                           text=f"{d['ip']} #{c['sequence']} {c['status']}", time=now()))

    def finish(self):
        with self.lock:
            for d in self.task['devices']:
                for c in d['commands']:
                    if c['status'] == '排队':
                        c['status'] = '未执行'
                        c['incomplete_reason'] = '设备后续队列未执行'
                        append(self.root / 'command_results.jsonl', jsonline(c))
            self.task['ended_at'] = now()
            states = [d['status'] for d in self.task['devices']]
            self.task['status'] = '完成' if all(x == '完成' for x in states) else '结束（有未完成项）'
            write_summary(self.root, self.task)
            self.save()
