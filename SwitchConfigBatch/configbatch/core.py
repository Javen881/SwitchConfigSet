from dataclasses import dataclass, asdict
from datetime import datetime
import ipaddress
import json
from pathlib import Path
import re
from string import Template
import threading
from uuid import uuid4

from switchbatch.engine import Batch, Halt, ANSI
from switchbatch.models import Target, Settings, parse_commands
from switchbatch.store import Archive, atomic_json, now
from switchbatch.transports import Credentials, factory, Mock

TOKENS = {'IP','OLD_IP','MASK','PREFIX','GATEWAY','INDEX'}


@dataclass
class Profile:
    mode: str = '逐台接入（相同出厂IP）'
    current: str = '192.168.1.1'
    first: str = '192.168.1.101'
    count: int = 1
    mask: str = '255.255.255.0'
    gateway: str = ''
    main: str = ''
    after: str = ''
    identity_command: str = ''
    identity_pattern: str = ''
    verify_command: str = ''
    verify_pattern: str = ''
    allow_disconnect: bool = False
    settle_seconds: int = 3


@dataclass
class Device:
    index: int
    old_ip: str
    ip: str
    mask: str
    prefix: int
    gateway: str

    def variables(self):
        return dict(IP=self.ip, OLD_IP=self.old_ip, MASK=self.mask, PREFIX=str(self.prefix),
                    GATEWAY=self.gateway, INDEX=str(self.index))


def render(text, device):
    template=Template(text)
    if not template.is_valid():
        raise ValueError('占位符格式错误；使用 ${IP} 等，字面美元符号写为 $$')
    unknown=set(template.get_identifiers())-TOKENS
    if unknown:
        raise ValueError('未知占位符：'+', '.join(sorted(unknown)))
    if 'GATEWAY' in template.get_identifiers() and not device.gateway:
        raise ValueError('模板使用了 ${GATEWAY}，请填写网关')
    result=template.substitute(device.variables())
    if result.strip():
        parse_commands(result)
    return result


def usable(ip, network):
    return ip in network and (network.prefixlen>=31 or ip not in (network.network_address,network.broadcast_address)) and not (ip.is_multicast or ip.is_unspecified or ip.is_loopback or ip.is_reserved or ip.is_link_local)


def make_plan(profile):
    if not 1 <= profile.count <= 1024:
        raise ValueError('设备数量必须为1～1024')
    if profile.mode not in ('逐台接入（相同出厂IP）','已有独立管理IP'):
        raise ValueError('无效接入方式')
    if not 0 <= profile.settle_seconds <= 120:
        raise ValueError('改址后等待时间需为0～120秒')
    first=ipaddress.IPv4Address(profile.first.strip())
    # Require a real dotted netmask, not IPv4Network's alternative hostmask form.
    mask=ipaddress.IPv4Address(profile.mask.strip())
    binary=f'{int(mask):032b}'
    if '01' in binary:
        raise ValueError('掩码必须是连续1后连续0的子网掩码')
    network=ipaddress.IPv4Network(f'{first}/{mask}',strict=False)
    gw=ipaddress.IPv4Address(profile.gateway.strip()) if profile.gateway.strip() else None
    if gw and not usable(gw,network):
        raise ValueError('网关需为目标子网内的可用单播地址')
    if profile.mode=='逐台接入（相同出厂IP）':
        old=[str(ipaddress.IPv4Address(profile.current.strip()))]*profile.count
    else:
        old=[str(ipaddress.IPv4Address(x)) for x in re.split(r'[,\s]+',profile.current.strip()) if x]
        if len(old)!=profile.count or len(old)!=len(set(old)):
            raise ValueError('当前管理IP列表需与数量一致且不得重复')
    if any(not usable(ipaddress.IPv4Address(x),ipaddress.IPv4Network(f'{x}/32')) for x in old):
        raise ValueError('当前地址必须为有效单播管理IP')
    if not profile.main.strip() or 'IP' not in Template(profile.main).get_identifiers():
        raise ValueError('第一阶段模板需包含 ${IP}；请填写实际CLI命令或加载模拟示例')
    result=[]
    for i in range(profile.count):
        address=ipaddress.IPv4Address(int(first)+i)
        if not usable(address,network):
            raise ValueError(f'{address} 已越过当前子网或不是可用主机地址，请调整起始IP/数量')
        if address==gw:
            raise ValueError(f'{address} 与网关冲突')
        if str(address) in old and (str(address)!=old[i] or profile.mode=='逐台接入（相同出厂IP）'):
            raise ValueError(f'目标IP {address} 与当前设备地址冲突')
        device=Device(i+1,old[i],str(address),str(mask),network.prefixlen,str(gw) if gw else '')
        for text in (profile.main,profile.after,profile.identity_command,profile.verify_command):
            render(text,device)
        result.append(device)
    return result


