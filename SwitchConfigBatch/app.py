import argparse
import os
from pathlib import Path


def main():
    if os.name!='nt':os.umask(0o077)
    parser=argparse.ArgumentParser(description='批量生成管理IP配置并通过CLI导入')
    parser.add_argument('--data-dir',type=Path,default=Path(__file__).resolve().parent)
    parser.add_argument('--demo',action='store_true')
    args=parser.parse_args()
    from switchbatch.instance import InstanceLock
    lock=InstanceLock(args.data_dir)
    if args.demo:
        from configbatch.core import demo_profile,make_plan,export_plan,Provisioner
        from switchbatch.models import Settings
        from switchbatch.transports import Credentials
        p=demo_profile();devices=make_plan(p);folder=export_plan(args.data_dir,p,devices)
        runner=Provisioner(args.data_dir,p,devices,Settings(),Credentials(),protocol='mock')
        results=[runner.run_device(d,folder) for d in devices]
        print('配置与结果：',folder)
        for r in results:print(r['device']['ip'],r['status'],r['error'])
        return 0 if all(r['status']=='模拟验证完成' for r in results) else 1
    from configbatch.gui import main as gui
    return gui(args.data_dir)


if __name__=='__main__':raise SystemExit(main())
