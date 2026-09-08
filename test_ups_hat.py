"""Tests for the DFR0494 UPS HAT driver and low-battery policy.

No hardware required: the I2C bus is faked and BatteryPolicy is pure logic.
"""

import unittest

import ups_hat
from ups_hat import BatteryPolicy, UpsHat, decode_percent, decode_voltage


class FakeBus:
    def __init__(self, registers=None, fail=False):
        self.registers = registers or {}
        self.fail = fail
        self.reads = []

    def read_byte_data(self, address, register):
        if self.fail:
            raise OSError("bus error")
        self.reads.append((address, register))
        return self.registers.get(register, 0)

    def read_byte(self, address):
        if self.fail:
            raise OSError("no device")
        return 0


class DecodeTest(unittest.TestCase):
    def test_voltage_masks_reserved_nibble(self):
        # Only the low nibble of the high byte is part of the 12-bit value.
        self.assertEqual(decode_voltage(0x00, 0x00), 0.0)
        self.assertEqual(decode_voltage(0x0c, 0x80), (0xc80) * 1.25)
        # The top nibble must be ignored, not shifted in.
        self.assertEqual(decode_voltage(0xfc, 0x80), decode_voltage(0x0c, 0x80))

    def test_percent(self):
        self.assertEqual(decode_percent(0x00, 0x00), 0.0)
        self.assertAlmostEqual(decode_percent(0x64, 0x00), 100.0, places=1)

    def test_read_returns_both_values(self):
        bus = FakeBus({0x03: 0x0c, 0x04: 0x80, 0x05: 0x64, 0x06: 0x00})
        reading = UpsHat(bus).read()
        self.assertAlmostEqual(reading['percent'], 100.0, places=1)
        self.assertAlmostEqual(reading['millivolts'], 4000.0, places=1)

    def test_percent_is_clamped(self):
        # The gauge can report slightly over 100% right off the charger.
        bus = FakeBus({0x05: 0xff, 0x06: 0xff})
        self.assertEqual(UpsHat(bus).read()['percent'], 100.0)

    def test_read_failure_returns_none(self):
        self.assertIsNone(UpsHat(FakeBus(fail=True)).read())

    def test_detect_returns_none_without_device(self):
        self.assertIsNone(ups_hat.detect(FakeBus(fail=True)))
        self.assertIsInstance(ups_hat.detect(FakeBus()), UpsHat)


def reading(percent, millivolts=3700):
    return {'percent': percent, 'millivolts': millivolts}


class BatteryPolicyTest(unittest.TestCase):
    def setUp(self):
        self.policy = BatteryPolicy(warn_percent=20, critical_percent=5,
                                    consecutive=3)

    def feed(self, percent, times=1, millivolts=3700):
        for _ in range(times):
            level = self.policy.update(reading(percent, millivolts))
        return level

    def test_starts_ok(self):
        self.assertEqual(self.policy.level, BatteryPolicy.OK)

    def test_requires_consecutive_readings_to_warn(self):
        self.assertEqual(self.feed(15), BatteryPolicy.OK)
        self.assertEqual(self.feed(15), BatteryPolicy.OK)
        self.assertEqual(self.feed(15), BatteryPolicy.WARN)

    def test_single_spurious_low_reading_does_not_trigger_shutdown(self):
        """The MAX17043's SoC output is noisy and CRITICAL powers the Pi off."""
        self.feed(50, times=5)
        self.assertEqual(self.feed(1), BatteryPolicy.OK)
        self.assertEqual(self.feed(50, times=3), BatteryPolicy.OK)

    def test_sustained_low_does_trigger_critical(self):
        self.assertEqual(self.feed(2, times=3), BatteryPolicy.CRITICAL)

    def test_interrupted_run_restarts_the_count(self):
        self.feed(2, times=2)
        self.feed(50)                      # breaks the streak
        self.assertEqual(self.feed(2, times=2), BatteryPolicy.OK)
        self.assertEqual(self.feed(2), BatteryPolicy.CRITICAL)

    def test_hysteresis_prevents_flapping_on_the_threshold(self):
        self.feed(15, times=3)
        self.assertEqual(self.policy.level, BatteryPolicy.WARN)
        # Just above the warn threshold is inside the hysteresis band, so we
        # stay in WARN rather than oscillating.
        self.assertEqual(self.feed(21, times=5), BatteryPolicy.WARN)
        self.assertEqual(self.feed(30, times=3), BatteryPolicy.OK)

    def test_recovers_from_critical(self):
        self.feed(2, times=3)
        self.assertEqual(self.policy.level, BatteryPolicy.CRITICAL)
        self.assertEqual(self.feed(50, times=3), BatteryPolicy.OK)

    def test_ignores_empty_readings(self):
        self.feed(50, times=3)
        self.assertEqual(self.policy.update(None), BatteryPolicy.OK)
        self.assertEqual(self.policy.update({}), BatteryPolicy.OK)


if __name__ == '__main__':
    unittest.main()
