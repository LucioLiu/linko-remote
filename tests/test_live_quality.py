import unittest

from lanpilot.live import AVC_PROFILE_HIGH, AVC_PROFILE_LOW
from lanpilot.live_view import AvcQuality


class AvcQualityTests(unittest.TestCase):
    def test_sustained_slow_wire_downgrades_and_fast_wire_restores_with_hysteresis(self):
        quality = AvcQuality()
        for now in (1, 2):
            quality.observe(now, 32_000, .16)
        self.assertEqual(quality.profile, AVC_PROFILE_HIGH)
        quality.observe(3, 32_000, .16)
        self.assertEqual(quality.profile, AVC_PROFILE_LOW)
        for now in range(4, 15):
            quality.observe(now, 16_000, .008)
            self.assertEqual(quality.profile, AVC_PROFILE_LOW)
        quality.observe(15, 16_000, .008)
        self.assertEqual(quality.profile, AVC_PROFILE_HIGH)
        for now in (16, 17, 18):
            quality.observe(now, 32_000, .16)
        self.assertEqual(quality.profile, AVC_PROFILE_HIGH)

    def test_tiny_idle_and_isolated_slow_samples_do_not_switch(self):
        quality = AvcQuality()
        for now in range(12):
            quality.observe(now, 2048, .6)
        for now in (12, 18, 26):
            quality.observe(now, 32_000, .16)
        for now in (27, 28, 29):
            quality.observe(now, 32_000, .015)
        self.assertEqual(quality.profile, AVC_PROFILE_HIGH)
