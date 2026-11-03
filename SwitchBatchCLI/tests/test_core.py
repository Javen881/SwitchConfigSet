import json
import threading
import time
import zipfile
from pathlib import Path
import pytest
from switchbatch.models import parse_targets, parse_commands, Target, Settings
from switchbatch.store import Archive, TaskStore, atomic_json, csvsafe
from switchbatch.transports import Credentials, Mock
from switchbatch.engine import Batch


def make(tmp_path, text='show version', count=1, **kwargs):
    return Batch(Archive(tmp_path), [Target(f'192.0.2.{i+1}') for i in range(count)],
                 parse_commands(text, '查询'), Settings(command_timeout=1, connect_timeout=1),
                 Credentials('tester','secret-not-in-logs'), **kwargs)


def test_address_semantics():
    result = parse_targets('192.0.2.0/31,192.0.2.1,192.0.2.2/32')
    assert list(result) == ['192.0.2.0','192.0.2.1','192.0.2.2']
    assert len(result['192.0.2.1']) == 2
    assert list(parse_targets('192.0.2.0/30')) == ['192.0.2.1','192.0.2.2']


@pytest.mark.parametrize('text',['192.0.2.3/24','10.0.0.0/8','300.1.1.1','::1','', '192.0.2.0/33'])
def test_invalid_address(text):
    with pytest.raises(ValueError):
        parse_targets(text)


def test_commands_preserve_duplicate_whitespace():
    cs = parse_commands(' show x  \n\nshow x\nshow x')
    assert [c.line for c in cs] == [1,3,4]
    assert cs[0].text == ' show x  '
    with pytest.raises(ValueError):
        parse_commands('show\x00x')


def test_happy_export_and_raw(tmp_path):
    batch = make(tmp_path, 'show version\ndemo pages\nshow version', count=3)
    root = batch.run()
    task = json.loads((root/'task.json').read_text())
    assert task['status'] == '完成'
    ids = set()
    for d in task['devices']:
        assert d['status'] == '完成'
        raw = (root/d['log_path']/'session.rx.bin').read_bytes()
        assert b'--More--' in raw and b'page 2' in raw
        for c in d['commands']:
            assert c['output_complete']
            ids.add(c['command_run_id'])
            assert (root/c['output_path']).read_bytes() == raw[c['rx_start']:c['rx_end']]
            assert (root/c['collected_path']).exists()
    assert len(ids) == 9
    events = [json.loads(line) for line in (root/'events.jsonl').read_text().splitlines()]
    assert [x['sequence'] for x in events] == list(range(1,len(events)+1))
    assert any(e['auxiliary'] and e['direction']=='TX' for e in events)
    zpath = batch.store.archive.export(root)
    with zipfile.ZipFile(zpath) as z:
        assert 'manifest.json' in z.namelist()
        assert not any(name.startswith('/') or '..' in Path(name).parts for name in z.namelist())
        for name in z.namelist():
            assert b'secret-not-in-logs' not in z.read(name)


@pytest.mark.parametrize('cmd,status,complete',[('demo timeout','超时',False),('demo disconnect','断连',False),('demo confirm','未知',False),('demo error','设备报错',True)])
def test_failure_preserves_partial_and_stops(tmp_path,cmd,status,complete):
    batch = make(tmp_path,f'show version\n{cmd}\nnever send')
    root = batch.run()
    d = batch.store.task['devices'][0]
    assert [c['status'] for c in d['commands']] == ['完成',status,'未执行']
    assert d['commands'][1]['output_complete'] == complete
    assert (root/d['commands'][1]['output_path']).stat().st_size > 0
    assert b'never send' not in (root/d['log_path']/'session.rx.bin').read_bytes()


def test_cancel_running(tmp_path):
    batch = make(tmp_path, 'demo timeout\nnever send',count=4)
    batch.settings.concurrency = 1
    thread = threading.Thread(target=batch.run); thread.start()
    deadline=time.monotonic()+3
    while not batch.store.task['devices'][0]['commands'][0]['sent_at'] and time.monotonic()<deadline:
        time.sleep(.01)
    batch.cancel(); thread.join(4)
    assert not thread.is_alive()
    assert batch.store.task['devices'][0]['commands'][0]['status']=='取消'
    assert all(c['status']=='未执行' for d in batch.store.task['devices'][1:] for c in d['commands'])


