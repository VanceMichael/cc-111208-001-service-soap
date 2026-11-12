"""端到端演示：缺件回执先不可见，补齐照片后才成为可查询的完工记录。

全程使用本地合成服务与临时状态目录，不访问任何真实客户系统。
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from .materialize import main as materialize
from .source_server import running


def run_cli(*arguments):
    result = subprocess.run([sys.executable, '-m', 'servicebridge', *arguments],
                            capture_output=True, text=True)
    print('$ servicebridge ' + ' '.join(arguments))
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print('stderr: ' + result.stderr.strip())
    print('exit=%d' % result.returncode)
    print()
    return result


def main():
    materialize()
    run_cli('inspect', 'fixtures/receipt.xml')
    with running() as server, tempfile.TemporaryDirectory() as state:
        origin = server.origin
        demo_path = '/receive-demo'
        # 第一页是 XML-only 且带续页游标：工单 W1 声明了照片但本批没有附件
        # → 缺件留证、工单不可见，游标停在 cursor-mtom。
        first = run_cli('receive', origin, '--window', 'WIN-2026-11-12',
                        '--state', state, '--path', demo_path)
        assert first.returncode == 0
        run_cli('export', 'W1', '--state', state)
        run_cli('failures', '--window', 'WIN-2026-11-12', '--state', state)

        # HTTP 200 的 SOAP Fault：另一个窗口拉取失败，游标不得推进。
        run_cli('receive', origin, '--window', 'WIN-FAULT', '--state', state,
                '--path', '/fault')

        # 续页返回根部在后、附件乱序的 MTOM：同内容补件，工单转为可见且不重复。
        second = run_cli('receive', origin, '--window', 'WIN-2026-11-12',
                         '--state', state, '--path', demo_path)
        assert second.returncode == 0
        visible = run_cli('export', 'W1', '--state', state)
        assert visible.returncode == 0
        record = json.loads(visible.stdout)
        stored = Path(record['photos'][0]['path'])
        assert stored.exists() and stored.stat().st_size == record['photos'][0]['length']

        # 已完成窗口重跑：直接短路，不产生重复记录。
        run_cli('receive', origin, '--window', 'WIN-2026-11-12',
                '--state', state)
        run_cli('failures', '--window', 'WIN-2026-11-12', '--state', state)

        # 保留原有的 capture 原文留存能力。
        run_cli('capture', origin + '/receipts', '--method', 'POST',
                '--state', str(Path(state) / 'capture'))

        audit = json.dumps({'request_count': len(server.requests),
                            'methods': sorted({item['method'] for item in server.requests}),
                            'paths': sorted({item['path'] for item in server.requests})},
                           ensure_ascii=True)
        print('request audit: ' + audit)


if __name__ == '__main__':
    main()
