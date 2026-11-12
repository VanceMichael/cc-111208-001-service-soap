"""receive 流程的端到端回归测试，全部走本地合成 HTTP 服务。"""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import quote

from scripts.materialize import main as materialize
from scripts.scenarios import (build_multipart, fault_body, photo_decl,
                               to_base64, wrong_namespace_body, xml_only_batch)
from scripts.source_server import ReplayServer
from servicebridge.receiver import Receiver
from servicebridge.soap import build_query
from servicebridge.store import ReceiveStore


REPO = Path(__file__).resolve().parents[1]


def write_scenario(directory, routes):
    (Path(directory) / 'scenario.json').write_text(
        json.dumps({'routes': routes}, ensure_ascii=True))


def server_in(directory, scenario='scenario.json'):
    server = ReplayServer(directory, scenario)
    import threading
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def multipart_route(boundary, root_cid, xml_body, attachments, status=200,
                    duplicate_cid=False, root_first=False):
    payload = build_multipart(boundary, root_cid, xml_body, attachments,
                              root_first=root_first, duplicate_cid=duplicate_cid)
    return {'status': status,
            'headers': {'Content-Type':
                        'multipart/related; boundary="%s"; start="<%s>"; '
                        'type="application/xop+xml"' % (boundary, root_cid)},
            'base64': to_base64(payload)}


def xml_route(xml_body, status=200):
    return {'status': status,
            'headers': {'Content-Type': 'application/soap+xml'},
            'base64': to_base64(xml_body)}


