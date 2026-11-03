# SwitchBatchCLI 0.1 使用说明

本地桌面工具，用于向已有唯一管理IP的交换机批量发送CLI命令和采集回显。默认离线模拟，不扫描或连接任何真实设备。第一版不含厂家二层改IP、自动配置模板、自动保存/回滚、协议功能自动判定。

## 启动

本机已安装项目独立虚拟环境，直接执行：

```bash
cd /home/javen03/data/Test/SwitchBatchCLI
./run.sh
```

通用安装方式（已验证Python 3.12/Linux，其他平台需要相应验证）：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

Windows源码运行方式：安装Python 3.12后，使用 `py -3.12 -m venv .venv`、`.venv\Scripts\python -m pip install -r requirements.txt`、`.venv\Scripts\python app.py`。现已另外提供Windows x64 EXE便携包，需完整解压后运行；尚未进行Windows实机启动验证。

离线端到端演示，无需实际交换机：

```bash
./run.sh --demo --data-dir /tmp/switchbatch-demo
```

桌面程序默认将任务写入项目下的 `data/`、`logs/`、`exports/`。可通过 `--data-dir /其他可写目录` 更换。同一数据目录只允许一个应用实例，防止运行中任务被另一个进程误恢复。

## 操作步骤

1. 输入IPv4单地址、列表或CIDR，点击“展开目标”。支持逗号/换行分隔。网络有主机位时拒绝并提示规范网段；确认修改后才能展开。上限1024，/31与/32按端点处理。
2. 勾选目标。可在每行设置协议、端口，或使用“应用协议/端口”批量设置。模拟协议为 `mock`；`ssh` 默认22，`telnet` 默认23。相同IP只能出现一次。
3. 可点击“探测所选端口”。仅测试TCP连接，不认证、不下发命令；端口开放不是登录成功。模拟模式不创建网络连接。
4. 输入或导入UTF-8命令TXT，一行一条。空白行跳过，非空行保留首尾空格和源行号，`#`不是注释。点击“生成发送清单”，逐条调整配置/查询/其他标记，可混排且不改变CLI上下文。
5. 填写用户名、密码或SSH私钥路径，设置编码、超时、并发和提示符。第一版同一批次共用账户、编码及CLI规则；不同账户或厂商规则分批运行。设备协议和端口可逐台设置。密码不保存；手动输入的配置命令及设备回显可能本身包含敏感数据。
6. 点击“预览并创建执行任务”，核对最终目标、协议/端口、命令原文，再确认执行。真实任务不允许夹带 `demo ...` 模拟命令。
7. “执行过程”显示逐设备状态、当前命令和实时收发。界面最近2000行，每条界面通知最多8192字符，显示拥塞时会提示省略数量；所有正常接收并成功落盘的原始字节独立保存，不依赖界面窗口。
8. 任务结束后到“历史与导出”查看记录、打开文件夹或导出ZIP。导出在后台完成，包含SHA-256和大小清单，并自动验证。运行中任务暂不允许导出。

## 首次接入真实设备

先用一台测试设备、一条只读查询命令验证。工具不翻译厂商命令，不自动进入enable/config模式、不自动保存或退出。上述命令均需用户明确列入清单。

### SSH主机密钥

默认读取系统/用户known_hosts及界面指定文件，拒绝未知或发生变化的密钥。未知密钥错误显示SHA-256指纹。请通过设备Console或管理员提供的可信记录核对指纹，再使用已核验的known_hosts条目。不提供“忽略密钥变化”的自动选项。

例如可用OpenSSH的 `ssh-keyscan -p 22 设备IP` 获取候选公钥，写入独立文件，用 `ssh-keygen -lf 文件名` 查看指纹；**获取候选公钥本身不代表已核验可信**。确认与可信指纹一致后，在界面填入该文件路径。非22端口的主机字段格式通常为 `[IP]:端口`。

当前SSH通过Paramiko交互通道直接接收字节，未采用Netmiko自动初始化/清理流程；这是对README候选方案的实现调整，目的是在规则处理前保存原始回显并避免隐式辅助命令。SSH密码/密钥认证已用本机服务器验证；具体交换机固件仍需实机验证。

### 提示符与分页

默认提示符识别末尾以 `>` 或 `#` 结束的非空行，可覆盖常见 `Switch#`、`Switch(config)#`。这是通用启发规则，不保证适合所有设备。建议按实机固定主机名缩小匹配范围，例如：

```text
(?m)^SW1(?:\([^\r\n]*\))?[>#][ \t]*$
```

方括号风格CLI可设置相应的精确提示符模式。ANSI转义仅用于判断，原始文件保留控制字符。引擎根据提示符并等待短暂接收静默确认一条命令结束，不能区分所有恰好长得像提示符的业务输出；必须通过首台实机的完整查询验证规则。

