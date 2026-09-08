"""Driver for the DFRobot DFR0514 "I2C 16x2 RGB LCD KeyPad HAT" (V1/V2).

Two independent devices live on this HAT:

  * An HD44780-compatible character LCD with a *native* I2C interface
    (AiP31068-class) at 0x3e. Unlike the common PCF8574 "backpack" displays,
    there is no port expander and no nibble/enable-pin clocking: bytes are sent
    whole, behind a control byte (0x80 = command, 0x40 = data). This is why
    RPLCD cannot drive this panel -- its I2C path only speaks
    PCF8574/MCP23008/MCP23017.
  * A PCA9633-class RGB backlight controller at 0x2d on newer boards, 0x60 on
    older ones. Probed at init.

The keypad is five plain GPIOs (BCM 16/17/18/19/20), active-high. Note the
on-board jumper must be installed for the buttons to be wired through.

Protocol reference: https://github.com/DFRobot/DFRobot_RGB1602_RaspberryPi
(python/rgb1602.py). Reimplemented rather than vendored: that code ships no
license, depends on RPi.GPIO (broken on Bookworm/Pi 5) and the apt-only smbus,
and its setBacklight() is broken (it toggles hardware blink, not the backlight).
Method naming here follows RPLCD's conventions so it reads familiarly.
"""

import logging
import time

logger = logging.getLogger(__name__)

LCD_ADDRESS = 0x3e
RGB_ADDRESS_NEW = 0x2d
RGB_ADDRESS_OLD = 0x60

# Control bytes prefixing every LCD write.
CTRL_COMMAND = 0x80
CTRL_DATA = 0x40

# HD44780 instruction set (only what we use).
LCD_CLEARDISPLAY = 0x01
LCD_RETURNHOME = 0x02
LCD_ENTRYMODESET = 0x04
LCD_DISPLAYCONTROL = 0x08
LCD_FUNCTIONSET = 0x20
LCD_SETCGRAMADDR = 0x40
LCD_SETDDRAMADDR = 0x80

LCD_ENTRYLEFT = 0x02
LCD_DISPLAYON = 0x04
LCD_CURSOROFF = 0x00
LCD_BLINKOFF = 0x00
LCD_2LINE = 0x08
LCD_5x8DOTS = 0x00
LCD_4BITMODE = 0x00
LCD_1LINE = 0x00

# PCA9633-class backlight registers.
REG_MODE1 = 0x00
REG_MODE2 = 0x01
REG_BLUE = 0x02
REG_GREEN = 0x03
REG_RED = 0x04
REG_GRPPWM = 0x06
REG_GRPFREQ = 0x07
REG_OUTPUT = 0x08

COLS = 16
ROWS = 2

# Row -> DDRAM base address.
_ROW_OFFSETS = (0x80, 0xc0)

# Buttons, by BCM pin. Active-high.
BUTTON_PINS = {
    'SELECT': 16,
    'UP': 17,
    'DOWN': 18,
    'LEFT': 19,
    'RIGHT': 20,
}


class RgbLcd:
    """16x2 character LCD with an RGB backlight, over I2C.

    `bus` must implement smbus2's write_i2c_block_data/read_byte. It is
    injectable so tests can pass a fake and assert the exact byte sequence.
    """

    def __init__(self, bus, cols=COLS, rows=ROWS):
        self._bus = bus
        self.cols = cols
        self.rows = rows
        self._rgb_address = RGB_ADDRESS_NEW
        self._last_rgb = None
        self._cursor_pos = (0, 0)
        self._init_display()

    # -- low level ---------------------------------------------------------

    def _command(self, value):
        self._bus.write_i2c_block_data(LCD_ADDRESS, CTRL_COMMAND, [value])

    def _write_byte(self, value):
        self._bus.write_i2c_block_data(LCD_ADDRESS, CTRL_DATA, [value])

    def _set_reg(self, reg, value):
        self._bus.write_i2c_block_data(self._rgb_address, reg, [value])

    def _detect_rgb_address(self):
        """Newer boards answer at 0x2d, older ones at 0x60."""
        try:
            self._bus.read_byte(RGB_ADDRESS_NEW)
            self._rgb_address = RGB_ADDRESS_NEW
        except OSError:
            self._rgb_address = RGB_ADDRESS_OLD
        logger.debug("LCD RGB controller at 0x%02x", self._rgb_address)

    def _init_display(self):
        self._detect_rgb_address()

        show_function = LCD_4BITMODE | LCD_5x8DOTS
        show_function |= LCD_2LINE if self.rows > 1 else LCD_1LINE

        # Per the HD44780 datasheet the controller needs >40ms after power
        # rises before it will accept commands, then the function set is sent
        # repeatedly to guarantee it latches.
        time.sleep(0.05)
        for _ in range(4):
            self._command(LCD_FUNCTIONSET | show_function)
            time.sleep(0.005)

        self._command(LCD_DISPLAYCONTROL | LCD_DISPLAYON | LCD_CURSOROFF | LCD_BLINKOFF)
        self.clear()
        self._command(LCD_ENTRYMODESET | LCD_ENTRYLEFT)

        # Backlight: normal mode, all outputs under PWM+GRPPWM control,
        # MODE2 group-dimming bit set so blink() below works.
        self._set_reg(REG_MODE1, 0x00)
        self._set_reg(REG_OUTPUT, 0xff)
        self._set_reg(REG_MODE2, 0x20)
        self.set_rgb(255, 255, 255)

    # -- text --------------------------------------------------------------

    def clear(self):
        self._command(LCD_CLEARDISPLAY)
        time.sleep(0.002)  # clear is slow; the controller NAKs if we rush it

    def home(self):
        self._command(LCD_RETURNHOME)
        time.sleep(0.002)

    @property
    def cursor_pos(self):
        return self._cursor_pos

    @cursor_pos.setter
    def cursor_pos(self, pos):
        row, col = pos
        row = max(0, min(row, self.rows - 1))
        col = max(0, min(col, self.cols - 1))
        self._cursor_pos = (row, col)
        self._command(_ROW_OFFSETS[row] | col)

    def write_string(self, text):
        for char in text:
            self._write_byte(ord(char))

    def write_line(self, row, text):
        """Draw one full row, padded to the panel width.

        Padding matters: the LCD has no concept of erasing, so a shorter string
        would leave the tail of the previous frame on screen.
        """
        self.cursor_pos = (row, 0)
        self.write_string(str(text)[:self.cols].ljust(self.cols))

    def create_char(self, slot, bitmap):
        """Define a custom glyph. 8 slots (0-7), 8 rows of 5 bits each."""
        self._command(LCD_SETCGRAMADDR | ((slot & 0x7) << 3))
        for row in bitmap:
            self._write_byte(row & 0x1f)

    # -- backlight ---------------------------------------------------------

    def set_rgb(self, r, g, b):
        rgb = (int(r) & 0xff, int(g) & 0xff, int(b) & 0xff)
        if rgb == self._last_rgb:
            return
        self._last_rgb = rgb
        self._set_reg(REG_RED, rgb[0])
        self._set_reg(REG_GREEN, rgb[1])
        self._set_reg(REG_BLUE, rgb[2])

    def blink(self, period_secs=1.0, duty=0.5):
        """Hardware-timed backlight blink.

        The controller runs this itself, so a pulsing winner flash or low
        battery warning costs no timer and no I2C traffic while it runs.
        Period is (GRPFREQ + 1) / 24 seconds; duty is GRPPWM / 256.
        """
        freq = max(0, min(int(round(period_secs * 24)) - 1, 0xff))
        self._set_reg(REG_GRPFREQ, freq)
        self._set_reg(REG_GRPPWM, max(0, min(int(duty * 256), 0xff)))

    def no_blink(self):
        self._set_reg(REG_GRPFREQ, 0x00)
        self._set_reg(REG_GRPPWM, 0xff)


