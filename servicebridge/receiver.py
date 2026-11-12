"""receive 业务流程：分页拉取、附件绑定校验、事务化接收与恢复。"""
import hashlib
import os

from .soap import ProtocolError, build_query, parse_message
from .transport import capture


class Receiver:
    def __init__(self, store, origin, path='/receipts'):
        self.store = store
        self.url = origin.rstrip('/') + path
        self.path = path

    def run(self, window, rescan=True):
        # 每次启动都先核对已可见工单的照片，文件丢失立即撤回可见性。
        self.store.repair_visibility(window)
        if self.store.window_finished(window):
            if rescan and self._has_incomplete(window):
                # 窗口曾结束，但缺件工单的补件可能已经到达：回到起点重拉，
                # 已存观察按内容哈希幂等，完整工单不会重复生成。
                self.store.reopen_window(window)
            else:
                return {'window': window, 'finished': True, 'pages': 0,
                        'accepted': 0, 'incomplete': 0, 'conflicts': 0,
                        'stopped': None}
        cursor = self.store.cursor_for(window)
        if cursor is None:
            cursor = ''
        pages = 0
        accepted = 0
        incomplete = 0
        conflicts = 0
        while True:
            page = self._fetch_page(window, cursor)
            if page is not None:
                raw_digest, content_type, payload = page
                try:
                    batch = parse_message(content_type, payload)
                except ProtocolError as error:
                    # HTTP 200 不代表业务成功：Fault/协议错误留证且不推进游标。
                    self.store.record_incident(
                        window, 'protocol', str(error),
                        batch_id=None, raw_digest=raw_digest)
                    return self._summary(window, pages, accepted, incomplete,
                                         conflicts, str(error))
                plans = self._build_plans(window, batch)
                for plan in plans.values():
                    if plan['complete']:
                        accepted += 1
                    else:
                        incomplete += 1
                self.store.commit_batch(window, cursor, batch, raw_digest, plans)
                pages += 1
                if batch.next_cursor == 'done':
                    return self._summary(window, pages, accepted, incomplete,
                                         conflicts, None)
                cursor = batch.next_cursor
            else:
                return self._summary(window, pages, accepted, incomplete, conflicts,
                                     'request failed; cursor not advanced')

    def _has_incomplete(self, window):
        row = self.store.database.execute(
            'SELECT 1 FROM receipt_observations WHERE window = ?'
            " AND status = 'incomplete' LIMIT 1", (window,)).fetchone()
        return row is not None

    def _fetch_page(self, window, cursor):
        body = build_query(window, cursor)
        headers = {'Content-Type': 'application/soap+xml; charset=utf-8'}
        try:
            status, response_headers, payload = capture(
                self.url, 'POST', body, headers)
        except (ValueError, OSError) as error:
            self.store.record_incident(window, 'transport', str(error))
            return None
        content_type = response_headers.get('Content-Type', '')
        raw_digest, _ = self.store.save_raw(payload, content_type, self.path)
        if status != 200:
            self.store.record_incident(
                window, 'http', 'upstream returned HTTP status %d' % status,
                raw_digest=raw_digest)
            return None
        return raw_digest, content_type, payload

    def _build_plans(self, window, batch):
        crash_hook = _photo_rename_crash_hook()
        plans = {}
        for receipt in batch.receipts:
            photo_rows = []
            missing = []
            mismatches = []
            for photo in receipt.photos:
                part = batch.attachments.get(photo.cid)
                if part is None:
                    # 缺件是“附件未收到”，绝不能被当成“客户未签收”。
                    missing.append(photo.cid)
                    photo_rows.append({'cid': photo.cid,
                                       'declared_digest': photo.sha256,
                                       'actual_digest': None,
                                       'declared_length': photo.length,
                                       'actual_length': None,
                                       'blob_path': None, 'present': False})
                    continue
                content = part.content
                actual_length = len(content)
                actual_digest = hashlib.sha256(content).hexdigest()
                photo_failed = False
                if actual_length != photo.length:
                    mismatches.append(
                        '%s length declared=%d actual=%d'
                        % (photo.cid, photo.length, actual_length))
                    photo_failed = True
                if actual_digest != photo.sha256:
                    mismatches.append(
                        '%s sha256 mismatch declared=%s actual=%s'
                        % (photo.cid, photo.sha256, actual_digest))
                    photo_failed = True
                if photo_failed:
                    photo_rows.append({'cid': photo.cid,
                                       'declared_digest': photo.sha256,
                                       'actual_digest': actual_digest,
                                       'declared_length': photo.length,
                                       'actual_length': actual_length,
                                       'blob_path': None, 'present': False})
                    continue
                _, blob_path = self.store.photo_store.write(
                    content, crash_after_rename=crash_hook)
                photo_rows.append({'cid': photo.cid,
                                   'declared_digest': photo.sha256,
                                   'actual_digest': actual_digest,
                                   'declared_length': photo.length,
                                   'actual_length': actual_length,
                                   'blob_path': blob_path, 'present': True})
            plans[receipt.work_order] = {
                'observation': receipt,
                'photos': photo_rows,
                'missing': missing,
                'mismatches': mismatches,
                'complete': not missing and not mismatches,
            }
        return plans

    @staticmethod
    def _summary(window, pages, accepted, incomplete, conflicts, stopped):
        return {'window': window, 'finished': stopped is None,
                'pages': pages, 'accepted': accepted, 'incomplete': incomplete,
                'conflicts': conflicts, 'stopped': stopped}


def _photo_rename_crash_hook():
    target = os.environ.get('SERVICEBRIDGE_CRASH_AFTER_PHOTO')
    if not target:
        return None

    def hook(digest):
        return digest == target or target == '*'

    return hook
