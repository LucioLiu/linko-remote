"""Opt-in Windows smoke tests. Read the current interactive desktop into RAM."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import queue
import re
import struct
import subprocess
import sys
import threading
import unittest

HEADER = struct.Struct("<4sIQQHHIiiII")
COMMAND = struct.Struct("<cQ")
EXE = Path(sys.argv.pop(1)).resolve() if len(sys.argv) > 1 else None


def read_exact(stream, length):
    parts = bytearray()
    while len(parts) < length:
        chunk = stream.read(length - len(parts))
        if not chunk:
            raise EOFError("AVC pipe closed")
        parts.extend(chunk)
    return parts


@unittest.skipUnless(sys.platform == "win32" and EXE and EXE.is_file(),
                     "pass a built helper on an interactive Windows desktop")
class SenderTest(unittest.TestCase):
    def setUp(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel.GetTickCount64.restype = ctypes.c_uint64

    def start(self, duration, watched_pid=None):
        access = 0x00100000 | 0x1000  # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
        handle = self.kernel.OpenProcess(access, True, watched_pid or os.getpid())
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        startup = subprocess.STARTUPINFO(lpAttributeList={"handle_list": [handle]})
        try:
            child = subprocess.Popen(
                [str(EXE), str(handle), str(self.kernel.GetTickCount64() + duration),
                 "1280", "720", "20", "3500000"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                startupinfo=startup, bufsize=0, creationflags=subprocess.CREATE_NO_WINDOW,
            )
        finally:
            self.kernel.CloseHandle(handle)
        self.addCleanup(self.cleanup, child)
        return child

    @staticmethod
    def cleanup(child):
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        for stream in (child.stdin, child.stdout, child.stderr):
            stream.close()

    def frame(self, child):
        # Limit the wait even if the encoder never writes the first header.
        result = queue.Queue()

        def read():
            try:
                header = HEADER.unpack(read_exact(child.stdout, HEADER.size))
                if header[0] != b"LPA1" or not 0 < header[1] <= 2 * 1024 * 1024:
                    raise ValueError("invalid AVC header")
                result.put((header, read_exact(child.stdout, header[1])))
            except Exception as error:
                result.put(error)

        threading.Thread(target=read, daemon=True).start()
        frame = result.get(timeout=5)
        if isinstance(frame, Exception):
            diagnostic = child.stderr.read().decode(errors="replace") if child.poll() is not None else ""
            raise AssertionError(diagnostic) from frame
        return frame

    def idr(self, frame):
        header, payload = frame
        types = [match[0] & 31 for match in re.findall(rb"\x00\x00(?:\x00)?\x01(.)", payload, re.S)]
        self.assertEqual(header[6], 1)
        for nal_type in (5, 7, 8):
            self.assertIn(nal_type, types)

    def command(self, child, opcode, value=0):
        child.stdin.write(COMMAND.pack(opcode, value))

    def test_first_frame_renew_key_and_stop(self):
        child = self.start(4000)
        first = self.frame(child)
        header = first[0]
        self.assertEqual(header[2], 1)
        self.assertLessEqual(header[3], self.kernel.GetTickCount64())
        self.assertTrue(64 <= header[4] <= 1280 and header[4] % 2 == 0)
        self.assertTrue(64 <= header[5] <= 720 and header[5] % 2 == 0)
        self.assertGreaterEqual(header[9], header[4])
        self.assertGreaterEqual(header[10], header[5])
        self.idr(first)

        self.command(child, b"R", self.kernel.GetTickCount64() + 4000)
        self.command(child, b"K")
        for sequence in range(2, 12):
            frame = self.frame(child)
            self.assertEqual(frame[0][2], sequence)
            if frame[0][6]:
                self.idr(frame)
                break
        else:
            self.fail("key request produced no IDR")
        self.command(child, b"S")
        self.assertEqual(child.wait(timeout=2), 0)

    def test_switch_and_restore_profile_with_continuous_sequence_and_geometry(self):
        child = self.start(4000)
        prior = self.frame(child)[0]
        geometry = prior[4:6], prior[7:11]
        for profile in (2, 1):
            self.command(child, b"R", self.kernel.GetTickCount64() + 4000)
            self.command(child, b"Q", profile)
            for _ in range(15):  # shorter than the native two-second periodic GOP
                frame = self.frame(child)
                header = frame[0]
                self.assertEqual(header[2], prior[2] + 1)
                self.assertGreaterEqual(header[3], prior[3])
                self.assertEqual((header[4:6], header[7:11]), geometry)
                prior = header
                if header[6]:
                    self.idr(frame)
                    break
            else:
                self.fail(f"profile {profile} produced no immediate IDR")
        self.command(child, b"S")
        self.assertEqual(child.wait(timeout=2), 0)

    def test_invalid_profile_command_fails_closed(self):
        child = self.start(4000)
        self.frame(child)
        self.command(child, b"Q", 750000)
        self.assertEqual(child.wait(timeout=2), 4)

    def test_already_expired(self):
        child = self.start(0)
        self.assertEqual(child.wait(timeout=2), 2)
        self.assertEqual(child.stdout.read(), b"")

    def test_expiry_with_stdout_unread(self):
        child = self.start(4000)
        self.frame(child)
        self.assertEqual(child.wait(timeout=6), 2)

    def test_watched_process_exit(self):
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(worker.wait, 5)
        self.addCleanup(lambda: worker.kill() if worker.poll() is None else None)
        child = self.start(4000, watched_pid=worker.pid)
        self.frame(child)
        # Keep the command pipe open to isolate the process-handle signal.
        worker.kill()
        self.assertEqual(child.wait(timeout=2), 3)

    def test_command_pipe_closed(self):
        child = self.start(4000)
        self.frame(child)
        child.stdin.close()
        self.assertEqual(child.wait(timeout=2), 4)


if __name__ == "__main__":
    unittest.main()