def test_connection_failure_no_password(tmp_path):
    def fail(*args):
        raise PermissionError('bad secret-not-in-logs')
    batch=make(tmp_path,transport_factory=fail)
    root=batch.run()
    d=batch.store.task['devices'][0]
    assert d['status']=='失败'
    assert d['commands'][0]['status']=='未执行'
    assert 'secret-not-in-logs' not in (root/'task.json').read_text()


def test_long_raw_output(tmp_path):
    batch=make(tmp_path,'demo long')
    batch.settings.command_timeout=30
    root=batch.run()
    d=batch.store.task['devices'][0]
    raw=(root/d['log_path']/'session.rx.bin').read_bytes()
    assert raw.count('接口/VLAN/生成树回显'.encode())==4000
    assert d['commands'][0]['output_complete']


def test_disk_failure_stops_sending(tmp_path,monkeypatch):
    batch=make(tmp_path,'show first\nshow second')
    import switchbatch.store as store
    original=store.append
    writes=[]
    class Tracking(Mock):
        def write(self,data):
            writes.append(data); super().write(data)
    batch.transport_factory=Tracking
    def broken(path,data):
        if Path(path).name=='session.rx.bin':
            raise OSError('disk full')
        return original(path,data)
    monkeypatch.setattr(store,'append',broken)
    with pytest.raises(OSError):
        batch.run()
    assert writes==[]


def test_restart_marks_incomplete(tmp_path):
    batch=make(tmp_path,'show version\nshow next')
    task=batch.store.task
    task['status']='运行'; task['devices'][0]['status']='运行'
    task['devices'][0]['commands'][0]['status']='运行'
    batch.store.save()
    archive=Archive(tmp_path); archive.recover()
    recovered=json.loads((batch.store.root/'task.json').read_text())
    assert recovered['status']=='中断/待核对'
    assert recovered['devices'][0]['commands'][0]['status']=='未知'
    assert recovered['devices'][0]['commands'][1]['status']=='未执行'
    assert (batch.store.root/'summary.csv').exists()


def test_preflight_and_active_export(tmp_path):
    with pytest.raises(ValueError):
        Batch(Archive(tmp_path),[Target('192.0.2.1')]*2,parse_commands('x'),Settings(),Credentials())
    batch=make(tmp_path)
    with pytest.raises(ValueError):
        batch.store.archive.export(batch.store.root)
    assert csvsafe(' =HYPERLINK("bad")').startswith("'")
    assert csvsafe('normal')=='normal'


def test_multibyte_split(tmp_path):
    class Split(Mock):
        def write(self,data):
            value='中文回显\r\nSwitch#'.encode()
            self.pending.extend([value[:1],value[1:2],value[2:7],value[7:]])
    batch=make(tmp_path,transport_factory=Split)
    batch.run()
    c=batch.store.task['devices'][0]['commands'][0]
    assert '中文回显' in (batch.store.root/c['output_path']).read_text()
    assert '\ufffd' not in (batch.store.root/c['output_path']).read_text()


def test_prompt_like_output_does_not_block_final_prompt(tmp_path):
    class Prompts(Mock):
        def write(self,data):
            self.pending.append(b'not-a-real-prompt#\r\nbody output\r\nSwitch#')
    batch=make(tmp_path,transport_factory=Prompts)
    batch.run()
    assert batch.store.task['devices'][0]['commands'][0]['status']=='完成'


def test_recovery_materializes_partial_raw(tmp_path):
    batch=make(tmp_path,'show long\nnever send')
    task=batch.store.task; task['status']='运行'
    d=task['devices'][0];d['status']='运行'
    c=d['commands'][0];c.update(status='运行',rx_start=0)
    (batch.store.root/d['log_path']/'session.rx.bin').write_bytes('已收部分中文'.encode())
    batch.store.save()
    Archive(tmp_path).recover()
    recovered=json.loads((batch.store.root/'task.json').read_text())
    c=recovered['devices'][0]['commands'][0]
    assert c['status']=='未知' and c['output_complete'] is False
    assert (batch.store.root/c['output_path']).read_text()=='已收部分中文'
    results=[json.loads(x) for x in (batch.store.root/'command_results.jsonl').read_text().splitlines()]
    assert [x['status'] for x in results]==['未知','未执行']


def test_single_instance(tmp_path):
    from switchbatch.instance import InstanceLock
    first=InstanceLock(tmp_path)
    try:
        with pytest.raises(RuntimeError): InstanceLock(tmp_path)
    finally: first.close()
    second=InstanceLock(tmp_path);second.close()
