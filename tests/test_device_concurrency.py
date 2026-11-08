import io
import json
import threading
import unittest
from unittest.mock import patch

from lanpilot import mcp


class MultiDeviceTests(unittest.TestCase):
    def test_independent_screens_progress_and_same_device_keeps_order(self):
        entered = threading.Event()
        released = threading.Event()
        finished = []

        class TestBridge:
            def device(self, name):
                return {"id": name}

        def observe(bridge, name, args):
            number = args["number"]
            if number == 1:
                entered.set()
                if not released.wait(3):
                    raise RuntimeError("Another device was blocked")
            if number == 3:
                if not entered.wait(3):
                    raise RuntimeError("First device did not start")
                released.set()
            finished.append(number)
            return {"ok": True}

        messages = []
        for number, device in enumerate(["A", "A", "B"], 1):
            params = {"name": "observe", "arguments": {"device": device, "number": number}}
            messages.append(json.dumps({"id": number, "method": "tools/call", "params": params}))
        output = io.StringIO()
        with patch.object(mcp, "Bridge", TestBridge), patch.object(mcp, "invoke", observe), \
                patch("sys.stdin", io.StringIO("\n".join(messages))), patch("sys.stdout", output):
            mcp.main()
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual({reply["id"] for reply in replies}, {1, 2, 3})
        self.assertTrue(all(reply.get("result") == {"ok": True} for reply in replies))
        self.assertLess(finished.index(1), finished.index(2))