def capture(text, pattern, label):
    regex=re.compile(pattern)
    if regex.groups<1:
        raise ValueError(f'{label}规则必须包含一个捕获组，例如 (... )')
    matches={match.group(1).strip() for match in regex.finditer(text) if match.group(1) is not None}
    if len(matches)!=1 or not next(iter(matches), ''):
        raise ValueError(f'{label}未提取到唯一非空值，请核对查询回显和规则')
    return next(iter(matches))


def validate_execution(profile, devices, protocol):
    for label,command,pattern in [('设备身份',profile.identity_command,profile.identity_pattern),('管理IP',profile.verify_command,profile.verify_pattern)]:
        if len(parse_commands(command))!=1:
            raise ValueError(f'{label}查询必须是一条只读命令')
        compiled=re.compile(pattern)
        if not pattern or compiled.groups<1 or compiled.search(''):
            raise ValueError(f'{label}提取正则需包含非空捕获组')
    if profile.allow_disconnect:
        final=parse_commands(profile.main)[-1].text
        if 'IP' not in Template(final).get_identifiers():
            raise ValueError('允许改址断连时，第一阶段最后一条命令必须含 ${IP}；请自行按厂家语法调整阶段')
    if protocol!='mock':
        for device in devices:
            for template in (profile.main,profile.after,profile.identity_command,profile.verify_command):
                if not template.strip():
                    continue
                if any(c.text.strip().startswith('demo ') for c in parse_commands(render(template,device)) if template.strip()):
                    raise ValueError('模拟示例不能发送到真实设备，请替换成实际CLI命令')


def export_plan(base, profile, devices):
    folder=Path(base)/'generated'/(datetime.now().strftime('%Y%m%d_%H%M%S_')+uuid4().hex[:8])
    folder.mkdir(parents=True, mode=0o700)
    atomic_json(folder/'plan.json',{'created_at':now(),'profile':asdict(profile),'devices':[asdict(d) for d in devices],
                                  'status':'仅生成，未下发','network_collision_checked':False})
    for name,value in [('template-main.txt',profile.main),('template-after.txt',profile.after)]:
        (folder/name).write_text(value,encoding='utf-8')
    for d in devices:
        target=folder/f'{d.index:04d}_{d.ip}'
        target.mkdir(mode=0o700)
        for name,value in [('01-main.txt',profile.main),('02-after-reconnect.txt',profile.after),
                           ('identity-query.txt',profile.identity_command),('ip-query.txt',profile.verify_command)]:
            (target/name).write_text(render(value,d),encoding='utf-8')
    return folder


class GuardedBatch(Batch):
    def __init__(self,*args, identity_pattern='', expected_identity=None, verify_pattern='', expected_ip='', **kwargs):
        super().__init__(*args,**kwargs)
        self.identity_pattern=identity_pattern
        self.expected_identity=expected_identity
        self.identity=None
        self.verify_pattern=verify_pattern
        self.expected_ip=expected_ip

    def receive(self,session,d,c,timeout,decoder):
        error=super().receive(session,d,c,timeout,decoder)
        if c is None or error:
            return error
        is_identity=c['sequence']==1
        is_verify=self.verify_pattern and c['sequence']==len(d['commands'])
        if not (is_identity or is_verify):
            return error
        path=self.store.root/d['log_path']/'session.rx.bin'
        with path.open('rb') as f:
            f.seek(c['rx_start']);raw=f.read()
        clean=ANSI.sub('',raw.decode(self.settings.encoding,errors='replace'))
        clean='\n'.join(line for line in clean.splitlines() if line.strip()!=c['text'].strip())
        try:
            if is_identity:
                self.identity=capture(clean,self.identity_pattern,'设备身份')
                if self.expected_identity is not None and self.identity!=self.expected_identity:
                    raise ValueError('新IP对应设备身份与改址前不一致，禁止继续发送配置')
            if is_verify:
                address=str(ipaddress.IPv4Address(capture(clean,self.verify_pattern,'管理IP')))
                if address!=self.expected_ip:
                    raise ValueError(f'回读管理IP为{address}，与目标{self.expected_ip}不一致')
        except ValueError as exc:
            raise Halt('未知',str(exc)) from exc
        return error


