import argparse
import json
from pathlib import Path
import sys
from .parser import inspect
from .journal import Journal
from .transport import capture
from .receiver import Receiver
from .store import ReceiveStore


def main():
    parser = argparse.ArgumentParser(description='安装服务合作方回执的原文接收与 XML 摘要工具。')
    sub = parser.add_subparsers(dest='command', required=True)
    view = sub.add_parser('inspect')
    view.add_argument('source')
    raw = sub.add_parser('capture')
    raw.add_argument('url')
    raw.add_argument('--state', default='var')
    raw.add_argument('--method', default='GET')
    raw.add_argument('--body')
    raw.add_argument('--header', action='append', default=[])
    receive = sub.add_parser('receive', help='接收查询窗口内的回执与照片为可重跑业务记录')
    receive.add_argument('origin', help='授权本地接口来源，例如 http://127.0.0.1:8765')
    receive.add_argument('--window', required=True, help='查询窗口标识')
    receive.add_argument('--state', default='var', help='接收状态目录')
    receive.add_argument('--path', default='/receipts', help='回执查询路径')
    export = sub.add_parser('export', help='按工单导出当前可见记录')
    export.add_argument('work_order')
    export.add_argument('--state', default='var')
    failures = sub.add_parser('failures', help='导出失败原因、缺件与冲突留证')
    failures.add_argument('--window', required=True)
    failures.add_argument('--state', default='var')
    args = parser.parse_args()
    try:
        if args.command == 'capture':
            headers = dict(value.split(':', 1) for value in args.header)
            headers = {key.strip(): value.strip() for key, value in headers.items()}
            status, response_headers, payload = capture(args.url, args.method,
                Path(args.body).read_bytes() if args.body else None, headers)
            journal = Journal(args.state)
            try:
                result = journal.save(payload, args.url)
            finally:
                journal.close()
            result.update({'http_status': status, 'headers': response_headers})
        elif args.command == 'inspect':
            result = inspect(Path(args.source).read_bytes())
        elif args.command == 'receive':
            store = ReceiveStore(args.state)
            try:
                result = Receiver(store, args.origin, args.path).run(args.window)
            finally:
                store.close()
            print(json.dumps(result, ensure_ascii=True, sort_keys=True))
            return 0 if result['finished'] else 3
        elif args.command == 'export':
            store = ReceiveStore(args.state)
            try:
                result = store.export_work_order(args.work_order)
            finally:
                store.close()
            if result is None:
                print('unknown work order: ' + args.work_order, file=sys.stderr)
                return 2
            print(json.dumps(result, ensure_ascii=True, sort_keys=True))
            return 0 if result.get('visible') else 2
        else:
            store = ReceiveStore(args.state)
            try:
                result = {'window': args.window,
                          'failures': store.list_failures(args.window)}
            finally:
                store.close()
            print(json.dumps(result, ensure_ascii=True, sort_keys=True))
            return 0
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
