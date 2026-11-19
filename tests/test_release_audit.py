import io
import json
import unittest
import zipfile

from lanpilot.release_audit import ReleaseAudit


class ReleaseAuditTests(unittest.TestCase):
    def test_nested_zip_and_utf16_private_values_are_checked(self):
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, 'w') as archive:
            archive.writestr('settings.txt', 'private-test-machine'.encode('utf-16le'))
        audit = ReleaseAudit(['private-test-machine'])
        audit.inspect('download.zip', nested.getvalue())
        self.assertTrue(any(x['file'] == 'download.zip!/settings.txt'
                            and x['category'] == 'private-name-id-or-path'
                            for x in audit.findings))

    def test_runtime_profiles_and_extra_endpoint_fields_cannot_ship(self):
        audit = ReleaseAudit()
        audit.inspect('code/client.json', b'{}')
        audit.inspect('code/peer-endpoint.json', json.dumps({'url': 'https://example.test',
                      'pin': 'a' * 64, 'credential': 'private'}).encode())
        self.assertEqual({'private-runtime-file', 'non-public-endpoint-fields'},
                         {x['category'] for x in audit.findings})
        clean = ReleaseAudit()
        clean.inspect('code/peer-endpoint.json', json.dumps({'url': 'https://example.test',
                      'pin': 'a' * 64}).encode())
        self.assertEqual([], clean.findings)