默认分页识别 `--More--` 和 `Press any key to continue`，仅自动发送一个空格，辅助发送前后都有事件记录。其他分页方式需按设备调整，或把厂家关闭分页命令作为显式命令加入列表。未知交互确认/Password提示停止该设备，不自动回答yes或发送enable密码。不匹配的交互最终超时；超时命令不会自动重发。

“分页/错误/交互规则与备注”可编辑正则，按样机收集回显后调整。错误规则不是全厂商语义解析；“完成”只代表收到提示符且未命中配置的错误规则，**业务验证始终为未验证**。

### 同出厂IP设备

必须先逐台接入、端口隔离，或使用已经验证的厂家MAC定向配置协议，分配唯一管理IP后才能批量运行。本版不能用ARP表区分多台同IP交换机，也不包含厂家私有协议。

## 状态和数据

- 设备报错：匹配错误规则并收齐提示符时，可标记该命令回显完整，但停止后续命令。
- 超时/断连/取消/未知交互：保留部分回显、记录完整性原因，不宣称设备已停止执行或改动已回滚。
- 连接/认证失败：保留设备失败记录，所有命令为未执行。
- 日志写失败：停止新命令并显示错误，不宣称完整留痕；后续启动将遗留任务标为中断/待核对。
- 重启恢复：不重放命令。对中断命令从原始字节重建可读输出，标记未知，并重建结果清单。恢复前的结果JSONL保留为 `command_results.before-recovery.jsonl`。
- 查询命令另存 `collected/` 副本；它是收到的CLI输出，不保证可作为设备恢复配置文件。
- 实时记录保留命令原文、重复命令独立ID、源行号、类型、事件序号、时间、单调耗时、原始流字节范围；任务快照保存输入来源及参数，不含登录密码/私钥内容。
- Telnet认证阶段的字节不进入普通证据文件，以避免登录密码被设备回显泄露。CLI原始流从认证后提示符开始；这不是全链路抓包。
- 文件先落盘再通知界面；事件中 `TX_INTENT` 是发送意图，`TX` 是已交给通道发送，不等于设备确认执行。操作系统/设备突然断电仍可能丢失尚未接收或未成功落盘的数据。

目录内容：

```text
logs/日期/任务ID/
  task.json                     参数、设备、逐命令最终状态
  commands.txt                  原始命令清单
  summary.csv                   每设备汇总，带CSV公式前缀处理
  command_results.jsonl         每条命令记录
  events.jsonl                  递增序号、收发与原始流偏移
  devices/内部ID/
    session.rx.bin              原始CLI字节流
    transcript.txt              带时间的收发过程
    commands/000001.txt         逐命令可读回显
    collected/000001.txt        查询回显副本
exports/任务ID_随机后缀.zip      完整任务与manifest.json
```

TXT/CSV/JSON可直接从任务文件夹读取。SQLite只是任务列表索引，不打包进ZIP。ZIP包含全部任务证据与配置内容，没有自动脱敏；认证材料不收录。普通应用入口在Linux使用仅当前用户可读写的文件权限。

## 测试与当前边界

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
```

测试使用内存设备与绑定127.0.0.1的SSH/Telnet模拟服务器；部分受限沙箱禁止创建socket，此时回环测试需要在正常本地环境运行。GUI通过离屏Qt运行。未连接该型号实物，不提供“已通过实机验收”的结论。

已实现模块见源代码：`models.py` 输入解析、`transports.py` 协议适配、`engine.py` 顺序执行与并发、`store.py` 流式留痕/恢复/导出、`gui.py` 桌面界面。

技术参考：[Paramiko Channel](https://docs.paramiko.org/en/stable/api/channel.html)、[telnetlib3 Client](https://telnetlib3.readthedocs.io/en/latest/api/client.html)、[Qt线程信号示例](https://doc.qt.io/qtforpython-6/examples/example_widgets_thread_signals.html)。实际安装版本锁定在 `requirements.lock.txt`。

## Windows EXE便携包

Windows 便携包由 `packaging/` 中的脚本生成，输出到未纳入 Git 的 `dist/` 目录。若项目发布页提供 `SwitchBatchCLI-Windows-x64.zip`，完整解压后双击 `SwitchBatchCLI.exe`。无需另装Python，不能只复制EXE，必须保留旁边的runtime/application目录。任务数据默认保存到 `%LOCALAPPDATA%\SwitchBatchCLI`；启动异常见该目录startup.log。目标Windows 10/11 x64，未签名。在Linux交叉生成的包仍需在Windows实机验证启动。
