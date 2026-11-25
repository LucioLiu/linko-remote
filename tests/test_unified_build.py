import hashlib
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
import zipfile

from unittest.mock import patch

from lanpilot.build_unified import mac, windows, copy_code, CLIENT_FILES, ROOT, WINDOWS_AVC


class UnifiedBuildTests(unittest.TestCase):
    def test_mac_links_both_swift_targets_for_the_minimum_version_in_the_plist(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            engine = root / 'LinkoService'
            (engine / 'ThirdParty').mkdir(parents=True)
            (engine / 'LinkoService').touch()
            output = root / 'Fixture.app'
            endpoint = {'url': 'https://127.0.0.1:1', 'pin': 'a' * 64}
            with patch('platform.machine', return_value='arm64'), \
                 patch('lanpilot.build_unified.subprocess.run') as run:
                mac(output, endpoint, engine)
            minimum = plistlib.loads((output / 'Contents/Info.plist').read_bytes())['LSMinimumSystemVersion']
            self.assertEqual('14.0', minimum)
            commands = [call.args[0] for call in run.call_args_list if call.args[0][0] == 'swiftc']
            self.assertEqual(2, len(commands))
            for command in commands:
                self.assertIn('-target', command)
                self.assertEqual('arm64-apple-macosx' + minimum, command[command.index('-target') + 1])

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

    def test_optional_windows_avc_helper_is_packaged_and_hashed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / 'python.zip'
            with zipfile.ZipFile(runtime, 'w') as archive:
                archive.writestr('python315._pth', 'python315.zip')
            output = root / 'package'
            helper = root / 'prebuilt-x64.exe'
            helper.write_bytes(b'fixture executable')
            checksum = hashlib.sha256(runtime.read_bytes()).hexdigest()
            endpoint = {'url': 'https://example.test', 'pin': 'a' * 64}
            windows(output, endpoint, runtime, checksum, helper)
            name = 'code/' + WINDOWS_AVC
            manifest = json.loads((output / 'checksums.json').read_text())
            self.assertEqual(helper.read_bytes(), (output / name).read_bytes())
            self.assertEqual(hashlib.sha256(helper.read_bytes()).hexdigest(), manifest[name])
            self.assertIn('code/windows/video/LiveAvc.cs', manifest)
            windows(output, endpoint, runtime, checksum)
            self.assertFalse((output / name).exists())
            self.assertNotIn(name, json.loads((output / 'checksums.json').read_text()))

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

    def test_portable_build_uses_a_gui_launcher_without_the_installer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / 'python.zip'
            with zipfile.ZipFile(runtime, 'w') as archive:
                archive.writestr('python315._pth', 'python315.zip')
            output = root / '邻控 试用'
            endpoint = {'url': 'https://example.test', 'pin': 'a' * 64}
            pin = hashlib.sha256(runtime.read_bytes()).hexdigest()

            def compile_launcher(command, **options):
                self.assertTrue(options['check'])
                self.assertIn('-mwindows', command)
                self.assertIn(str(ROOT / 'windows/PortableLauncher.c'), command)
                Path(command[command.index('-o') + 1]).write_bytes(b'fixture launcher')

            with patch('lanpilot.build_unified.subprocess.run', side_effect=compile_launcher):
                windows(output, endpoint, runtime, pin, portable=True)
            manifest = json.loads((output / 'checksums.json').read_text())
            self.assertIn('打开邻控试用版.exe', manifest)
            self.assertIn('code/windows/Start-Desktop.ps1', manifest)
            self.assertNotIn('Install-Unified.ps1', manifest)
            self.assertNotIn('打开邻控.cmd', manifest)
            with self.assertRaises(FileExistsError):
                windows(output, endpoint, runtime, pin, portable=True)
            with self.assertRaisesRegex(ValueError, 'portable output'):
                windows(output, endpoint, runtime, pin)


if __name__ == '__main__':
    unittest.main()
