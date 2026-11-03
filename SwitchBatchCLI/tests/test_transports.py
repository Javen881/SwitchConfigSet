"""Real SSH/Telnet traffic, restricted to loopback simulator servers."""
import asyncio
import socket
import threading
from pathlib import Path
import time
import paramiko
import pytest
import telnetlib3
from switchbatch.engine import Batch
from switchbatch.models import Target, Settings, parse_commands
from switchbatch.transports import Credentials
from switchbatch.store import Archive


class SSHServer(paramiko.ServerInterface):
    def __init__(self):
        self.ready=threading.Event()
    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if (username,password)==('tester','private-test-password') else paramiko.AUTH_FAILED
    def get_allowed_auths(self, username): return 'password'
    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind=='session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
    def check_channel_pty_request(self,*args): return True
    def check_channel_shell_request(self,channel): self.ready.set(); return True


@pytest.fixture
def ssh_server():
    listener=socket.socket(); listener.bind(('127.0.0.1',0)); listener.listen(1); listener.settimeout(5)
    port=listener.getsockname()[1]; key=paramiko.RSAKey.generate(2048)
    sent=[]
    def serve():
        transport=None
        try:
            conn,_=listener.accept(); transport=paramiko.Transport(conn); transport.add_server_key(key)
            server=SSHServer(); transport.start_server(server=server)
            channel=transport.accept(5)
            if channel is None or not server.ready.wait(5): return
            channel.sendall(b'Switch#'); buf=b''
            while True:
                block=channel.recv(4096)
                if not block: break
                buf+=block
                while b'\n' in buf:
                    command,buf=buf.split(b'\n',1); sent.append(command)
                    channel.sendall(command+b'\n'+('真实SSH中文回显\r\n').encode()+b'Switch#')
        except (EOFError,OSError,paramiko.SSHException):
            pass
        finally:
            if transport: transport.close()
    thread=threading.Thread(target=serve,daemon=True); thread.start()
    yield port,key,sent
    listener.close(); thread.join(6)


def test_ssh_loopback_preserves_raw(tmp_path,ssh_server):
    port,key,sent=ssh_server
    hosts=tmp_path/'known_hosts'; hosts.write_text(f'[127.0.0.1]:{port} {key.get_name()} {key.get_base64()}\n')
    batch=Batch(Archive(tmp_path),[Target('127.0.0.1','ssh',port)],parse_commands('show version\nshow vlan','查询'),
                Settings(known_hosts=str(hosts),command_timeout=3),Credentials('tester','private-test-password'))
    root=batch.run(); d=batch.store.task['devices'][0]
    assert d['status']=='完成',d['error']
    assert len(sent)==2
    assert '真实SSH中文回显' in (root/d['log_path']/'session.rx.bin').read_bytes().decode()
    assert 'private-test-password' not in (root/'task.json').read_text()


def test_ssh_unknown_key_rejected(tmp_path,ssh_server):
    port,_,sent=ssh_server
    batch=Batch(Archive(tmp_path),[Target('127.0.0.1','ssh',port)],parse_commands('never send'),Settings(connect_timeout=2),Credentials('tester','private-test-password'))
    batch.run(); d=batch.store.task['devices'][0]
    assert 'SHA256:' in d['error']
    assert d['commands'][0]['status']=='未执行'
    assert sent==[]


def test_ssh_auth_failure(tmp_path,ssh_server):
    port,key,sent=ssh_server
    hosts=tmp_path/'known_hosts'; hosts.write_text(f'[127.0.0.1]:{port} {key.get_name()} {key.get_base64()}\n')
    batch=Batch(Archive(tmp_path),[Target('127.0.0.1','ssh',port)],parse_commands('never send'),
                Settings(known_hosts=str(hosts),connect_timeout=2),Credentials('tester','wrong-password'))
    batch.run(); d=batch.store.task['devices'][0]
    assert d['status']=='失败'
    assert d['commands'][0]['status']=='未执行'
    assert sent==[]


def test_telnet_loopback_no_login_secret(tmp_path):
    loop=asyncio.new_event_loop(); ready=threading.Event(); state={}; received=[]
    async def shell(reader,writer):
        try:
            writer.write(b'Username: '); await writer.drain()
            user=await reader.readline()
            writer.write(b'Password: '); await writer.drain()
            password=await reader.readline()
            # Deliberately echo authentication input to verify it is excluded from CLI logs.
            writer.write(password+b'\r\nSwitch#'); await writer.drain()
            while True:
                command=await reader.readline()
                if not command: break
                received.append(command)
                writer.write(command+'Telnet中文数据\r\n'.encode()+b'Switch#'); await writer.drain()
        finally:
            writer.close()
    def serve():
        asyncio.set_event_loop(loop)
        server=loop.run_until_complete(telnetlib3.create_server(host='127.0.0.1',port=0,encoding=False,shell=shell,connect_maxwait=.2))
        state['server']=server; state['port']=server.sockets[0].getsockname()[1]; ready.set()
        loop.run_forever()
        server.close(); loop.run_until_complete(server.wait_closed())
        tasks=asyncio.all_tasks(loop)
        for t in tasks: t.cancel()
        loop.run_until_complete(asyncio.gather(*tasks,return_exceptions=True)); loop.close()
    thread=threading.Thread(target=serve,daemon=True); thread.start(); assert ready.wait(5)
    try:
        batch=Batch(Archive(tmp_path),[Target('127.0.0.1','telnet',state['port'])],parse_commands('show version','查询'),
                    Settings(connect_timeout=4,command_timeout=4),Credentials('tester','telnet-private-password'))
        root=batch.run(); d=batch.store.task['devices'][0]
        assert d['status']=='完成', d['error']
        assert len(received)==1
        for p in root.rglob('*'):
            if p.is_file(): assert b'telnet-private-password' not in p.read_bytes()
        assert 'Telnet中文数据' in (root/d['log_path']/'session.rx.bin').read_bytes().decode()
    finally:
        loop.call_soon_threadsafe(loop.stop);thread.join(5)


def test_ssh_changed_key_rejected(tmp_path,ssh_server):
    port,_,sent=ssh_server
    wrong=paramiko.RSAKey.generate(2048)
    hosts=tmp_path/'known_hosts';hosts.write_text(f'[127.0.0.1]:{port} {wrong.get_name()} {wrong.get_base64()}\n')
    batch=Batch(Archive(tmp_path),[Target('127.0.0.1','ssh',port)],parse_commands('never send'),
                Settings(known_hosts=str(hosts),connect_timeout=2),Credentials('tester','private-test-password'))
    batch.run();d=batch.store.task['devices'][0]
    assert 'BadHostKeyException' in d['error']
    assert d['commands'][0]['status']=='未执行'
    assert not sent
