"""Byte-preserving adapters. Authentication is intentionally outside CLI logging."""
import asyncio
from dataclasses import dataclass
import hashlib
import base64
import re
import socket
import time


@dataclass
class Credentials:
    username: str = ''
    password: str = ''
    key_filename: str = ''


class SSH:
    def __init__(self, target, credentials, settings):
        import paramiko
        self.client = paramiko.SSHClient()
        self.client.load_system_host_keys()
        if settings.known_hosts:
            self.client.load_host_keys(settings.known_hosts)

        class Strict(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self, client, hostname, key):
                fingerprint = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')
                raise paramiko.SSHException(f'未信任SSH主机密钥 {hostname} {key.get_name()} SHA256:{fingerprint}；请核对后提供known_hosts文件')

        self.client.set_missing_host_key_policy(Strict())
        try:
            self.client.connect(target.ip, port=target.port, username=credentials.username,
                password=credentials.password or None, key_filename=credentials.key_filename or None,
                timeout=settings.connect_timeout, auth_timeout=settings.connect_timeout,
                banner_timeout=settings.connect_timeout, allow_agent=False, look_for_keys=False)
            self.channel = self.client.invoke_shell(width=160, height=48)
            self.channel.settimeout(0.1)
        except Exception:
            self.client.close()
            raise

    def read(self):
        try:
            value = self.channel.recv(65536)
        except socket.timeout:
            return None
        if value == b'':
            raise EOFError('SSH连接关闭')
        return value

    def write(self, data):
        self.channel.sendall(data)

    def close(self):
        self.client.close()


class Telnet:
    def __init__(self, target, credentials, settings):
        import telnetlib3
        self.loop = asyncio.new_event_loop()
        self.writer = None
        self.pending = b''
        try:
            async def connect():
                return await asyncio.wait_for(telnetlib3.open_connection(
                    target.ip, target.port, encoding=False, connect_minwait=0,
                    connect_maxwait=0.3, term='vt100'), settings.connect_timeout)
            self.reader, self.writer = self.loop.run_until_complete(connect())
            # Authentication bytes, including echoed secrets, never enter normal evidence files.
            tail = b''
            deadline = time.monotonic() + settings.connect_timeout
            sent_user = sent_password = False
            while time.monotonic() < deadline:
                block = self._read()
                if block is None:
                    continue
                tail = (tail + block)[-16384:]
                text = tail.decode(settings.encoding, errors='replace')
                if re.search(r'(?i)(authentication failed|login incorrect|access denied)', text):
                    raise PermissionError('Telnet认证失败')
                if re.search(r'(?im)(?:login|username|user name)\s*:\s*$', text):
                    if sent_user:
                        raise PermissionError('Telnet重复登录提示，认证失败')
                    self.write(credentials.username.encode(settings.encoding) + b'\r\n')
                    sent_user = True
                    tail = b''
                elif re.search(r'(?im)password\s*:\s*$', text):
                    if sent_password:
                        raise PermissionError('Telnet重复密码提示，认证失败')
                    self.write(credentials.password.encode(settings.encoding) + b'\r\n')
                    sent_password = True
                    tail = b''
                else:
                    match = re.search(settings.prompt, text)
                    if match:
                        # Keep only the post-auth prompt, never echo from login phase.
                        self.pending = match.group().encode(settings.encoding)
                        return
            raise TimeoutError('Telnet认证/初始提示符超时')
        except Exception:
            self.close()
            raise

    def _read(self):
        async def receive():
            return await asyncio.wait_for(self.reader.read(65536), 0.1)
        try:
            value = self.loop.run_until_complete(receive())
        except asyncio.TimeoutError:
            return None
        if not value:
            raise EOFError('Telnet连接关闭')
        return value

    def read(self):
        if self.pending:
            result, self.pending = self.pending, b''
            return result
        return self._read()

    def write(self, data):
        self.writer.write(data)
        self.loop.run_until_complete(self.writer.drain())

    def close(self):
        if self.writer:
            self.writer.close()
            try:
                self.loop.run_until_complete(asyncio.wait_for(self.writer.wait_closed(), 1))
            except Exception:
                pass
        if not self.loop.is_closed():
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.close()


class Mock:
    def __init__(self, target, credentials, settings):
        self.encoding = settings.encoding
        self.pending = [b'DEMO switch\r\nSwitch#']
        self.disconnected = False
        self.paging = False
        self.closed = False

    def write(self, data):
        if data == b' ' and self.paging:
            self.pending.extend([b'\r\npage 2\r\n', b'Switch#'])
            self.paging = False
            return
        command = data.decode(self.encoding).strip()
        self.pending.append(data)
        if command == 'demo timeout':
            self.pending.append('部分输出，随后模拟无响应\r\n'.encode(self.encoding))
        elif command == 'demo disconnect':
            self.pending.append(b'partial output before disconnect\r\n')
            self.disconnected = True
        elif command == 'demo error':
            self.pending.append(b'% Invalid input detected\r\nSwitch#')
        elif command == 'demo confirm':
            self.pending.append(b'Continue? [y/n]')
        elif command == 'demo pages':
            self.pending.append(b'page 1\r\n--More--')
            self.paging = True
        elif command == 'demo long':
            self.pending.extend([(f'{i:05d} 接口/VLAN/生成树回显\r\n').encode(self.encoding) for i in range(4000)])
            self.pending.append(b'Switch#')
        else:
            self.pending.append(f'模拟执行：{command}\r\nSwitch#'.encode(self.encoding))

    def read(self):
        if self.pending:
            return self.pending.pop(0)
        if self.disconnected:
            raise EOFError('模拟断连')
        time.sleep(0.01)
        return None

    def close(self):
        self.closed = True


def factory(target, credentials, settings):
    return {'ssh': SSH, 'telnet': Telnet, 'mock': Mock}[target.protocol](target, credentials, settings)


def probe(ip, port, timeout=1.5):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return '开放（未认证）'
    except OSError as exc:
        return '不可连接: ' + str(exc)
