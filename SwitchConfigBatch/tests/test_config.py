import json
import threading
import time
from dataclasses import replace
import pytest
from configbatch.core import Profile,make_plan,render,export_plan,validate_execution,Provisioner,demo_profile,DemoFactory,capture
from switchbatch.models import Settings
from switchbatch.transports import Credentials,Mock


def test_generation_preserves_unmarked_addresses(tmp_path):
    p=Profile(main='description original 192.168.1.1\naddress ${IP} mask ${MASK}\nacl host 192.168.1.1',count=3)
    ds=make_plan(p)
    text=render(p.main,ds[1])
    assert 'address 192.168.1.102 mask 255.255.255.0' in text
    assert text.count('192.168.1.1')==3  # includes target .102 plus two unchanged exact old literals
    assert 'acl host 192.168.1.1' in text
    folder=export_plan(tmp_path,p,ds)
    assert (folder/'0002_192.168.1.102/01-main.txt').read_text()==text
    plan=json.loads((folder/'plan.json').read_text())
    assert plan['status']=='仅生成，未下发'
    assert export_plan(tmp_path,p,ds)!=folder


@pytest.mark.parametrize('changes',[
    {'first':'192.168.1.254','count':3}, {'mask':'255.0.255.0'}, {'mask':'0.0.0.255'},
    {'first':'224.0.0.1'}, {'gateway':'192.168.2.1'}, {'gateway':'192.168.1.101'},
    {'first':'192.168.1.1'}, {'count':0}, {'count':1025}, {'main':'ip ${TYPO}'},
    {'main':'ip ${IP} via ${GATEWAY}'}, {'main':'ip ${IP'}, {'main':''},
    {'mode':'已有独立管理IP','count':2,'current':'192.168.1.1,192.168.1.1'},
    {'mode':'已有独立管理IP','count':2,'current':'192.168.1.102,192.168.1.2'},
])
def test_reject_invalid_plans(changes):
    with pytest.raises(ValueError):make_plan(replace(Profile(main='ip ${IP}'),**changes))


def test_unique_old_addresses_and_prefix():
    p=Profile(mode='已有独立管理IP',current='10.0.0.1,10.0.0.2',count=2,main='ip ${IP}/${PREFIX}\nname sw${INDEX}\nold ${OLD_IP}')
    ds=make_plan(p)
    assert ds[1].old_ip=='10.0.0.2'
    assert '192.168.1.102/24' in render(p.main,ds[1])
    assert 'sw2' in render(p.main,ds[1])


def test_capture_rejects_ambiguous_identity():
    assert capture('Serial: A\nSerial: A',r'Serial: (\S+)','身份')=='A'
    with pytest.raises(ValueError):capture('Serial: A\nSerial: B',r'Serial: (\S+)','身份')
    with pytest.raises(ValueError):capture('nothing',r'Serial: (\S+)','身份')


def test_demo_cannot_send_real_and_disconnect_must_be_last():
    p=demo_profile();ds=make_plan(p)
    with pytest.raises(ValueError,match='模拟'):validate_execution(p,ds,'telnet')
    p=replace(p,main=p.main+'\ndemo save')
    with pytest.raises(ValueError,match='最后'):validate_execution(p,ds,'mock')


def test_complete_ip_change_and_query(tmp_path):
    p=demo_profile();ds=make_plan(p);folder=export_plan(tmp_path,p,ds)
    runner=Provisioner(tmp_path,p,ds,Settings(),Credentials('test','secret-value'),protocol='mock')
    result=runner.run_device(ds[0],folder)
    assert result['status']=='模拟验证完成',result
    assert result['identity']=='DEMO-0001'
    assert len(result['phases'])==2 and not result['persistent_save_verified']
    for path in folder.rglob('*'):
        if path.is_file():assert b'secret-value' not in path.read_bytes()
    again=runner.run_device(ds[0],folder)
    assert again['attempt_id']!=result['attempt_id']
    assert len(list(folder.glob('*_result.json')))==2


def test_identity_mismatch_stops_before_post_configuration(tmp_path):
    p=demo_profile();ds=make_plan(p);folder=export_plan(tmp_path,p,ds);writes=[]
    class Different(Mock):
        def __init__(self,t,c,s):super().__init__(t,c,s);self.ip=t.ip
        def write(self,data):
            writes.append((self.ip,data))
            if data.strip()==b'demo identity':
                self.pending.append(b'Serial: '+(b'OLD' if self.ip==ds[0].old_ip else b'OTHER')+b'\r\nSwitch#')
            else:super().write(data)
    runner=Provisioner(tmp_path,p,ds,Settings(),Credentials(),protocol='mock',transport_factory=Different)
    result=runner.run_device(ds[0],folder)
    assert result['status']=='失败/待核对'
    assert '身份' in result['error']
    assert not any(data.strip()==b'demo save' for ip,data in writes)


def test_missing_identity_stops_before_any_change(tmp_path):
    p=demo_profile();ds=make_plan(p);folder=export_plan(tmp_path,p,ds);writes=[]
    class NoIdentity(Mock):
        def write(self,data):writes.append(data);super().write(data)
    result=Provisioner(tmp_path,p,ds,Settings(),Credentials(),protocol='mock',transport_factory=NoIdentity).run_device(ds[0],folder)
    assert result['status']=='失败/待核对'
    assert writes==[b'demo identity\r\n']


def test_failure_before_last_never_reconnects(tmp_path):
    p=replace(demo_profile(),main='demo error\ndemo set-ip ${IP}')
    ds=make_plan(p);folder=export_plan(tmp_path,p,ds)
    result=Provisioner(tmp_path,p,ds,Settings(),Credentials(),protocol='mock').run_device(ds[0],folder)
    assert result['status']=='失败/待核对'
    assert len(result['phases'])==1


def test_ip_mismatch_is_not_success(tmp_path):
    p=demo_profile();ds=make_plan(p);folder=export_plan(tmp_path,p,ds)
    class WrongIP(Mock):
        def write(self,data):
            if data.strip()==b'demo identity':self.pending.append(b'Serial: SAME\r\nSwitch#')
            elif data.strip()==b'demo show-ip':self.pending.append(b'Management IP: 192.168.1.200\r\nSwitch#')
            else:super().write(data)
    result=Provisioner(tmp_path,p,ds,Settings(),Credentials(),protocol='mock',transport_factory=WrongIP).run_device(ds[0],folder)
    assert result['status']=='失败/待核对' and '192.168.1.200' in result['error']


def test_cancel_before_reconnect(tmp_path):
    p=replace(demo_profile(),settle_seconds=10);ds=make_plan(p);folder=export_plan(tmp_path,p,ds)
    runner=Provisioner(tmp_path,p,ds,Settings(),Credentials(),protocol='mock');out=[]
    t=threading.Thread(target=lambda:out.append(runner.run_device(ds[0],folder)));t.start()
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        results=list(folder.glob('*_result.json'))
        if results and json.loads(results[0].read_text())['status']=='等待新IP核对':break
        time.sleep(.02)
    runner.cancel();t.join(3)
    assert not t.is_alive()
    assert out[0]['status']=='取消/待核对' and len(out[0]['phases'])==1
