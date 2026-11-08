import base64
import hashlib
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from lanpilot.broker import update_listeners
from lanpilot.core import Bridge
from lanpilot.enrollment import enroll


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bridge = Bridge(self.root / "bridge")
        self.key = self.public_key("key")

    def public_key(self, name):
        rsa = self.root / (name + ".pem")
        subprocess.run(["openssl", "genrsa", "-out", str(rsa), "2048"], check=True, capture_output=True)
        modulus = subprocess.check_output(["openssl", "rsa", "-in", str(rsa), "-modulus", "-noout"]).strip().split(b"=")[1]
        value = base64.b64encode(bytes.fromhex(modulus.decode())).decode()
        key = self.root / (name + ".xml")
        key.write_text(f"<RSAKeyValue><Modulus>{value}</Modulus><Exponent>AQAB</Exponent></RSAKeyValue>")

        return key

    def test_install_enrolls_and_listener_refresh_keeps_first_device(self):
        output = self.root / "kit.zip"
        one = enroll(self.bridge, "Office A", self.key, output, host="127.0.0.1")
        with zipfile.ZipFile(output) as archive:
            self.assertNotIn("server.key", archive.namelist())
            manifest = json.loads(archive.read("manifest.json"))
            for filename, checksum in manifest.items():
                self.assertEqual(hashlib.sha256(archive.read(filename)).hexdigest().upper(), checksum)
            opened = subprocess.check_output(["openssl", "pkeyutl", "-decrypt", "-inkey", str(self.root / "key.pem"),
                                              "-pkeyopt", "rsa_padding_mode:oaep"], input=archive.read("pairing.bin"))
            config = json.loads((Path(self.bridge.device(one["id"])["runtime"]) / "config.json").read_text())
            self.assertEqual(json.loads(opened)["token"], config["device"])
            self.assertNotIn("admin", json.loads(opened))
        with patch("lanpilot.broker.start_listener") as start:
            active = {}
            update_listeners(self.bridge, active)
            first = active[one["id"]]
            with self.assertRaisesRegex(ValueError, "已配对"):
                enroll(self.bridge, "Same Windows", self.key, self.root / "duplicate.zip", host="127.0.0.1")
            two = enroll(self.bridge, "Office B", self.public_key("second"), self.root / "kit-b.zip", host="127.0.0.1")
            self.assertNotEqual(one["port"], two["port"])
            update_listeners(self.bridge, active)
            self.assertIs(active[one["id"]], first)
            self.assertEqual(start.call_count, 2)

    def test_failed_export_leaves_no_device_or_partial_runtime(self):
        output = self.root / "kit.zip"
        output.write_text("existing")
        with self.assertRaises(FileExistsError):
            enroll(self.bridge, "Office A", self.key, output, host="127.0.0.1")
        self.assertEqual(output.read_text(), "existing")
        self.assertEqual(self.bridge.devices(), [])
        self.assertEqual(list((self.bridge.home / "devices").iterdir()), [])

if __name__ == "__main__":
    unittest.main()
