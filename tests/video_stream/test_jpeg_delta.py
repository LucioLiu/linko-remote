import socket
import unittest
from struct import Struct

import numpy as np

from jpeg_delta import FRAME, STRIP, Jpeg, MissingBase, Receiver, Sender, full_packet


class JpegDeltaTest(unittest.TestCase):
    def setUp(self):
        self.sender = Sender(Jpeg(), key_interval=4)
        self.receiver = Receiver(Jpeg())
        self.baseline_encoder = Jpeg()
        self.baseline_receiver = Receiver(Jpeg())
        rng = np.random.default_rng(19)
        self.rgb = rng.integers(0, 256, (273, 401, 3), dtype=np.uint8)

    def assert_matches_full(self, packet, rgb):
        _, sequence, _, _, _ = FRAME.unpack_from(packet)
        expected = self.baseline_receiver.receive(full_packet(self.baseline_encoder, rgb, sequence))
        self.assertTrue(np.array_equal(self.receiver.receive(packet), expected))

    def test_merged_adjacent_tiles_and_non_aligned_edges(self):
        self.assert_matches_full(self.sender.send(self.rgb), self.rgb)
        self.rgb[-5:, -150:, :] = (18, 111, 204)
        packet = self.sender.send(self.rgb)
        self.assertEqual(FRAME.unpack_from(packet), (b"D", 2, 1, 401, 273))
        x, y, width, size = STRIP.unpack_from(packet, FRAME.size)
        self.assertEqual((x, y, width), (128, 256, 273))
        self.assertEqual(len(packet), FRAME.size + STRIP.size + size)
        self.assert_matches_full(packet, self.rgb)

    def test_unchanged_frame_uses_only_header_and_periodic_full_frame(self):
        self.assert_matches_full(self.sender.send(self.rgb), self.rgb)
        first = self.receiver.frame
        for _ in range(3):
            packet = self.sender.send(self.rgb)
            self.assertEqual(len(packet), FRAME.size)
            self.assertIs(self.receiver.receive(packet), first)
        self.assertEqual(self.receiver.sequence, 4)
        packet = self.sender.send(self.rgb)
        self.assertEqual(packet[:1], b"F")
        self.assert_matches_full(packet, self.rgb)

    def test_skipped_frame_needs_new_full_frame(self):
        self.assert_matches_full(self.sender.send(self.rgb), self.rgb)
        self.rgb[10, 15] = 17
        self.sender.send(self.rgb)
        self.rgb[20, 25] = 43
        with self.assertRaises(MissingBase):
            self.receiver.receive(self.sender.send(self.rgb))
        self.assertEqual(self.receiver.sequence, 1)
        self.assert_matches_full(self.sender.send(self.rgb, force_full=True), self.rgb)

    def test_binary_packets_across_socket_stream(self):
        length = Struct("!I")
        rgb = np.full((256, 384, 3), 210, dtype=np.uint8)
        outgoing, incoming = socket.socketpair()
        with outgoing, incoming, incoming.makefile("rb") as stream:
            outgoing.settimeout(2)
            incoming.settimeout(2)
            for index, color in enumerate(((210, 210, 210), (40, 60, 110), (208, 78, 63))):
                rgb[10 + index:15 + index, 20:45] = color
                packet = self.sender.send(rgb)
                outgoing.sendall(length.pack(len(packet)) + packet)
                size, = length.unpack(stream.read(length.size))
                received = stream.read(size)
                self.assertEqual(received[:1], b"F" if index == 0 else b"D")
                self.assert_matches_full(received, rgb)

    def test_invalid_strip_keeps_frame_and_sequence(self):
        self.assert_matches_full(self.sender.send(self.rgb), self.rgb)
        first = self.receiver.frame
        self.rgb[10, 15] = 57
        packet = self.sender.send(self.rgb)
        for invalid in (packet[:-1], packet + packet[FRAME.size:]):
            with self.assertRaises(ValueError):
                self.receiver.receive(invalid)
            self.assertIs(self.receiver.frame, first)
            self.assertEqual(self.receiver.sequence, 1)
        self.assert_matches_full(packet, self.rgb)

    def test_dense_motion_and_resize_send_full_frame(self):
        self.assert_matches_full(self.sender.send(self.rgb), self.rgb)
        for rgb in (255 - self.rgb, self.rgb[:240, :360]):
            packet = self.sender.send(rgb)
            self.assertEqual(packet[:1], b"F")
            self.assert_matches_full(packet, rgb)


if __name__ == "__main__":
    unittest.main()