class ReceiveFlowTests(unittest.TestCase):
    def setUp(self):
        materialize()
        self.tmp = tempfile.mkdtemp()
        self.server = None
        self.thread = None

    def tearDown(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self, routes):
        write_scenario(self.tmp, routes)
        self.server, self.thread = server_in(self.tmp)
        return self.server.origin

    def run_receive(self, origin, window='WIN-1', state=None, path='/receipts'):
        state = state or str(Path(self.tmp) / 'state')
        store = ReceiveStore(state)
        try:
            return Receiver(store, origin, path).run(window)
        finally:
            store.close()

    # -- 协议解析 ------------------------------------------------------
    def test_root_second_part_and_escaped_cid_binds_correct_photo(self):
        photo = b'photo-bytes\r\n--almost-a-boundary\x00\xff'
        decl = photo_decl(photo, 'photo@local')
        xml = xml_only_batch('B1', 'done',
                             {'W1': {'revision': 2, 'photos': [decl]}})
        routes = {'POST /receipts': [multipart_route(
            'part-boundary', 'root@local', xml,
            [('photo@local', 'image/jpeg', photo)])]}
        origin = self.start(routes)
        result = self.run_receive(origin)
        self.assertTrue(result['finished'])
        self.assertEqual(result['accepted'], 1)
        store = ReceiveStore(str(Path(self.tmp) / 'state'))
        try:
            exported = store.export_work_order('W1')
        finally:
            store.close()
        self.assertTrue(exported['visible'])
        stored = Path(exported['photos'][0]['path']).read_bytes()
        self.assertEqual(stored, photo)
        self.assertEqual(exported['photos'][0]['sha256'], decl['sha256'])

    def test_duplicate_content_id_is_rejected_and_cursor_holds(self):
        photo = b'photo-bytes'
        decl = photo_decl(photo, 'photo@local')
        xml = xml_only_batch('B1', 'done',
                             {'W1': {'revision': 2, 'photos': [decl]}})
        routes = {'POST /receipts': [multipart_route(
            'part-boundary', 'root@local', xml,
            [('photo@local', 'image/jpeg', photo)], duplicate_cid=True)]}
        origin = self.start(routes)
        result = self.run_receive(origin)
        self.assertFalse(result['finished'])
        self.assertEqual(result['accepted'], 0)
        store = ReceiveStore(str(Path(self.tmp) / 'state'))
        try:
            self.assertIsNone(store.cursor_for('WIN-1'))
            failures = store.list_failures('WIN-1')
        finally:
            store.close()
        self.assertTrue(any('Duplicate Content-ID' in item['reason'] for item in failures))

    def test_fault_http_200_does_not_advance_cursor(self):
        routes = {'POST /fault': [xml_route(fault_body())]}
        origin = self.start(routes)
        result = self.run_receive(origin, path='/fault')
        self.assertFalse(result['finished'])
        self.assertEqual(result['accepted'], 0)
        store = ReceiveStore(str(Path(self.tmp) / 'state'))
        try:
            self.assertIsNone(store.cursor_for('WIN-1'))
            failures = store.list_failures('WIN-1')
            self.assertTrue(any('Fault' in item['reason'] for item in failures))
            raw_count = store.database.execute(
                'SELECT COUNT(*) FROM raw_responses').fetchone()[0]
        finally:
            store.close()
        self.assertEqual(raw_count, 1)  # 原文证据保留

    def test_wrong_soap_namespace_is_incident_not_success(self):
        routes = {'POST /receipts': [xml_route(wrong_namespace_body())]}
        origin = self.start(routes)
        result = self.run_receive(origin)
        self.assertFalse(result['finished'])
        store = ReceiveStore(str(Path(self.tmp) / 'state'))
        try:
            self.assertIsNone(store.cursor_for('WIN-1'))
        finally:
            store.close()

    # -- 缺件与补件 ----------------------------------------------------
    def test_missing_photo_hides_ticket_and_completion_needs_no_duplicate(self):
        photo = b'signed-delivery-photo'
        decl = photo_decl(photo, 'sign@local')
        first_xml = xml_only_batch('B1', 'done',
                                   {'W1': {'revision': 1, 'photos': [decl]}})
        second_xml = xml_only_batch('B1', 'done',
                                    {'W1': {'revision': 1, 'photos': [decl]}})
        routes = {'POST /receipts': [
            xml_route(first_xml),
            multipart_route('b2', 'root@local', second_xml,
                            [('sign@local', 'image/jpeg', photo)])]}
        origin = self.start(routes)
        first = self.run_receive(origin)
        self.assertTrue(first['finished'])
        self.assertEqual(first['incomplete'], 1)
        state = str(Path(self.tmp) / 'state')
        store = ReceiveStore(state)
        try:
            hidden = store.export_work_order('W1')
            self.assertFalse(hidden['visible'])
            failures = store.list_failures('WIN-1')
            self.assertTrue(any(item['status'] == 'incomplete'
                                and 'sign@local' in item['reason']
                                for item in failures))
        finally:
            store.close()
        # 重跑同一窗口：缺件仍在，重拉第一页做幂等核对，不产生重复记录。
        rerun = self.run_receive(origin, state=state)
        self.assertTrue(rerun['finished'])
        self.assertEqual(rerun['pages'], 1)
        store = ReceiveStore(state)
        try:
            self.assertEqual(store.database.execute(
                'SELECT COUNT(*) FROM receipt_observations').fetchone()[0], 1)
            self.assertEqual(store.database.execute(
                'SELECT COUNT(*) FROM work_orders').fetchone()[0], 1)
        finally:
            store.close()
        # 上游补齐附件（同内容 MTOM）：重跑后同一观察原地转 complete，不生成重复工单。
        second = self.run_receive(origin, window='WIN-1', state=state)
        self.assertTrue(second['finished'])
        store = ReceiveStore(state)
        try:
            visible = store.export_work_order('W1')
            self.assertTrue(visible['visible'])
            self.assertEqual(store.database.execute(
                'SELECT COUNT(*) FROM work_orders').fetchone()[0], 1)
            self.assertEqual(store.database.execute(
                "SELECT COUNT(*) FROM receipt_observations").fetchone()[0], 1)
            self.assertEqual(store.database.execute(
                "SELECT status FROM receipt_observations").fetchone()[0], 'complete')
        finally:
            store.close()

    def test_complete_and_incomplete_tickets_coexist_in_one_batch(self):
        good_photo = b'good-photo'
        good = photo_decl(good_photo, 'good@local')
        missing = photo_decl(b'never-sent', 'missing@local')
        xml = xml_only_batch('B1', 'done', {
            'W-GOOD': {'revision': 1, 'photos': [good]},
            'W-LACK': {'revision': 1, 'photos': [missing]}})
        routes = {'POST /receipts': [multipart_route(
            'mix', 'root@local', xml, [('good@local', 'image/jpeg', good_photo)])]}
        origin = self.start(routes)
        result = self.run_receive(origin)
        self.assertTrue(result['finished'])
        store = ReceiveStore(str(Path(self.tmp) / 'state'))
        try:
            self.assertTrue(store.export_work_order('W-GOOD')['visible'])
            self.assertFalse(store.export_work_order('W-LACK')['visible'])
        finally:
            store.close()

    def test_length_and_digest_mismatches_are_verification_failures(self):
        photo = b'actual-photo-bytes'
        bad_length = dict(photo_decl(photo, 'p@local'), length=999)
        bad_digest = dict(photo_decl(photo, 'p@local'),
                          sha256='0' * 64)
        xml = xml_only_batch('B1', 'done', {
            'W-LEN': {'revision': 1, 'photos': [bad_length]},
            'W-DIG': {'revision': 1, 'photos': [bad_digest]}})
        routes = {'POST /receipts': [multipart_route(
            'v', 'root@local', xml, [('p@local', 'image/jpeg', photo)])]}
        origin = self.start(routes)
        result = self.run_receive(origin)
        self.assertTrue(result['finished'])
        store = ReceiveStore(str(Path(self.tmp) / 'state'))
        try:
            self.assertFalse(store.export_work_order('W-LEN')['visible'])
            self.assertFalse(store.export_work_order('W-DIG')['visible'])
            failures = store.list_failures('WIN-1')
            reasons = ' '.join(item['reason'] or '' for item in failures)
            self.assertIn('length declared=999', reasons)
            self.assertIn('sha256 mismatch', reasons)
            # 校验失败的照片不落盘。
            self.assertEqual(store.database.execute(
                'SELECT COUNT(*) FROM observation_photos WHERE present = 1'
            ).fetchone()[0], 0)
        finally:
            store.close()

    # -- 冲突与修订 ----------------------------------------------------
    def test_same_revision_conflict_is_retained_without_overwrite(self):
        photo_a = b'photo-content-a'
        photo_b = b'photo-content-b-different'
        decl_a = photo_decl(photo_a, 'p@local')
        decl_b = photo_decl(photo_b, 'p@local')
        xml_a = xml_only_batch('B1', 'done',
                               {'W1': {'revision': 3, 'photos': [decl_a]}})
        xml_b = xml_only_batch('B2', 'done',
                               {'W1': {'revision': 3, 'photos': [decl_b]}})
        routes = {'POST /receipts': [
            multipart_route('ba', 'root@local', xml_a,
                            [('p@local', 'image/jpeg', photo_a)]),
            multipart_route('bb', 'root@local', xml_b,
                            [('p@local', 'image/jpeg', photo_b)])]}
        origin = self.start(routes)
        self.run_receive(origin, window='WIN-1')
        state = str(Path(self.tmp) / 'state')
        self.run_receive(origin, window='WIN-2', state=state)
        store = ReceiveStore(state)
        try:
            exported = store.export_work_order('W1')
            self.assertTrue(exported['visible'])
            self.assertEqual(Path(exported['photos'][0]['path']).read_bytes(),
                             photo_a)  # 当前记录仍是先到内容
            self.assertEqual(store.database.execute(
                'SELECT COUNT(*) FROM work_orders').fetchone()[0], 1)
            statuses = [row[0] for row in store.database.execute(
                'SELECT status FROM receipt_observations ORDER BY id')]
            self.assertEqual(statuses, ['complete', 'conflict'])
            failures = store.list_failures('WIN-2')
            self.assertTrue(any(item['status'] == 'conflict' for item in failures))
        finally:
            store.close()

    def test_late_older_revision_cannot_roll_back_current(self):
        new_photo = b'rev-7-photo'
        old_photo = b'rev-2-photo'
        new_decl = photo_decl(new_photo, 'p@local')
        old_decl = photo_decl(old_photo, 'p@local')
        xml_new = xml_only_batch('B1', 'done',
                                 {'W1': {'revision': 7, 'photos': [new_decl]}})
        xml_old = xml_only_batch('B2', 'done',
                                 {'W1': {'revision': 2, 'photos': [old_decl]}})
        routes = {'POST /receipts': [
            multipart_route('bn', 'root@local', xml_new,
                            [('p@local', 'image/jpeg', new_photo)]),
            multipart_route('bo', 'root@local', xml_old,
                            [('p@local', 'image/jpeg', old_photo)])]}
        origin = self.start(routes)
        self.run_receive(origin, window='WIN-1')
        state = str(Path(self.tmp) / 'state')
        self.run_receive(origin, window='WIN-2', state=state)
        store = ReceiveStore(state)
        try:
            exported = store.export_work_order('W1')
            self.assertEqual(exported['revision'], 7)
            self.assertEqual(Path(exported['photos'][0]['path']).read_bytes(), new_photo)
            statuses = [row[0] for row in store.database.execute(
                'SELECT status FROM receipt_observations ORDER BY id')]
            self.assertEqual(statuses, ['complete', 'late_revision'])
        finally:
            store.close()

    def test_incomplete_new_revision_keeps_old_one_visible_until_complete(self):
        old_photo = b'rev-1-photo'
        new_photo = b'rev-2-photo'
        old_decl = photo_decl(old_photo, 'p@local')
        new_decl = photo_decl(new_photo, 'p@local')
        xml_old = xml_only_batch('B1', 'done',
                                 {'W1': {'revision': 1, 'photos': [old_decl]}})
        xml_missing = xml_only_batch('B2', 'done',
                                     {'W1': {'revision': 2, 'photos': [new_decl]}})
        xml_complete = xml_only_batch('B3', 'done',
                                      {'W1': {'revision': 2, 'photos': [new_decl]}})
        routes = {'POST /receipts': [
            multipart_route('o', 'root@local', xml_old,
                            [('p@local', 'image/jpeg', old_photo)]),
            xml_route(xml_missing),
            multipart_route('n', 'root@local', xml_complete,
                            [('p@local', 'image/jpeg', new_photo)])]}
        origin = self.start(routes)
        state = str(Path(self.tmp) / 'state')
        self.run_receive(origin, window='WIN-1', state=state)
        store = ReceiveStore(state)
        try:
            self.assertEqual(store.export_work_order('W1')['revision'], 1)
        finally:
            store.close()
        # 新修订缺件：当前记录仍指向旧修订且保持可见，不暴露半成品。
        self.run_receive(origin, window='WIN-2', state=state)
        store = ReceiveStore(state)
        try:
            exported = store.export_work_order('W1')
            self.assertTrue(exported['visible'])
            self.assertEqual(exported['revision'], 1)
            self.assertEqual(store.database.execute(
                "SELECT status FROM receipt_observations WHERE revision = 2"
            ).fetchone()[0], 'incomplete')
        finally:
            store.close()
        # 新修订补齐后才切换为当前可见记录。
        self.run_receive(origin, window='WIN-3', state=state)
        store = ReceiveStore(state)
        try:
            exported = store.export_work_order('W1')
            self.assertTrue(exported['visible'])
            self.assertEqual(exported['revision'], 2)
            self.assertEqual(Path(exported['photos'][0]['path']).read_bytes(), new_photo)
        finally:
            store.close()

    # -- 分页 ----------------------------------------------------------
    def test_pagination_follows_cursor_and_visible_when_complete(self):
        p1 = b'page-one-photo'
        p2 = b'page-two-photo'
        d1 = photo_decl(p1, 'p1@local')
        d2 = photo_decl(p2, 'p2@local')
        xml1 = xml_only_batch('B1', 'cursor-2',
                              {'W1': {'revision': 1, 'photos': [d1]}})
        xml2 = xml_only_batch('B2', 'done',
                              {'W2': {'revision': 1, 'photos': [d2]}})
        routes = {'POST /receipts': [
            {'match': {'not_contains': 'cursor-2'}, **multipart_route(
                'c1', 'root@local', xml1, [('p1@local', 'image/jpeg', p1)])},
            {'match': {'contains': quote('cursor-2', safe='')},
             **multipart_route('c2', 'root@local', xml2,
                               [('p2@local', 'image/jpeg', p2)])}]}
        origin = self.start(routes)
        result = self.run_receive(origin)
        self.assertTrue(result['finished'])
        self.assertEqual(result['pages'], 2)
        store = ReceiveStore(str(Path(self.tmp) / 'state'))
        try:
            self.assertEqual(store.cursor_for('WIN-1'), 'done')
            self.assertTrue(store.export_work_order('W1')['visible'])
            self.assertTrue(store.export_work_order('W2')['visible'])
            # 第二页请求确实带上了续页游标。
            bodies = [item['body'] for item in self.server.requests]
            self.assertTrue(any('cursor-2' in body for body in bodies[1:]))
        finally:
            store.close()

    def test_fault_second_page_holds_cursor_and_resumes_after(self):
        photo = b'page-one-only'
        decl = photo_decl(photo, 'p1@local')
        xml1 = xml_only_batch('B1', 'cursor-2',
                              {'W1': {'revision': 1, 'photos': [decl]}})
        routes = {'POST /receipts': [
            {'match': {'not_contains': 'cursor-2'}, **multipart_route(
                'c1', 'root@local', xml1, [('p1@local', 'image/jpeg', photo)])},
            {'match': {'contains': quote('cursor-2', safe=''), 'times': 1},
             **xml_route(fault_body())},
            {'match': {'contains': quote('cursor-2', safe='')},
             **xml_route(xml_only_batch('B2', 'done', {}))}]}
        origin = self.start(routes)
        first = self.run_receive(origin)
        self.assertFalse(first['finished'])
        self.assertEqual(first['pages'], 1)
        state = str(Path(self.tmp) / 'state')
        store = ReceiveStore(state)
        try:
            self.assertEqual(store.cursor_for('WIN-1'), 'cursor-2')
            self.assertTrue(store.export_work_order('W1')['visible'])
        finally:
            store.close()
        resumed = self.run_receive(origin, state=state)
        self.assertTrue(resumed['finished'])
        self.assertEqual(resumed['pages'], 1)

    # -- 崩溃恢复 ------------------------------------------------------
    def test_process_killed_after_photo_rename_recovers_consistently(self):
        photo = b'crash-boundary-photo--part-boundary\x00'
        decl = photo_decl(photo, 'p@local')
        xml = xml_only_batch('B1', 'done',
                             {'W1': {'revision': 1, 'photos': [decl]}})
        routes = {'POST /receipts': [multipart_route(
            'k', 'root@local', xml, [('p@local', 'image/jpeg', photo)])]}
        origin = self.start(routes)
        state = str(Path(self.tmp) / 'state')
        env = dict(os.environ)
        env['SERVICEBRIDGE_CRASH_AFTER_PHOTO'] = decl['sha256']
        killed = subprocess.run(
            [sys.executable, '-m', 'servicebridge', 'receive', origin,
             '--window', 'WIN-1', '--state', state, '--path', '/receipts'],
            cwd=str(REPO), env=env, capture_output=True)
        self.assertEqual(killed.returncode, -signal.SIGKILL)
        # 第一次运行留下了照片文件，但游标与工单可见性尚未提交。
        store = ReceiveStore(state)
        try:
            self.assertIsNone(store.cursor_for('WIN-1'))
            self.assertIsNone(store.export_work_order('W1'))
            self.assertTrue(store.photo_store.exists(decl['sha256']))
        finally:
            store.close()
        # 重启：同一照片按内容寻址复用，窗口正常接收完成。
        resumed = self.run_receive(origin, state=state)
        self.assertTrue(resumed['finished'])
        store = ReceiveStore(state)
        try:
            exported = store.export_work_order('W1')
            self.assertTrue(exported['visible'])
            self.assertEqual(Path(exported['photos'][0]['path']).read_bytes(), photo)
            self.assertEqual(store.cursor_for('WIN-1'), 'done')
            # 原始响应证据仍可追溯。
            raw = Path(exported['raw_response']['path'])
            self.assertTrue(raw.exists())
            self.assertEqual(store.database.execute(
                'SELECT COUNT(*) FROM batches').fetchone()[0], 1)
        finally:
            store.close()

    def test_repair_hides_ticket_when_photo_blob_is_lost(self):
        photo = b'will-disappear'
        decl = photo_decl(photo, 'p@local')
        xml = xml_only_batch('B1', 'done',
                             {'W1': {'revision': 1, 'photos': [decl]}})
        routes = {'POST /receipts': [multipart_route(
            'r', 'root@local', xml, [('p@local', 'image/jpeg', photo)])]}
        origin = self.start(routes)
        state = str(Path(self.tmp) / 'state')
        self.run_receive(origin, state=state)
        store = ReceiveStore(state)
        try:
            blob = Path(store.export_work_order('W1')['photos'][0]['path'])
        finally:
            store.close()
        blob.unlink()  # 模拟落盘文件在提交后丢失
        self.run_receive(origin, state=state)
        store = ReceiveStore(state)
        try:
            self.assertFalse(store.export_work_order('W1')['visible'])
            self.assertTrue(any(item['status'] == 'recovery_hidden'
                                for item in store.list_failures('WIN-1')))
        finally:
            store.close()

    # -- CLI -----------------------------------------------------------
    def test_cli_receive_export_and_failures(self):
        photo = b'cli-photo'
        decl = photo_decl(photo, 'p@local')
        missing = photo_decl(b'absent', 'm@local')
        xml = xml_only_batch('B1', 'done', {
            'W1': {'revision': 1, 'photos': [decl]},
            'W2': {'revision': 1, 'photos': [missing]}})
        routes = {'POST /receipts': [multipart_route(
            'cli', 'root@local', xml, [('p@local', 'image/jpeg', photo)])]}
        origin = self.start(routes)
        state = str(Path(self.tmp) / 'state')
        receive = subprocess.run(
            [sys.executable, '-m', 'servicebridge', 'receive', origin,
             '--window', 'WIN-1', '--state', state],
            cwd=str(REPO), capture_output=True, text=True)
        self.assertEqual(receive.returncode, 0, receive.stderr)
        payload = json.loads(receive.stdout.strip().splitlines()[-1])
        self.assertEqual(payload['accepted'], 1)
        exported = subprocess.run(
            [sys.executable, '-m', 'servicebridge', 'export', 'W1',
             '--state', state], cwd=str(REPO), capture_output=True, text=True)
        self.assertEqual(exported.returncode, 0)
        record = json.loads(exported.stdout)
        self.assertEqual(record['work_order'], 'W1')
        hidden = subprocess.run(
            [sys.executable, '-m', 'servicebridge', 'export', 'W2',
             '--state', state], cwd=str(REPO), capture_output=True, text=True)
        self.assertEqual(hidden.returncode, 2)
        failures = subprocess.run(
            [sys.executable, '-m', 'servicebridge', 'failures',
             '--window', 'WIN-1', '--state', state],
            cwd=str(REPO), capture_output=True, text=True)
        self.assertEqual(failures.returncode, 0)
        self.assertTrue(json.loads(failures.stdout)['failures'])
        # 请求确实是 SOAP 1.2 Query，且只走 loopback。
        logged = self.server.requests[0]
        self.assertEqual(logged['method'], 'POST')
        self.assertIn('Query', logged['body'])
        self.assertIn(build_query('WIN-1', '')[:60].decode(), logged['body'])

    def test_query_request_escapes_window_and_cursor(self):
        xml = xml_only_batch('B1', 'done', {})
        origin = self.start({'POST /receipts': [xml_route(xml)]})
        self.run_receive(origin, window='WIN&"<>')
        body = self.server.requests[0]['body']
        self.assertIn('&amp;', body)
        self.assertIn('&quot;', body)
        self.assertIn('&lt;', body)

    def test_empty_batch_still_closes_window(self):
        xml = xml_only_batch('B-empty', 'done', {})
        origin = self.start({'POST /receipts': [xml_route(xml)]})
        result = self.run_receive(origin)
        self.assertTrue(result['finished'])
        self.assertEqual(result['accepted'], 0)
        store = ReceiveStore(str(Path(self.tmp) / 'state'))
        try:
            self.assertEqual(store.cursor_for('WIN-1'), 'done')
            self.assertEqual(store.list_visible(), [])
        finally:
            store.close()

    def test_boundary_lookalike_bytes_do_not_split_attachment(self):
        # 附件正文中包含边界字节，但前面不是 CRLF，分段只承认完整边界行。
        photo = b'head--part-boundary\x00tail'
        decl = photo_decl(photo, 'p@local')
        xml = xml_only_batch('B1', 'done',
                             {'W1': {'revision': 0, 'photos': [decl]}})
        routes = {'POST /receipts': [multipart_route(
            'part-boundary', 'root@local', xml,
            [('p@local', 'image/jpeg', photo)])]}
        origin = self.start(routes)
        self.run_receive(origin)
        store = ReceiveStore(str(Path(self.tmp) / 'state'))
        try:
            exported = store.export_work_order('W1')
            self.assertTrue(exported['visible'])
            self.assertEqual(Path(exported['photos'][0]['path']).read_bytes(), photo)
        finally:
            store.close()


if __name__ == '__main__':
    unittest.main()
