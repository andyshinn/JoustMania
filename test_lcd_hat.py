"""Tests for the DFR0514 LCD driver, against a fake I2C bus."""

import unittest

import lcd_hat
from lcd_hat import (CTRL_COMMAND, CTRL_DATA, LCD_ADDRESS, RGB_ADDRESS_NEW,
                     RGB_ADDRESS_OLD, Keypad, RgbLcd)


class FakeBus:
    """Records every write so tests can assert the exact byte sequence."""

    def __init__(self, missing_addresses=()):
        self.writes = []
        self.missing = set(missing_addresses)

    def write_i2c_block_data(self, address, register, data):
        if address in self.missing:
            raise OSError("no device at 0x%02x" % address)
        self.writes.append((address, register, list(data)))

    def read_byte(self, address):
        if address in self.missing:
            raise OSError("no device at 0x%02x" % address)
        return 0

    def commands(self):
        return [d[0] for a, r, d in self.writes
                if a == LCD_ADDRESS and r == CTRL_COMMAND]

    def text(self):
        return ''.join(chr(d[0]) for a, r, d in self.writes
                       if a == LCD_ADDRESS and r == CTRL_DATA)

    def rgb_writes(self, address=RGB_ADDRESS_NEW):
        return [(r, d[0]) for a, r, d in self.writes if a == address]


class FakeButton:
    def __init__(self):
        self.is_pressed = False
        self.closed = False

    def close(self):
        self.closed = True


class InitTest(unittest.TestCase):
    def test_init_sequence_matches_the_hd44780_reference(self):
        bus = FakeBus()
        RgbLcd(bus)
        commands = bus.commands()
        # Four function-set writes (2-line, 5x8), then display on, clear,
        # entry mode -- the sequence the DFRobot reference driver sends.
        self.assertEqual(commands[:4], [0x28] * 4)
        self.assertEqual(commands[4], 0x0c)   # display on, cursor/blink off
        self.assertEqual(commands[5], 0x01)   # clear
        self.assertEqual(commands[6], 0x06)   # entry mode, left to right

    def test_init_configures_the_backlight_controller(self):
        bus = FakeBus()
        RgbLcd(bus)
        regs = dict(bus.rgb_writes())
        self.assertEqual(regs[lcd_hat.REG_MODE1], 0x00)
        self.assertEqual(regs[lcd_hat.REG_OUTPUT], 0xff)
        self.assertEqual(regs[lcd_hat.REG_MODE2], 0x20)

    def test_falls_back_to_the_old_rgb_address(self):
        """V1 boards answer at 0x60 instead of 0x2d."""
        bus = FakeBus(missing_addresses=[RGB_ADDRESS_NEW])
        lcd = RgbLcd(bus)
        self.assertEqual(lcd._rgb_address, RGB_ADDRESS_OLD)
        self.assertTrue(bus.rgb_writes(RGB_ADDRESS_OLD))


class TextTest(unittest.TestCase):
    def setUp(self):
        self.bus = FakeBus()
        self.lcd = RgbLcd(self.bus)
        self.bus.writes.clear()

    def test_write_line_pads_to_the_panel_width(self):
        """Padding is what erases the tail of the previous frame."""
        self.lcd.write_line(0, 'hi')
        self.assertEqual(self.bus.text(), 'hi' + ' ' * 14)

    def test_write_line_truncates_overlong_text(self):
        self.lcd.write_line(0, 'x' * 40)
        self.assertEqual(self.bus.text(), 'x' * 16)

    def test_row_addressing(self):
        self.lcd.write_line(0, '')
        self.assertEqual(self.bus.commands()[0], 0x80)
        self.bus.writes.clear()
        self.lcd.write_line(1, '')
        self.assertEqual(self.bus.commands()[0], 0xc0)

    def test_cursor_pos_is_clamped_to_the_panel(self):
        self.lcd.cursor_pos = (9, 99)
        self.assertEqual(self.lcd.cursor_pos, (1, 15))

    def test_create_char_writes_eight_masked_rows(self):
        self.lcd.create_char(2, [0xff] * 8)
        self.assertEqual(self.bus.commands()[0], 0x40 | (2 << 3))
        data = [d[0] for a, r, d in self.bus.writes if r == CTRL_DATA]
        self.assertEqual(data, [0x1f] * 8)   # only 5 bits per row are valid


