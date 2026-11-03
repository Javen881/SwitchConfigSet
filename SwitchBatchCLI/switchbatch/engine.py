import codecs
from concurrent.futures import ThreadPoolExecutor, as_completed
import re
import threading
import time
from pathlib import Path
from .models import Target, validate_targets
from .store import TaskStore, append, now
from .transports import factory

ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


class Halt(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class Batch:
    def __init__(self, archive, targets, commands, settings, credentials,
                 input_snapshot='', callback=None, transport_factory=factory):
        validate_targets(targets)
        settings.validate()
        if not commands:
            raise ValueError('命令清单为空')
        for c in commands:
            c.text.encode(settings.encoding, errors='strict')
        self.settings, self.credentials = settings, credentials
        self.stop = threading.Event()
        self.transport_factory = transport_factory
        self.store = TaskStore(archive, targets, commands, settings, input_snapshot, callback)

    def cancel(self):
        self.stop.set()

    def check(self):
        if self.store.failed.is_set():
            raise OSError('日志写入失败；停止任务后续发送')
        if self.stop.is_set():
            raise Halt('取消', '用户取消；当前命令在设备侧是否完成未知')

    def receive(self, session, d, c, timeout, decoder):
        deadline = time.monotonic() + timeout
        tail = ''
        matched_at = None
        error_seen = False
        while True:
            self.check()
            if time.monotonic() > deadline:
                raise Halt('超时', '等待提示符超时；保留部分回显，设备执行状态未知')
            try:
                block = session.read()
            except EOFError as exc:
                raise Halt('断连', str(exc)) from exc
            if block:
                text = decoder.decode(block)
                self.store.event(d, c, 'RX', block, text=text)
                tail = (tail + text)[-32768:]
                clean = ANSI.sub('', tail).replace('\x08', '')
                # Do not classify the echoed command itself as a CLI error.
                inspected = '\n'.join(line for line in clean.splitlines()
                                      if not c or line.strip() != c['text'].strip())
                error_seen |= bool(re.search(self.settings.error, inspected))
                if re.search(self.settings.interaction, clean):
                    raise Halt('未知', '检测到交互确认/密码提示，停止后续命令；未自动回答')
                if re.search(self.settings.pager, clean):
                    self.store.event(d, c, 'TX_INTENT', b' ', text='分页继续：Space', auxiliary=True)
                    self.check()
                    session.write(b' ')
                    self.store.event(d, c, 'TX', b' ', text='分页继续：Space', auxiliary=True)
                    tail = ''
                    matched_at = None
                    continue
                matches = list(re.finditer(self.settings.prompt, clean))
                match = matches[-1] if matches else None
                matched_at = time.monotonic() if match and not clean[match.end():].strip() else None
            elif matched_at is not None and time.monotonic() - matched_at >= 0.15:
                return error_seen

    def materialize(self, d, c):
        raw = self.store.root / d['log_path'] / 'session.rx.bin'
        c['rx_end'] = raw.stat().st_size
        output = self.store.root / c['output_path']
        decoder = codecs.getincrementaldecoder(self.settings.encoding)(errors='replace')
        with raw.open('rb') as src, output.open('w', encoding='utf-8') as dst:
            src.seek(c['rx_start'])
            remaining = c['rx_end'] - c['rx_start']
            while remaining:
                block = src.read(min(65536, remaining))
                if not block:
                    raise IOError('原始回显范围不完整')
                dst.write(decoder.decode(block))
                remaining -= len(block)
            dst.write(decoder.decode(b'', final=True))
        if c['kind'] == '查询':
            import shutil
            collected = self.store.root / d['log_path'] / 'collected' / output.name
            shutil.copyfile(output, collected)
            c['collected_path'] = str(collected.relative_to(self.store.root))

    def device(self, d):
        session = None
        c = None
        started = None
        try:
            self.check()
            d.update(status='运行', started_at=now(), authentication='进行中')
            self.store.save()
            self.store.event(d, None, 'SYS', text=f"连接 {d['ip']}:{d['port']} ({d['protocol']})")
            target = Target(d['ip'], d['protocol'], d['port'], d['device_type'])
            session = self.transport_factory(target, self.credentials, self.settings)
            d['authentication'] = '模拟' if d['protocol'] == 'mock' else '已认证'
            decoder = codecs.getincrementaldecoder(self.settings.encoding)(errors='replace')
            self.receive(session, d, None, self.settings.connect_timeout, decoder)
            for c in d['commands']:
                self.check()
                started = time.monotonic()
                c.update(status='运行', rx_start=(self.store.root / d['log_path'] / 'session.rx.bin').stat().st_size,
                         incomplete_reason='命令执行中')
                self.store.save()
                data = c['text'].encode(self.settings.encoding) + b'\r\n'
                self.store.event(d, c, 'TX_INTENT', data, text=c['text'])
                self.check()
                session.write(data)
                c['sent_at'] = now()
                self.store.event(d, c, 'TX', data, text=c['text'])
                error = self.receive(session, d, c, self.settings.command_timeout, decoder)
                c.update(status='设备报错' if error else '完成', ended_at=now(),
                         duration=time.monotonic() - started, output_complete=True, incomplete_reason='',
                         basis='收到匹配提示符；CLI错误规则命中' if error else '收到匹配提示符；业务未验证',
                         error='设备回显命中错误规则' if error else '')
                self.materialize(d, c)
                self.store.result(d, c)
                if error:
                    raise Halt('设备报错', '命令报错，停止该设备后续队列')
                c = None
            d['status'] = '完成'
        except Exception as exc:
            status = exc.status if isinstance(exc, Halt) else ('超时' if isinstance(exc, TimeoutError) else '失败')
            # Exception messages from a third-party library are not copied into evidence blindly.
            message = str(exc) if isinstance(exc, (Halt, OSError, TimeoutError)) else f'{type(exc).__name__}: {str(exc)}'
            for secret in (self.credentials.password,):
                if secret:
                    message = message.replace(secret, '[REDACTED]')
            d.update(status=status, error=message)
            if d['authentication'] == '进行中':
                d['authentication'] = '未完成'
            if c and c['status'] == '运行':
                c.update(status=status, ended_at=now(), duration=time.monotonic() - started,
                         output_complete=False, incomplete_reason=message, error=message)
                try:
                    self.materialize(d, c)
                    self.store.result(d, c)
                except Exception:
                    self.store.failed.set()
            try:
                self.store.event(d, c, 'SYS', text=f'{status}: {message}')
            except Exception:
                self.store.failed.set()
        finally:
            if session:
                try:
                    session.close()
                except Exception:
                    pass
            d['ended_at'] = now()
            for item in d['commands']:
                if item['status'] == '排队':
                    item['incomplete_reason'] = d.get('error') or '任务未执行'
            try:
                self.store.save()
            except Exception:
                self.store.failed.set()

    def run(self):
        self.store.task.update(status='运行', started_at=now())
        self.store.save()
        with ThreadPoolExecutor(max_workers=self.settings.concurrency) as pool:
            futures = [pool.submit(self.device, d) for d in self.store.task['devices']]
            for future in as_completed(futures):
                future.result()
        if self.store.failed.is_set():
            raise IOError(f'日志写入失败，未宣称完整留痕；检查磁盘和目录：{self.store.root}')
        self.store.finish()
        return self.store.root
