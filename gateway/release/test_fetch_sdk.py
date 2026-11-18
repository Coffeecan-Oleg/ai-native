"""SDK fetch accepts pinned bytes only and preserves a previous file on failure."""
import hashlib
import io
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).with_name('fetch-sdk.py')


class FetchSdkTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='gws-'))).resolve()
        self.target = self.root / 'gateway_mcp/help_assets/bitrix24-sdk-v1.js'
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b'previous-copy')
        self.payload = b'synthetic SDK fixture, not distributed official JavaScript'
        self.info = {'source_url':'https://api.bitrix24.tech/api/v1/', 'bytes':len(self.payload),
                     'sha256':hashlib.sha256(self.payload).hexdigest()}

    def fetch(self, *, docker_layout=False, payload=None):
        metadata = self.root / ('release-sdk-provenance.json' if docker_layout else 'docs/bitrix-sdk-provenance.json')
        metadata.parent.mkdir(parents=True, exist_ok=True)
        metadata.write_text(json.dumps(self.info), encoding='utf-8')
        with patch('sys.argv', [str(SCRIPT), '--source-root', str(self.root)]), \
             patch('urllib.request.urlopen', return_value=io.BytesIO(self.payload if payload is None else payload)) as request:
            runpy.run_path(str(SCRIPT), run_name='__main__')
        request.assert_called_once_with('https://api.bitrix24.tech/api/v1/', timeout=45)

    def test_checkout_layout_fetches_only_hash_checked_bytes(self):
        self.fetch()
        self.assertEqual(self.target.read_bytes(), self.payload)

    def test_existing_docker_layout_remains_supported(self):
        self.fetch(docker_layout=True)
        self.assertEqual(self.target.read_bytes(), self.payload)

    def test_changed_hash_fails_without_replacing_existing_file(self):
        with self.assertRaisesRegex(AssertionError, 'SDK changed'):
            self.fetch(payload=b'x' * len(self.payload))
        self.assertEqual(self.target.read_bytes(), b'previous-copy')

    def test_changed_length_fails_without_replacing_existing_file(self):
        with self.assertRaisesRegex(AssertionError, 'SDK length'):
            self.fetch(payload=self.payload+b'x')
        self.assertEqual(self.target.read_bytes(), b'previous-copy')

    def test_unexpected_source_url_is_rejected_before_network(self):
        self.info['source_url'] = 'https://untrusted.example.invalid/sdk'
        with patch('urllib.request.urlopen') as request, self.assertRaises(AssertionError):
            self.fetch()
        request.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b'previous-copy')


if __name__ == '__main__':
    unittest.main()