class BacklightTest(unittest.TestCase):
    def setUp(self):
        self.bus = FakeBus()
        self.lcd = RgbLcd(self.bus)
        self.bus.writes.clear()

    def test_set_rgb_writes_all_three_channels(self):
        self.lcd.set_rgb(10, 20, 30)
        self.assertEqual(dict(self.bus.rgb_writes()), {
            lcd_hat.REG_RED: 10,
            lcd_hat.REG_GREEN: 20,
            lcd_hat.REG_BLUE: 30,
        })

    def test_repeated_color_is_not_rewritten(self):
        """The loop calls this every tick; only changes should hit the bus."""
        self.lcd.set_rgb(10, 20, 30)
        self.bus.writes.clear()
        self.lcd.set_rgb(10, 20, 30)
        self.assertEqual(self.bus.writes, [])

    def test_blink_period_and_duty(self):
        self.lcd.blink(period_secs=1.0, duty=0.5)
        regs = dict(self.bus.rgb_writes())
        self.assertEqual(regs[lcd_hat.REG_GRPFREQ], 23)   # (23+1)/24 == 1s
        self.assertEqual(regs[lcd_hat.REG_GRPPWM], 128)


class KeypadTest(unittest.TestCase):
    def setUp(self):
        self.buttons = {name: FakeButton() for name in lcd_hat.BUTTON_PINS}
        self.keypad = Keypad(self.buttons)

    def test_press_reports_once_until_released(self):
        self.buttons['SELECT'].is_pressed = True
        self.assertEqual(self.keypad.get_events(now=0), ['SELECT'])
        self.assertEqual(self.keypad.get_events(now=0.1), [])
        self.buttons['SELECT'].is_pressed = False
        self.assertEqual(self.keypad.get_events(now=0.2), [])
        self.buttons['SELECT'].is_pressed = True
        self.assertEqual(self.keypad.get_events(now=0.3), ['SELECT'])

    def test_up_and_down_auto_repeat_when_held(self):
        self.buttons['UP'].is_pressed = True
        self.assertEqual(self.keypad.get_events(now=0), ['UP'])
        self.assertEqual(self.keypad.get_events(now=0.2), [])
        # Repeat only starts after the initial delay.
        self.assertEqual(self.keypad.get_events(now=0.5), ['UP'])
        self.assertEqual(self.keypad.get_events(now=0.63), ['UP'])

    def test_select_does_not_auto_repeat(self):
        """Holding Select must not fire a command over and over."""
        self.buttons['SELECT'].is_pressed = True
        self.keypad.get_events(now=0)
        self.assertEqual(self.keypad.get_events(now=10), [])

    def test_pins_match_the_hat_silkscreen(self):
        self.assertEqual(lcd_hat.BUTTON_PINS,
                         {'SELECT': 16, 'UP': 17, 'DOWN': 18,
                          'LEFT': 19, 'RIGHT': 20})


class DetectTest(unittest.TestCase):
    def test_no_display_returns_nothing(self):
        lcd, keypad = lcd_hat.detect(FakeBus(missing_addresses=[LCD_ADDRESS]))
        self.assertIsNone(lcd)
        self.assertIsNone(keypad)

    def test_missing_bus_is_not_an_error(self):
        # Patched rather than relying on the host lacking I2C, so this test
        # also passes on a Pi with the HAT attached.
        original = lcd_hat.open_bus
        lcd_hat.open_bus = lambda *a, **k: None
        try:
            self.assertEqual(lcd_hat.detect(None), (None, None))
        finally:
            lcd_hat.open_bus = original


if __name__ == '__main__':
    unittest.main()
