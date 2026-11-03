from dataclasses import dataclass, asdict
import codecs
import ipaddress
import re


@dataclass
class Target:
    ip: str
    protocol: str = 'mock'
    port: int = 22
    device_type: str = 'generic_cli'


@dataclass
class Command:
    text: str
    line: int
    kind: str = '其他'


@dataclass
class Settings:
    concurrency: int = 5
    connect_timeout: float = 10
    command_timeout: float = 30
    encoding: str = 'utf-8'
    prompt: str = r'(?m)^[^\r\n]{1,100}[>#]\s*$'
    pager: str = r'(?i)(?:--\s*more\s*--|press any key to continue)\s*$'
    interaction: str = r'(?im)(?:\[y/n\]|\[yes/no\]|\[confirm\]|password\s*:|continue\?)[^\r\n]*$'
    error: str = r'(?im)^\s*(?:%\s*(?:error|invalid|incomplete|ambiguous|unknown)|error\s*:|invalid (?:input|command)|unknown command|unrecognized command|permission denied)'
    known_hosts: str = ''
    related_case_id: str = ''
    notes: str = ''

    def validate(self):
        if not 1 <= self.concurrency <= 32:
            raise ValueError('执行并发必须为1～32')
        if not 0.1 <= self.connect_timeout <= 600 or not 0.1 <= self.command_timeout <= 3600:
            raise ValueError('超时不在允许范围')
        codecs.lookup(self.encoding)
        for name in ('prompt', 'pager', 'interaction', 'error'):
            pattern = getattr(self, name)
            if not pattern or re.compile(pattern).search(''):
                raise ValueError(f'{name} 正则不能为空或匹配空字符串')
        return self


def parse_targets(text, limit=1024):
    """Pre-count every network before enumerating; reject host-bit CIDRs."""
    result = {}
    for token in re.split(r'[,\s]+', text.strip()):
        if not token:
            continue
        try:
            if '/' in token:
                try:
                    net = ipaddress.IPv4Network(token, strict=True)
                except ValueError:
                    net = ipaddress.IPv4Network(token, strict=False)
                    raise ValueError(f'{token} 含主机位，请确认后改为 {net}')
                count = net.num_addresses if net.prefixlen >= 31 else net.num_addresses - 2
                if count > limit:
                    raise ValueError(f'{token} 展开{count}个地址，超过上限{limit}')
                addresses = net.hosts()
            else:
                addresses = [ipaddress.IPv4Address(token)]
            for address in addresses:
                result.setdefault(str(address), []).append(token)
                if len(result) > limit:
                    raise ValueError(f'目标总数超过上限{limit}')
        except ipaddress.AddressValueError as exc:
            raise ValueError(f'无效IPv4地址：{token}') from exc
    if not result:
        raise ValueError('请填写目标IP或CIDR')
    return result


def parse_commands(text, kind='其他'):
    result = []
    if kind not in ('配置', '查询', '其他'):
        raise ValueError('无效命令类型')
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if any(ord(c) < 32 for c in line):
            raise ValueError(f'第{number}行包含控制字符')
        result.append(Command(line, number, kind))
    if not result:
        raise ValueError('命令清单为空')
    return result


def validate_targets(targets):
    if not targets or len(targets) > 1024:
        raise ValueError('目标数必须为1～1024')
    seen = set()
    for t in targets:
        ipaddress.IPv4Address(t.ip)
        if t.ip in seen:
            raise ValueError(f'重复目标：{t.ip}；同设备仅允许一个会话')
        seen.add(t.ip)
        if t.protocol not in ('ssh', 'telnet', 'mock') or not 1 <= t.port <= 65535:
            raise ValueError('协议或端口无效')
