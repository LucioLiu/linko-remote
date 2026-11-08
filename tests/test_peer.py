import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lanpilot import peer


class PeerExportTest(unittest.TestCase):
    def test_device_specific_export_survives_restarts_and_keeps_controller_identity_private(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(peer, "PEER", Path(temp)):
            root = Path(temp)
            devices = []
            for ident in ("a", "b"):
                runtime = root / ident
                runtime.mkdir()
                config = {"pin": ident * 64}
                (runtime / "config.json").write_text(json.dumps(config))
                (root / (ident + ".log")).write_text(
                    f"To connect, use e.g.:\ndumbpipe connect-tcp endpointcc\n"
                    f"To connect, use e.g.:\ndumbpipe connect-tcp endpoint{ident}{ident}\n"
                )
                identity = peer.profile(ident)
                self.assertEqual(identity, peer.profile(ident))
                out = root / (ident + ".ps1")
                device = dict(id=ident, runtime=str(runtime))
                peer.export_windows(device, out)
                content = out.read_text(encoding="utf-8-sig")
                self.assertIn("endpoint" + ident * 2, content)
                self.assertIn(config["pin"], content)
                self.assertIn(identity["alpn"], content)
                self.assertNotIn(identity["secret"], content)
                self.assertNotIn("endpointcc", content)
                self.assertEqual(out.stat().st_mode & 0o777, 0o600)
                with self.assertRaises(FileExistsError):
                    peer.export_windows(device, out)
                devices.append(identity)
            self.assertNotEqual(devices[0], devices[1])
