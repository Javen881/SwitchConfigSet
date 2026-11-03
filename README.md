# SwitchConfigSet

本仓库包含两个本地桌面工具，面向工业交换机的 CLI 批量操作。

## 工具说明

| 工具 | 作用 | 典型场景 |
|---|---|---|
| `SwitchBatchCLI` | 向已有唯一管理 IP 的多台交换机批量发送 SSH/Telnet 命令，并完整保存回显 | 批量查询版本、生成树、VLAN，或下发已确认的配置命令 |
| `SwitchConfigBatch` | 使用占位符批量生成管理 IP 配置，并按原 IP、新 IP 两阶段执行和核验 | 多台设备批量改管理 IP，然后导入后续配置 |

两个工具默认都可使用离线模拟模式，不会连接真实设备。实际设备的 CLI 语法、提示符、分页、保存和确认行为需要先在单台测试设备上验证。

## 快速开始

推荐 Python 3.12：

```bash
cd SwitchBatchCLI
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./run.sh
```

另一个工具：

```bash
cd SwitchConfigBatch
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./run.sh
```

Windows 可使用对应目录中的构建脚本生成便携包。仓库不提交虚拟环境、编译缓存、设备日志和 Windows 运行时成品，避免 Git 历史膨胀或泄露设备信息。

## 文档

- [`SwitchBatchCLI/USAGE.md`](SwitchBatchCLI/USAGE.md)：通用批量命令工具使用说明。
- [`SwitchConfigBatch/README.md`](SwitchConfigBatch/README.md)：批量改 IP 与配置导入说明。
- [`SwitchBatchCLI/examples/cisco-rstp/`](SwitchBatchCLI/examples/cisco-rstp/)：Cisco IOS 风格的 RSTP 示例命令，仅作为模板，不能视为所有交换机兼容。

## 测试

```bash
cd SwitchBatchCLI
.venv/bin/python -m pytest -q

cd ../SwitchConfigBatch
.venv/bin/python -m pytest -q
```

部分 SSH/Telnet 测试会在本机 `127.0.0.1` 创建临时服务端，不会访问外部交换机。

## 重要边界

- 同一网络中多台设备使用相同出厂 IP 时，普通 ARP/IP 管理无法可靠区分设备。请逐台接入、使用端口隔离，或使用经过验证的厂家二层协议。
- 命令收到提示符只表示 CLI 交互结束，不代表配置业务已经通过验证。
- Telnet 为明文协议。生产网络优先使用 SSH；使用 Telnet 时应在隔离的管理网络中操作。
- 登录密码仅在运行内存中使用；配置命令和设备回显仍可能含敏感信息，请妥善保护任务目录和导出文件。