class Keypad:
    """The HAT's five buttons, active-high on BCM 16-20.

    Uses gpiozero rather than the reference driver's raw RPi.GPIO polling:
    gpiozero ships on Raspberry Pi OS, works on both Pi 4 and Pi 5 (RPi.GPIO
    does not), and handles debouncing for us.

    Reminder: the on-board jumper must be installed or these pins read nothing.
    """

    # Held UP/DOWN repeat, so adjusting a number doesn't mean 20 presses.
    REPEAT_DELAY_SECS = 0.45
    REPEAT_INTERVAL_SECS = 0.12
    REPEATABLE = ('UP', 'DOWN')

    def __init__(self, buttons):
        self._buttons = buttons
        self._down_since = {}
        self._next_repeat = {}

    def get_events(self, now=None):
        """Return button names pressed since the last call, in a stable order."""
        now = time.time() if now is None else now
        events = []
        for name, button in self._buttons.items():
            pressed = bool(button.is_pressed)
            was_down = name in self._down_since
            if pressed and not was_down:
                self._down_since[name] = now
                self._next_repeat[name] = now + self.REPEAT_DELAY_SECS
                events.append(name)
            elif pressed and name in self.REPEATABLE and now >= self._next_repeat[name]:
                self._next_repeat[name] = now + self.REPEAT_INTERVAL_SECS
                events.append(name)
            elif not pressed and was_down:
                del self._down_since[name]
                del self._next_repeat[name]
        return events

    def close(self):
        for button in self._buttons.values():
            try:
                button.close()
            except Exception:
                pass


def open_bus(bus_number=1):
    """Open /dev/i2c-<n>, or None if smbus2 or the bus is unavailable."""
    try:
        import smbus2
    except ImportError:
        logger.info("smbus2 not installed; I2C HATs unavailable")
        return None
    try:
        return smbus2.SMBus(bus_number)
    except (OSError, IOError) as exc:
        logger.info("Could not open I2C bus %s: %s", bus_number, exc)
        return None


def detect(bus=None):
    """Return (RgbLcd, Keypad), either of which may be None.

    Never raises: a missing HAT, a missing library or a wedged bus all degrade
    to None so JoustMania runs normally on a Pi without the HAT, on Windows,
    and on the Steam Deck.
    """
    lcd = None
    keypad = None

    if bus is None:
        bus = open_bus()
    if bus is not None:
        try:
            bus.read_byte(LCD_ADDRESS)
            lcd = RgbLcd(bus)
            logger.info("LCD KeyPad HAT found at 0x%02x", LCD_ADDRESS)
        except (OSError, IOError) as exc:
            logger.info("No LCD at 0x%02x: %s", LCD_ADDRESS, exc)
        except Exception:
            logger.exception("LCD init failed")

    if lcd is not None:
        try:
            from gpiozero import Button
            buttons = {
                name: Button(pin, pull_up=False, bounce_time=0.02)
                for name, pin in BUTTON_PINS.items()
            }
            keypad = Keypad(buttons)
            logger.info("LCD keypad ready on BCM %s",
                        sorted(BUTTON_PINS.values()))
        except ImportError:
            logger.warning("gpiozero not installed; LCD buttons disabled")
        except Exception:
            logger.exception("LCD keypad init failed; display will be read-only")

    return lcd, keypad
