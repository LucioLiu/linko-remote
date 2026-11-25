import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile

from lanpilot.build_unified import linux


@unittest.skipUnless(os.name == 'posix', 'Linux installer uses POSIX files and locking')
class LinuxInstallTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.data = self.root / 'user app data'
        self.profile = self.root / 'user config' / 'linko-remote'
        self.profile.mkdir(parents=True)
        (self.profile / 'client.json').write_text('{"name":"Preserve existing profile"}')
        self.env = dict(os.environ, XDG_DATA_HOME=str(self.data), XDG_CONFIG_HOME=str(self.profile.parent))

    def package(self, pin):
        output = self.root / (pin[0] + '.zip')
        endpoint = {'url': 'https://127.0.0.1:53000', 'pin': pin}
        linux(output, endpoint)
        extracted = self.root / pin[0]
        with zipfile.ZipFile(output) as archive:
            archive.extractall(extracted)
        return output, extracted / 'LinkoRemote-Linux'

    def run_install(self, package, check=True):
        return subprocess.run([sys.executable, str(package / 'install.py')], env=self.env,
                              capture_output=True, text=True, check=check)

    def test_archive_contains_only_linux_client_payload_and_checksums(self):
        output, package = self.package('a' * 64)
        with zipfile.ZipFile(output) as archive:
            names = set(archive.namelist())
            self.assertTrue(all(name.startswith('LinkoRemote-Linux/') for name in names))
            self.assertFalse(any('/windows/' in name or '/client.json' in name for name in names))
            for launcher in ('run.sh', 'install.py'):
                info = archive.getinfo('LinkoRemote-Linux/' + launcher)
                self.assertEqual(0o755, stat.S_IMODE(info.external_attr >> 16))
        manifest = json.loads((package / 'checksums.json').read_bytes())
        self.assertTrue(all(hashlib.sha256((package / name).read_bytes()).hexdigest() == expected
                            for name, expected in manifest.items()))
        subprocess.run(['sh', str(package / 'run.sh'), '--help'], env=self.env, cwd=self.root,
                       check=True, capture_output=True)

    def test_install_creates_a_launcher_and_keeps_the_prior_copy_and_profile(self):
        _, first = self.package('a' * 64)
        self.run_install(first)
        self.run_install(first)
        pointer = self.data / 'linko-remote/current'
        menu = self.data / 'applications/local.linko.remote.desktop'
        self.assertIn(f'Exec=/bin/sh "{pointer / "run.sh"}"\n', menu.read_text())
        original = pointer.resolve(strict=True)
        self.assertEqual([original], list(original.parent.iterdir()))
        subprocess.run([str(pointer / 'run.sh'), '--help'], env=self.env, cwd=self.root,
                       check=True, capture_output=True)

        _, second = self.package('b' * 64)
        self.run_install(second)
        self.assertNotEqual(original, pointer.resolve(strict=True))
        self.assertTrue((original / 'code/peer-endpoint.json').is_file())
        self.assertEqual({'name': 'Preserve existing profile'}, json.loads((self.profile / 'client.json').read_text()))

    def test_modified_package_cannot_replace_the_current_launcher(self):
        _, first = self.package('a' * 64)
        self.run_install(first)
        pointer = self.data / 'linko-remote/current'
        original = pointer.resolve(strict=True)
        _, second = self.package('b' * 64)
        (second / 'run.sh').write_text('changed')
        failed = self.run_install(second, check=False)
        self.assertNotEqual(0, failed.returncode)
        self.assertEqual(original, pointer.resolve(strict=True))

    def test_single_file_installer_creates_the_application_menu_entry(self):
        package = self.root / 'Linko Remote preview.pyz'
        linux(package, {'url': 'https://127.0.0.1:53000', 'pin': 'a' * 64})
        subprocess.run([sys.executable, str(package)], env=self.env, check=True, capture_output=True)
        self.assertTrue((self.data / 'applications/local.linko.remote.desktop').is_file())
        self.assertTrue((self.data / 'linko-remote/current/code/peer-endpoint.json').is_file())
        self.assertEqual({'name': 'Preserve existing profile'}, json.loads((self.profile / 'client.json').read_text()))


if __name__ == '__main__':
    unittest.main()