class DemoFactory:
    def __init__(self,device):
        self.device=device
    def __call__(self,target,credentials,settings):
        device=self.device
        class Demo(Mock):
            def write(self,data):
                command=data.decode(self.encoding).strip()
                if command=='demo identity':
                    self.pending.extend([data,f'Serial: DEMO-{device.index:04d}\r\nSwitch#'.encode()])
                elif command=='demo show-ip':
                    self.pending.extend([data,f'Management IP: {device.ip}\r\nSwitch#'.encode()])
                elif command.startswith('demo set-ip '):
                    self.pending.extend([data,b'IP change requested\r\n'])
                    self.disconnected=True
                else:
                    super().write(data)
        return Demo(target,credentials,settings)


class Provisioner:
    def __init__(self,base,profile,devices,settings,credentials,protocol='telnet',port=23,callback=None,transport_factory=None):
        validate_execution(profile,devices,protocol)
        settings.validate()
        self.profile=profile;self.settings=settings;self.credentials=credentials
        self.protocol=protocol;self.port=port
        if protocol not in ('telnet','ssh','mock') or not 1<=port<=65535:
            raise ValueError('协议/端口无效')
        self.archive=Archive(base)
        self.archive.recover()
        self.callback=callback or (lambda event:None)
        self.stop=threading.Event();self.active=None;self.factory=transport_factory

    def cancel(self):
        self.stop.set()
        if self.active:self.active.cancel()

    def phase(self,device,address,commands,expected=None,verify=False):
        if self.stop.is_set():raise RuntimeError('已取消，未继续发送')
        transport=self.factory or (DemoFactory(device) if self.protocol=='mock' else factory)
        self.active=GuardedBatch(self.archive,[Target(address,self.protocol,self.port)],commands,self.settings,self.credentials,
            input_snapshot={'device':asdict(device),'phase':'改址后核对及配置' if verify else '改址前身份核对及配置'},
            callback=self.callback,transport_factory=transport,identity_pattern=self.profile.identity_pattern,
            expected_identity=expected,verify_pattern=self.profile.verify_pattern if verify else '',expected_ip=device.ip)
        if self.stop.is_set():self.active.cancel()
        self.active.run()
        return self.active

    def run_device(self,device,folder):
        record={'attempt_id':uuid4().hex,'device':asdict(device),'started_at':now(),'ended_at':None,'status':'运行','phases':[],
                'identity':None,'error':'','persistent_save_verified':False}
        path=Path(folder)/f"{device.index:04d}_{record['attempt_id'][:10]}_result.json"
        atomic_json(path,record)
        try:
            first=parse_commands(render(self.profile.identity_command,device),'查询')+parse_commands(render(self.profile.main,device),'配置')
            batch=self.phase(device,device.old_ip,first)
            record['phases'].append(str(batch.store.root));record['identity']=batch.identity
            d=batch.store.task['devices'][0]
            tail=d['commands'][-1]
            allowable=(self.profile.allow_disconnect and tail['status'] in ('断连','超时') and tail['sent_at'] is not None
                       and all(c['status']=='完成' for c in d['commands'][:-1]) and batch.identity is not None)
            if d['status']!='完成' and not allowable:
                raise RuntimeError('第一阶段未完成：'+d.get('error',''))
            record['status']='等待新IP核对';atomic_json(path,record)
            if self.stop.wait(self.profile.settle_seconds):raise RuntimeError('已取消，改址结果待核对')
            after=parse_commands(render(self.profile.identity_command,device),'查询')
            if self.profile.after.strip():after+=parse_commands(render(self.profile.after,device),'配置')
            after+=parse_commands(render(self.profile.verify_command,device),'查询')
            second=self.phase(device,device.ip,after,expected=batch.identity,verify=True)
            record['phases'].append(str(second.store.root))
            d2=second.store.task['devices'][0]
            if d2['status']!='完成':raise RuntimeError('新IP核对/第二阶段未完成：'+d2.get('error',''))
            record['status']='模拟验证完成' if self.protocol=='mock' else '身份及管理IP核对完成'
        except Exception as exc:
            record['status']='取消/待核对' if self.stop.is_set() else '失败/待核对'
            record['error']=str(exc)
            if self.active and str(self.active.store.root) not in record['phases']:
                record['phases'].append(str(self.active.store.root))
        finally:
            record['ended_at']=now();atomic_json(path,record)
        return record


def demo_profile():
    return Profile(count=3,main='demo configure\ndemo set-ip ${IP} ${MASK}',after='demo save',
                   identity_command='demo identity',identity_pattern=r'(?m)^Serial: (\S+)\s*$',
                   verify_command='demo show-ip',verify_pattern=r'(?m)^Management IP: ([0-9.]+)\s*$',
                   allow_disconnect=True,settle_seconds=0)
