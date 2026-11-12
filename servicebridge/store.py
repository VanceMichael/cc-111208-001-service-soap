"""接收状态存储：SQLite 状态库与内容寻址落盘。

崩溃安全的不变量：
- 照片先以临时文件 fsync 后 rename 到内容寻址路径，再写 SQLite；
- 工单可见性与游标在同一个事务里推进，提交前外部看不到半成品；
- 启动时核对可见工单引用的照片，文件缺失或摘要不符立即撤回可见性。
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS windows (
    win TEXT PRIMARY KEY,
    cursor TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS raw_responses (
    digest TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    content_type TEXT NOT NULL,
    request_path TEXT NOT NULL,
    stored_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    cursor_before TEXT NOT NULL,
    cursor_after TEXT NOT NULL,
    raw_digest TEXT NOT NULL REFERENCES raw_responses(digest),
    received_at TEXT NOT NULL,
    UNIQUE(window, batch_id)
);
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window TEXT NOT NULL,
    batch_id TEXT,
    work_order TEXT,
    revision INTEGER,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    raw_digest TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receipt_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window TEXT NOT NULL,
    work_order TEXT NOT NULL,
    revision INTEGER NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    reason TEXT,
    batch_id TEXT NOT NULL,
    raw_digest TEXT NOT NULL REFERENCES raw_responses(digest),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observation_photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER NOT NULL REFERENCES receipt_observations(id),
    cid TEXT NOT NULL,
    declared_digest TEXT NOT NULL,
    actual_digest TEXT,
    declared_length INTEGER NOT NULL,
    actual_length INTEGER,
    blob_path TEXT,
    present INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS work_orders (
    work_order TEXT PRIMARY KEY,
    current_revision INTEGER NOT NULL,
    observation_id INTEGER NOT NULL REFERENCES receipt_observations(id),
    visible INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class BlobStore:
    """按 sha256 内容寻址的二进制存储，临时文件 + fsync + rename。"""

    def __init__(self, root, sharded=False):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.sharded = sharded

    def path_for(self, digest):
        if self.sharded:
            return self.root / digest[:2] / digest[2:4] / digest
        return self.root / digest

    def write(self, content, crash_after_rename=None):
        digest = hashlib.sha256(content).hexdigest()
        destination = self.path_for(digest)
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(dir=destination.parent)
            try:
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, destination)
                if crash_after_rename is not None and crash_after_rename(digest):
                    # 测试崩溃点：rename 已完成、SQLite 尚未提交。
                    os.kill(os.getpid(), signal_sigkill())
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        return digest, str(destination)

    def exists(self, digest):
        return self.path_for(digest).exists()

    def open_blob(self, digest):
        return self.path_for(digest).open('rb')


def signal_sigkill():
    import signal
    return signal.SIGKILL


def content_hash(work_order, revision, photos):
    """同一工单同一修订、同一组照片声明才视为同一内容。"""
    declared = sorted((photo.cid, photo.sha256, photo.length) for photo in photos)
    payload = json.dumps({'work_order': work_order, 'revision': revision,
                          'photos': declared}, sort_keys=True).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


class ReceiveStore:
    def __init__(self, state_dir):
        self.root = Path(state_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.raw_store = BlobStore(self.root / 'raw')
        self.photo_store = BlobStore(self.root / 'photos', sharded=True)
        self.database = sqlite3.connect(self.root / 'receive.sqlite3')
        self.database.execute('PRAGMA foreign_keys = ON')
        self.database.executescript(SCHEMA)

    def close(self):
        self.database.close()

    # -- 原始响应 ------------------------------------------------------
    def save_raw(self, payload, content_type, request_path):
        digest, path = self.raw_store.write(payload)
        with self.database:
            self.database.execute(
                'INSERT OR IGNORE INTO raw_responses VALUES (?, ?, ?, ?, ?)',
                (digest, len(payload), content_type, request_path, utc_now()))
        return digest, path

    def raw_path(self, digest):
        return str(self.raw_store.path_for(digest))

    # -- 游标 ----------------------------------------------------------
    def cursor_for(self, window):
        row = self.database.execute(
            'SELECT cursor FROM windows WHERE win = ?', (window,)).fetchone()
        return None if row is None else row[0]

    def window_finished(self, window):
        return self.cursor_for(window) == 'done'

    def reopen_window(self, window):
        """缺件工单的补件可能在窗口结束后才到达：重跑时回到窗口起点重新拉取，
        已落库的观察按内容哈希幂等去重，不会生成重复工单。"""
        with self.database:
            self.database.execute('DELETE FROM windows WHERE win = ?', (window,))

    # -- 故障留证 ------------------------------------------------------
    def record_incident(self, window, kind, detail, batch_id=None, work_order=None,
                        revision=None, raw_digest=None):
        with self.database:
            self._insert_incident(window, kind, detail, batch_id, work_order,
                                  revision, raw_digest)

    def _insert_incident(self, window, kind, detail, batch_id, work_order,
                         revision, raw_digest):
        # 同一窗口同类同详情的留证幂等，避免重跑时堆叠重复故障。
        exists = self.database.execute(
            'SELECT 1 FROM incidents WHERE window = ? AND kind = ? AND detail = ?'
            ' AND COALESCE(batch_id, \'\') = COALESCE(?, \'\')'
            ' AND COALESCE(work_order, \'\') = COALESCE(?, \'\')'
            ' AND revision IS ?',
            (window, kind, detail, batch_id, work_order, revision)).fetchone()
        if exists:
            return
        self.database.execute(
            'INSERT INTO incidents (window, batch_id, work_order, revision, kind,'
            ' detail, raw_digest, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (window, batch_id, work_order, revision, kind, detail,
             raw_digest, utc_now()))

    # -- 批次事务提交 --------------------------------------------------
    def commit_batch(self, window, cursor_before, batch, raw_digest, plans):
        """plans: {work_order: {'observation': ReceiptDecl, 'photos': [photo_row],
        'complete': bool, 'missing': [cid...], 'mismatches': [str...]}}。"""
        now = utc_now()
        with self.database:
            self.database.execute(
                'INSERT OR IGNORE INTO batches (window, batch_id, cursor_before,'
                ' cursor_after, raw_digest, received_at) VALUES (?, ?, ?, ?, ?, ?)',
                (window, batch.batch_id, cursor_before, batch.next_cursor,
                 raw_digest, now))
            for work_order, plan in plans.items():
                self._commit_observation(window, batch, raw_digest, work_order, plan, now)
            self.database.execute(
                'INSERT INTO windows (win, cursor, updated_at) VALUES (?, ?, ?)'
                ' ON CONFLICT(win) DO UPDATE SET cursor = excluded.cursor,'
                ' updated_at = excluded.updated_at',
                (window, batch.next_cursor, now))

    def _commit_observation(self, window, batch, raw_digest, work_order, plan, now):
        observation = plan['observation']
        digest_hash = content_hash(work_order, observation.revision, observation.photos)
        existing = self.database.execute(
            'SELECT id, status FROM receipt_observations WHERE content_hash = ?',
            (digest_hash,)).fetchone()
        if existing is None:
            status = 'complete' if plan['complete'] else 'incomplete'
            reason = None
            if not plan['complete']:
                reason = self._explain_plan(plan)
            cursor = self.database.execute(
                'INSERT INTO receipt_observations (window, work_order, revision,'
                ' content_hash, status, reason, batch_id, raw_digest, created_at)'
                ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (window, work_order, observation.revision, digest_hash, status, reason,
                 batch.batch_id, raw_digest, now))
            observation_id = cursor.lastrowid
            for photo in plan['photos']:
                self.database.execute(
                    'INSERT INTO observation_photos (observation_id, cid,'
                    ' declared_digest, actual_digest, declared_length, actual_length,'
                    ' blob_path, present) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    (observation_id, photo['cid'], photo['declared_digest'],
                     photo['actual_digest'], photo['declared_length'],
                     photo['actual_length'], photo['blob_path'],
                     1 if photo['present'] else 0))
        else:
            observation_id, old_status = existing
            if old_status == 'incomplete' and plan['complete']:
                # 同一内容补件：原地补齐，不生成第二条工单记录。
                self.database.execute(
                    'UPDATE receipt_observations SET status = ?, reason = NULL WHERE id = ?',
                    ('complete', observation_id))
                self.database.execute(
                    'DELETE FROM observation_photos WHERE observation_id = ?',
                    (observation_id,))
                for photo in plan['photos']:
                    self.database.execute(
                        'INSERT INTO observation_photos (observation_id, cid,'
                        ' declared_digest, actual_digest, declared_length, actual_length,'
                        ' blob_path, present) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                        (observation_id, photo['cid'], photo['declared_digest'],
                         photo['actual_digest'], photo['declared_length'],
                         photo['actual_length'], photo['blob_path'], 1))
            else:
                return self._reconcile_work_order(window, batch, work_order,
                                                  observation, observation_id, plan, now)
        self._reconcile_work_order(window, batch, work_order, observation,
                                   observation_id, plan, now)

    def _reconcile_work_order(self, window, batch, work_order, observation,
                              observation_id, plan, now):
        current = self.database.execute(
            'SELECT current_revision, observation_id, visible FROM work_orders'
            ' WHERE work_order = ?', (work_order,)).fetchone()
        if current is None:
            self.database.execute(
                'INSERT INTO work_orders (work_order, current_revision, observation_id,'
                ' visible, updated_at) VALUES (?, ?, ?, ?, ?)',
                (work_order, observation.revision, observation_id,
                 1 if plan['complete'] else 0, now))
            if not plan['complete']:
                self._incident_locked(window, batch.batch_id, work_order,
                                      observation.revision, 'incomplete',
                                      self._explain_plan(plan))
            return
        current_revision, current_observation_id, visible = current
        if observation.revision > current_revision:
            # 新修订缺件时保留旧修订的可见性，直到新修订照片齐全。
            next_visible = 1 if plan['complete'] else visible
            self.database.execute(
                'UPDATE work_orders SET current_revision = ?, observation_id = ?,'
                ' visible = ?, updated_at = ? WHERE work_order = ?',
                (observation.revision, observation_id,
                 next_visible, now, work_order))
            if not plan['complete']:
                self._incident_locked(window, batch.batch_id, work_order,
                                      observation.revision, 'incomplete',
                                      self._explain_plan(plan))
        elif observation.revision == current_revision and observation_id != current_observation_id:
            # 同修订内容冲突：独立留证，当前记录不被覆盖。
            self.database.execute(
                'UPDATE receipt_observations SET status = ?, reason = ? WHERE id = ?',
                ('conflict', 'same revision delivered with conflicting content',
                 observation_id))
            self._incident_locked(window, batch.batch_id, work_order,
                                  observation.revision, 'conflict',
                                  'conflicting content for current revision; record retained')
        elif observation.revision < current_revision:
            self.database.execute(
                'UPDATE receipt_observations SET status = ?, reason = ? WHERE id = ?',
                ('late_revision', 'older revision arrived after a newer one',
                 observation_id))
            self._incident_locked(window, batch.batch_id, work_order,
                                  observation.revision, 'late_revision',
                                  'revision %d is older than current %d'
                                  % (observation.revision, current_revision))
        elif observation_id == current_observation_id and plan['complete']:
            # 当前工单（含重投的同一缺件观察）现在照片齐全：转为可见。
            self.database.execute(
                'UPDATE work_orders SET visible = 1, updated_at = ? WHERE work_order = ?',
                (now, work_order))

    def _incident_locked(self, window, batch_id, work_order, revision, kind, detail):
        self._insert_incident(window, kind, detail, batch_id, work_order,
                              revision, None)

    @staticmethod
    def _explain_plan(plan):
        problems = []
        if plan['missing']:
            problems.append('missing attachments: ' + ','.join(plan['missing']))
        problems.extend(plan['mismatches'])
        return '; '.join(problems) if problems else 'incomplete receipt'

    # -- 崩溃恢复 ------------------------------------------------------
    def repair_visibility(self, window):
        """撤回可见工单（其任一完整观察引用的照片缺失或摘要不符）的可见性。"""
        rows = self.database.execute(
            'SELECT DISTINCT wo.work_order, op.blob_path, op.actual_digest, op.present'
            ' FROM work_orders wo'
            ' JOIN receipt_observations ro ON ro.work_order = wo.work_order'
            ' JOIN observation_photos op ON op.observation_id = ro.id'
            ' WHERE wo.visible = 1 AND ro.window = ? AND ro.status = ?',
            (window, 'complete')).fetchall()
        for work_order, blob_path, actual_digest, present in rows:
            reason = None
            if not present or not blob_path:
                reason = 'photo reference lacks stored blob after recovery'
            elif not Path(blob_path).exists():
                reason = 'stored photo disappeared before commit completed'
            else:
                digest = hashlib.sha256(Path(blob_path).read_bytes()).hexdigest()
                if digest != actual_digest:
                    reason = 'stored photo digest no longer matches declaration'
            if reason:
                with self.database:
                    self.database.execute(
                        'UPDATE work_orders SET visible = 0 WHERE work_order = ?',
                        (work_order,))
                    self._insert_incident(window, 'recovery_hidden', reason,
                                          None, work_order, None, None)

    # -- 查询与导出 ----------------------------------------------------
    def list_visible(self):
        return self.database.execute(
            'SELECT work_order, current_revision FROM work_orders WHERE visible = 1'
            ' ORDER BY work_order').fetchall()

    def export_work_order(self, work_order):
        row = self.database.execute(
            'SELECT current_revision, observation_id, visible FROM work_orders'
            ' WHERE work_order = ?', (work_order,)).fetchone()
        if row is None:
            return None
        current_revision, pointer_id, visible = row
        if not visible:
            status = self.database.execute(
                'SELECT status FROM receipt_observations WHERE id = ?',
                (pointer_id,)).fetchone()[0]
            return {'work_order': work_order, 'visible': False,
                    'current_revision': current_revision, 'status': status}
        # 指针可能指向尚未补齐的更新修订；可见时导出最新的完整观察。
        visible_row = self.database.execute(
            'SELECT id, raw_digest, created_at, revision FROM receipt_observations'
            ' WHERE work_order = ? AND status = ? ORDER BY revision DESC, id DESC'
            ' LIMIT 1', (work_order, 'complete')).fetchone()
        if visible_row is None:
            return {'work_order': work_order, 'visible': False,
                    'current_revision': current_revision, 'status': 'incomplete'}
        observation_id, raw_digest, created_at, revision = visible_row
        photos = [{'cid': cid, 'sha256': actual, 'length': length, 'path': path}
                  for cid, actual, length, path in self.database.execute(
                      'SELECT cid, actual_digest, actual_length, blob_path'
                      ' FROM observation_photos WHERE observation_id = ? ORDER BY cid',
                      (observation_id,))]
        return {
            'work_order': work_order,
            'visible': True,
            'revision': revision,
            'received_at': created_at,
            'photos': photos,
            'raw_response': {'digest': raw_digest, 'path': self.raw_path(raw_digest)},
        }

    def list_failures(self, window):
        result = []
        observations = self.database.execute(
            'SELECT work_order, revision, status, reason, batch_id, created_at'
            ' FROM receipt_observations WHERE window = ? AND status != ?'
            ' ORDER BY id', (window, 'complete')).fetchall()
        for work_order, revision, status, reason, batch_id, created_at in observations:
            result.append({'kind': 'observation', 'work_order': work_order,
                           'revision': revision, 'status': status, 'reason': reason,
                           'batch_id': batch_id, 'created_at': created_at})
        incidents = self.database.execute(
            'SELECT work_order, revision, kind, detail, batch_id, created_at'
            ' FROM incidents WHERE window = ? ORDER BY id', (window,)).fetchall()
        for work_order, revision, kind, detail, batch_id, created_at in incidents:
            result.append({'kind': 'incident', 'work_order': work_order,
                           'revision': revision, 'status': kind, 'reason': detail,
                           'batch_id': batch_id, 'created_at': created_at})
        return result
