import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from servicebridge.parser import inspect
from servicebridge.journal import Journal
from servicebridge.transport import capture
from scripts.materialize import main as materialize
from scripts.source_server import running


class BaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        materialize()

    def test_capture_journal_is_content_addressed(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory)
            try:
                first = journal.save(b'synthetic', 'source-1')
                second = journal.save(b'synthetic', 'source-1')
                self.assertEqual(first, second)
                self.assertEqual(Path(first['path']).read_bytes(), b'synthetic')
                self.assertEqual(journal.database.execute('SELECT COUNT(*) FROM captures').fetchone()[0], 1)
            finally:
                journal.close()

    def test_remote_transport_is_rejected_before_io(self):
        with self.assertRaises(ValueError):
            capture('https://example.invalid/private')

    def test_cli_help(self):
        result = subprocess.run([sys.executable, '-m', 'servicebridge', '--help'], capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn(b'inspect', result.stdout)

    def test_local_source_and_request_log(self):
        with running() as server:
            status, headers, payload = capture(server.origin + '/receipts', 'POST', headers={})
            self.assertEqual(status, 200)
            self.assertGreater(len(payload), 0)
            self.assertEqual(server.requests[0]['method'], 'POST')

    def test_unknown_source_route_is_not_success(self):
        with running() as server:
            self.assertEqual(capture(server.origin + '/not-present')[0], 404)

    def test_receipt_identity(self):
        self.assertEqual(inspect(Path('fixtures/receipt.xml').read_bytes())['receipts'], [{'id': 'W1', 'revision': '2'}])

    def test_fault_is_not_empty_success(self):
        with self.assertRaisesRegex(ValueError, 'Fault'):
            inspect(Path('fixtures/fault.xml').read_bytes())

    def test_wrong_soap_namespace(self):
        with self.assertRaises(ValueError):
            inspect(b'<Envelope/>')


if __name__ == '__main__':
    unittest.main()
