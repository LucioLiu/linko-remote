import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from unittest.mock import patch

from lanpilot.build_unified import windows, copy_code, CLIENT_FILES, ROOT


class UnifiedBuildTests(unittest.TestCase):
    def test_release_payload_uses_an_allowlist_and_rejects_leftover_profiles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = root / 'source', root / 'package'
            for name in CLIENT_FILES:
                target = source / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / name).read_bytes())
            (source / 'lanpilot/client.json').write_text('{"credential":"must-not-ship"}')
            (source / 'windows/devices.json').write_text('["private-device"]')
            with patch('lanpilot.build_unified.ROOT', source):
                copy_code(output, {'url': 'https://example.test', 'pin': 'a' * 64})
            paths = {p.relative_to(output).as_posix() for p in output.rglob('*') if p.is_file()}
            self.assertEqual(set(CLIENT_FILES) | {'peer-endpoint.json', 'release.json'}, paths)
            (output / 'client.json').write_text('{"credential":"must-not-ship"}')
            with self.assertRaisesRegex(ValueError, 'Unexpected files'):
                copy_code(output, {'url': 'https://example.test', 'pin': 'a' * 64})

    def test_repeated_build_has_a_verifiable_manifest_without_self_reference(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / 'python.zip'
            with zipfile.ZipFile(runtime, 'w') as archive:
                archive.writestr('python315._pth', 'python315.zip\n.\n')
            output = root / 'package'
            pin = hashlib.sha256(runtime.read_bytes()).hexdigest()
            endpoint = {'url': 'https://example.test', 'pin': 'a' * 64}
            for _ in range(2):
                windows(output, endpoint, runtime, pin)
                manifest = json.loads((output / 'checksums.json').read_text())
                self.assertNotIn('checksums.json', manifest)
                self.assertTrue(all(hashlib.sha256((output / name).read_bytes()).hexdigest() == digest
                                    for name, digest in manifest.items()))
                self.assertEqual(b'\xef\xbb\xbf', (output / 'Install-Unified.ps1').read_bytes()[:3])


if __name__ == '__main__':
    unittest.main()
