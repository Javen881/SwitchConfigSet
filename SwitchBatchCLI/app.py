#!/usr/bin/env python3
import argparse
import os
from pathlib import Path


def main():
    if os.name != 'nt':
        os.umask(0o077)
    parser = argparse.ArgumentParser(description='SwitchBatchCLI local desktop utility')
    parser.add_argument('--data-dir', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--demo', action='store_true', help='运行无网络模拟任务并验证ZIP导出')
    args = parser.parse_args()
    from switchbatch.instance import InstanceLock
    instance_lock = InstanceLock(args.data_dir)
    if args.demo:
        from switchbatch.models import Target, Settings, parse_commands
        from switchbatch.transports import Credentials
        from switchbatch.store import Archive
        from switchbatch.engine import Batch
        archive = Archive(args.data_dir); archive.recover()
        batch = Batch(archive, [Target('192.0.2.10'), Target('192.0.2.11')],
                      parse_commands('show version\ndemo pages\nshow spanning-tree', '查询'),
                      Settings(), Credentials(), '离线模拟任务')
        print('任务目录:', batch.run())
        print('导出校验通过:', archive.export(batch.store.root))
        return 0
    from switchbatch.gui import main as gui_main
    return gui_main(args.data_dir)


if __name__ == '__main__':
    raise SystemExit(main())
