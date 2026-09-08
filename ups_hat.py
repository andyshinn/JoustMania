"""Driver and low-battery policy for the DFRobot DFR0494 UPS HAT.

The board carries a MAX17043 fuel gauge, but it is *not* exposed at the
MAX17043's native 0x36 -- an on-board MCU fronts it at 0x10 and mirrors the
gauge registers:

    0x03/0x04  VCELL   millivolts = ((hi & 0x0F) << 8 | lo) * 1.25
    0x05/0x06  SOC     percent    =  (hi << 8 | lo) * 0.003906

Reference: https://wiki.dfrobot.com/dfr0494/docs/19867
"""

import logging
from collections import deque

logger = logging.getLogger(__name__)

UPS_ADDRESS = 0x10

REG_VCELL_HI = 0x03
REG_VCELL_LO = 0x04
REG_SOC_HI = 0x05
REG_SOC_LO = 0x06

VCELL_STEP_MV = 1.25
SOC_STEP_PCT = 0.003906


def decode_voltage(hi, lo):
    """VCELL is a 12-bit value; the top nibble of the high byte is reserved."""
    return ((hi & 0x0f) << 8 | lo) * VCELL_STEP_MV


def decode_percent(hi, lo):
    return (hi << 8 | lo) * SOC_STEP_PCT


class UpsHat:
    def __init__(self, bus, address=UPS_ADDRESS):
        self._bus = bus
        self._address = address

    def read(self):
        """Return {'percent', 'millivolts'}, or None if the read failed."""
        try:
            vcell_hi = self._bus.read_byte_data(self._address, REG_VCELL_HI)
            vcell_lo = self._bus.read_byte_data(self._address, REG_VCELL_LO)
            soc_hi = self._bus.read_byte_data(self._address, REG_SOC_HI)
            soc_lo = self._bus.read_byte_data(self._address, REG_SOC_LO)
        except (OSError, IOError) as exc:
            logger.debug("UPS read failed: %s", exc)
            return None
        return {
            'percent': min(100.0, max(0.0, decode_percent(soc_hi, soc_lo))),
            'millivolts': decode_voltage(vcell_hi, vcell_lo),
        }


def detect(bus=None):
    """Return a UpsHat, or None if the HAT is absent. Never raises."""
    if bus is None:
        import lcd_hat
        bus = lcd_hat.open_bus()
    if bus is None:
        return None
    try:
        bus.read_byte(UPS_ADDRESS)
    except (OSError, IOError) as exc:
        logger.info("No UPS HAT at 0x%02x: %s", UPS_ADDRESS, exc)
        return None
    except Exception:
        logger.exception("UPS probe failed")
        return None
    logger.info("UPS HAT found at 0x%02x", UPS_ADDRESS)
    return UpsHat(bus)


class BatteryPolicy:
    """Decides OK / WARN / CRITICAL from a series of state-of-charge readings.

    Pure logic, no I/O, so the thresholds are unit-testable without hardware.

    The MAX17043's SoC output is noisy, and CRITICAL triggers a shutdown, so a
    level change requires `consecutive` readings in agreement -- one spurious
    sample must never power the machine off mid-party. Recovery uses a wider
    band than the trigger so a battery hovering on a threshold does not flap.
    """

    OK = 'ok'
    WARN = 'warn'
    CRITICAL = 'critical'

    HYSTERESIS_PCT = 3.0
    WINDOW = 5

    def __init__(self, warn_percent=20, critical_percent=5, consecutive=3):
        self.warn_percent = float(warn_percent)
        self.critical_percent = float(critical_percent)
        self.consecutive = max(1, int(consecutive))
        self.level = self.OK
        self._candidate = None
        self._candidate_count = 0
        self._readings = deque(maxlen=self.WINDOW)

    def _classify(self, percent):
        """Map a percentage to a level, biased toward staying where we are."""
        warn = self.warn_percent
        critical = self.critical_percent
        # Recovering out of a level takes a few extra points, so a battery
        # sitting exactly on a threshold doesn't oscillate.
        if self.level == self.CRITICAL:
            critical += self.HYSTERESIS_PCT
        if self.level in (self.WARN, self.CRITICAL):
            warn += self.HYSTERESIS_PCT

        if percent <= critical:
            return self.CRITICAL
        if percent <= warn:
            return self.WARN
        return self.OK

    def update(self, reading):
        """Feed one reading. Returns the current level."""
        if not reading:
            return self.level
        percent = reading.get('percent')
        if percent is None:
            return self.level

        self._readings.append(reading)
        candidate = self._classify(percent)

        if candidate == self.level:
            self._candidate = None
            self._candidate_count = 0
            return self.level

        if candidate == self._candidate:
            self._candidate_count += 1
        else:
            self._candidate = candidate
            self._candidate_count = 1

        if self._candidate_count >= self.consecutive:
            logger.info("Battery level %s -> %s (%.1f%%)",
                        self.level, candidate, percent)
            self.level = candidate
            self._candidate = None
            self._candidate_count = 0
        return self.level

    def trend(self):
        """'charging', 'discharging' or 'steady', from the voltage trend.

        The HAT exposes no charge-status line, so this is inferred. Voltage is
        used rather than SoC because it moves first and monotonically.
        """
        if len(self._readings) < self.WINDOW:
            return 'steady'
        first = self._readings[0]['millivolts']
        last = self._readings[-1]['millivolts']
        delta = last - first
        if delta > 10:
            return 'charging'
        if delta < -10:
            return 'discharging'
        return 'steady'
