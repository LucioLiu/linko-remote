import hashlib
import io
import json
from pathlib import Path
import plistlib
import secrets
import subprocess
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from lanpilot.updater import (UpdateManager, select_release, version_key, asset_name,
                              safe_extract, check_url, replace_mac_app, apply_update, stop_mac_launcher)


def release(version='0.8.2-preview.1', data=b'valid-update', **changes):
    name = asset_name(version, 'windows', 'AMD64')
    value = {'tag_name': 'v' + version, 'draft': False, 'prerelease': '-' in version,
             'body': 'Update notes', 'assets': [{'name': name, 'size': len(data),
             'digest': 'sha256:' + hashlib.sha256(data).hexdigest(),
             'browser_download_url': 'https://github.com/LucioLiu/linko-remote/releases/download/v' + version + '/' + name}]}
    value.update(changes)
    return value


class UpdateTests(unittest.TestCase):
    def test_semantic_order_preview_channel_and_no_downgrade(self):
        self.assertLess(version_key('v0.8.1-preview.9'), version_key('0.8.1-preview.10'))
        self.assertLess(version_key('0.8.1-rc.1'), version_key('0.8.1'))
        self.assertIsNone(select_release([release('0.8.1-preview.1')], '0.8.1-preview.1', 'windows', 'AMD64'))
        self.assertIsNone(select_release([release('0.8.2-preview.1')], '0.8.1', 'windows', 'AMD64'))
        self.assertEqual('0.8.2', select_release([release('0.8.2'), release('0.8.3-preview.1', draft=True)],
                         '0.8.1-preview.1', 'windows', 'AMD64')['version'])

    def test_download_is_tied_to_exact_repository_asset_and_digest(self):
        value = release()
        value['assets'][0]['browser_download_url'] = 'https://github.com/other/project/installer.zip'
        with self.assertRaises(ValueError):
            select_release([value], '0.8.1-preview.1', 'windows', 'AMD64')
        value = release()
        value['assets'][0]['digest'] = None
        with self.assertRaises(ValueError):
            select_release([value], '0.8.1-preview.1', 'windows', 'AMD64')
        for url in ('http://github.com/a', 'https://evil.example/a', 'https://github.com@127.0.0.1/a',
                    'https://github.com:8443/a', 'https://token@github.com/a'):
            with self.assertRaises(ValueError):
                check_url(url)
        self.assertEqual('https://release-assets.githubusercontent.com/file?signature=value',
                         check_url('https://release-assets.githubusercontent.com/file?signature=value'))

    def test_check_download_checksum_and_recheck_before_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            def opener(url):
                return io.BytesIO(json.dumps([release()]).encode() if '/releases?' in url else b'valid-update')
            manager = UpdateManager(temporary, current='0.8.1-preview.1', system='windows', machine='AMD64', opener=opener)
            manager.check()
            self.assertEqual('available', manager.snapshot()['status'])
            manager.download()
            self.assertEqual('ready', manager.snapshot()['status'])
            manager.archive.write_bytes(b'tampered')
            with patch('lanpilot.updater.subprocess.Popen') as spawn:
                with self.assertRaisesRegex(ValueError, '修改'):
                    manager.prepare_install()
                spawn.assert_not_called()

    def test_corrupt_download_is_removed_without_touching_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            profile = home / 'client.json'
            profile.write_text('{"credential":"existing-private-identity","ssh":["saved"]}')
            before = profile.read_bytes()
            def opener(url):
                return io.BytesIO(json.dumps([release()]).encode() if '/releases?' in url else b'wrong-update')
            manager = UpdateManager(home, system='windows', machine='AMD64', opener=opener)
            manager.check()
            with self.assertRaises(ValueError):
                manager.download()
            self.assertEqual(before, profile.read_bytes())
            self.assertEqual([], list(manager.directory.glob('*.part')))
            self.assertIsNone(manager.archive)

    def test_interrupted_download_retries_and_revalidates_the_complete_archive(self):
        data = b'valid-update'

        class Interrupted(io.BytesIO):
            def read(self, _):
                part = super().read(3)
                if self.tell() > 3:
                    raise TimeoutError('read operation timed out')
                return part

        with tempfile.TemporaryDirectory() as temporary:
            replies = iter([io.BytesIO(json.dumps([release(data=data)]).encode()),
                            Interrupted(data), io.BytesIO(data)])
            manager = UpdateManager(temporary, current='0.8.1-preview.1', system='windows', machine='AMD64',
                                    opener=lambda _: next(replies))
            manager.check()
            manager.download()
            self.assertEqual('ready', manager.snapshot()['status'])
            self.assertEqual(data, manager.archive.read_bytes())

    def test_macos_update_can_stop_only_the_original_launcher(self):
        app = Path('/Applications/邻控.app')
        launcher = str(app / 'Contents/MacOS/LinkoRemote')
        with patch('lanpilot.updater.subprocess.run') as ps, patch('lanpilot.updater.os.kill') as kill, \
                patch('lanpilot.updater.wait_for_exit') as wait:
            ps.return_value = subprocess.CompletedProcess([], 0, stdout='/bin/other')
            with self.assertRaisesRegex(ValueError, '无法确认'):
                stop_mac_launcher(app, 178)
            kill.assert_not_called()
            ps.return_value.stdout = launcher + '\n'
            stop_mac_launcher(app, 178)
            self.assertEqual(178, kill.call_args.args[0])
            wait.assert_called_once_with(178)

    def test_update_waits_for_the_online_relay_to_reconnect(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            cache = home / 'updates'
            cache.mkdir()
            archive = cache / 'update.zip'
            archive.write_bytes(b'valid-update')
            version = '99.1.0-preview.1'
            spec = cache / 'install-test.json'
            spec.write_text(json.dumps({'home': str(home), 'archive': str(archive), 'system': 'windows',
                        'version': version, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                        'parent_pid': 12345, 'expect_relay': True}))
            states = [{'version': version, 'connected': False}, {'version': version, 'connected': True}]
            with patch('lanpilot.updater.wait_for_exit'), patch('lanpilot.updater.safe_extract', return_value=home), \
                 patch('lanpilot.updater.subprocess.run'), patch('lanpilot.updater.time.sleep'), \
                 patch('lanpilot.updater.local_state', side_effect=states) as state:
                self.assertTrue(apply_update(spec)['ok'])
            self.assertEqual(2, state.call_count)

            spec.write_text(json.dumps({'home': str(home), 'archive': str(archive), 'system': 'windows',
                        'version': version, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                        'parent_pid': 12345, 'expect_relay': True, 'previous_python': 'old-python'}))
            with patch('lanpilot.updater.wait_for_exit'), patch('lanpilot.updater.safe_extract', return_value=home), \
                 patch('lanpilot.updater.subprocess.run'), patch('lanpilot.updater.time.sleep'), \
                 patch('lanpilot.updater.time.monotonic', side_effect=[0, 1, 45]), \
                 patch('lanpilot.updater.local_state', return_value=states[0]), \
                 patch('lanpilot.updater.stop_updated_client') as stop, \
                 patch('lanpilot.updater.restore_windows_launcher') as restore:
                self.assertFalse(apply_update(spec)['ok'])
            stop.assert_called_once_with(home, version)
            restore.assert_called_once()

    def test_archive_traversal_link_and_duplicate_paths_are_rejected(self):
        bad_names = ['../outside', 'LinkoRemote-Unified/../outside', '/absolute',
                     'LinkoRemote-Unified/C:escape', 'LinkoRemote-Unified/.. /outside',
                     'LinkoRemote-Unified\\outside', 'OtherProduct/install.exe']
        for name in bad_names:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                archive = Path(temporary) / 'bad.zip'
                with zipfile.ZipFile(archive, 'w') as zipped:
                    zipped.writestr(name, b'bad')
                with self.assertRaises(ValueError):
                    safe_extract(archive, Path(temporary) / 'out', 'windows')
                self.assertFalse((Path(temporary) / 'out').exists())
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / 'bad.zip'
            with zipfile.ZipFile(archive, 'w') as zipped:
                entry = zipfile.ZipInfo('LinkoRemote.app/link')
                entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                zipped.writestr(entry, '/outside')
            with self.assertRaises(ValueError):
                safe_extract(archive, Path(temporary) / 'out', 'macos')
            with zipfile.ZipFile(archive, 'w') as zipped:
                zipped.writestr('LinkoRemote-Unified/File', b'one')
                zipped.writestr('LinkoRemote-Unified/file', b'two')
            with self.assertRaises(ValueError):
                safe_extract(archive, Path(temporary) / 'out', 'windows')

    def test_failed_mac_swap_restores_previous_app_and_preserves_user_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, target = root / 'new.app', root / 'installed.app'
            for app, content in [(source, 'new'), (target, 'old')]:
                (app / 'Contents').mkdir(parents=True)
                (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': 'local.linko.remote.unified.preview'}))
                (app / 'Contents/version').write_text(content)
            profile = root / 'client.json'
            profile.write_text('existing-profile')
            import os
            replace = os.replace
            def fail_second(a, b):
                if Path(a).name.startswith('.LinkoRemote-staged-'):
                    raise OSError('simulated filesystem failure')
                return replace(a, b)
            with patch('lanpilot.updater.subprocess.run'), patch('lanpilot.updater.os.replace', side_effect=fail_second):
                with self.assertRaises(OSError):
                    replace_mac_app(source, target)
            self.assertEqual('old', (target / 'Contents/version').read_text())
            self.assertEqual('existing-profile', profile.read_text())

    def test_helper_rejects_corrupt_archive_before_waiting_or_installing(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            cache = home / 'updates'
            cache.mkdir()
            archive = cache / 'update.zip'
            archive.write_bytes(b'corrupt')
            profile = home / 'client.json'
            profile.write_text('untouched')
            spec = cache / 'install-test.json'
            spec.write_text(json.dumps({'home': str(home), 'archive': str(archive), 'system': 'windows',
                        'version': '0.8.2-preview.1', 'sha256': 'a' * 64, 'parent_pid': 12345,
                        'previous_python': 'unused'}))
            with patch('lanpilot.updater.wait_for_exit') as wait, patch('lanpilot.updater.subprocess.Popen') as spawn:
                result = apply_update(spec)
            self.assertFalse(result['ok'])
            wait.assert_not_called()
            spawn.assert_not_called()
            self.assertEqual('untouched', profile.read_text())
            self.assertFalse(spec.exists())
            self.assertFalse(json.loads((cache / 'last-result.json').read_text())['ok'])
