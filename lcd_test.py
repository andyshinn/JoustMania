#!/usr/bin/env python3
"""Standalone smoke test for the LCD KeyPad and UPS HATs.

Run this before touching JoustMania to confirm wiring, the I2C bus, and the
keypad jumper:

    ./lcd_test.py

It probes both HATs, walks the backlight through red/green/blue, draws a test
pattern, prints a UPS reading, and then echoes button presses until Ctrl-C.
"""

import logging
import sys
import time

import lcd_hat
import ups_hat

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')


def main():
    bus = lcd_hat.open_bus()
    if bus is None:
        print("Could not open the I2C bus.")
        print("Enable it with: sudo raspi-config nonint do_i2c 0   (then reboot)")
        return 1

    print("Probing the I2C bus...")
    lcd, keypad = lcd_hat.detect(bus)
    ups = ups_hat.detect(bus)

    if lcd is None:
        print("No LCD found at 0x%02x." % lcd_hat.LCD_ADDRESS)
        print("Check `sudo i2cdetect -y 1` -- expect 0x3e, 0x2d (or 0x60), 0x10.")
        return 1
    print("LCD found (backlight controller at 0x%02x)." % lcd._rgb_address)

    if ups is None:
        print("No UPS HAT at 0x%02x (fine if it is not fitted)." % ups_hat.UPS_ADDRESS)
    else:
        reading = ups.read()
        if reading:
            print("UPS: %.1f%%  %.2fV" % (reading['percent'],
                                          reading['millivolts'] / 1000.0))
        else:
            print("UPS found but the read failed.")

    print("Cycling the backlight: red, green, blue, white...")
    lcd.write_line(0, 'JoustMania')
    lcd.write_line(1, 'LCD self-test')
    for name, color in (('red', (255, 0, 0)), ('green', (0, 255, 0)),
                        ('blue', (0, 0, 255)), ('white', (255, 255, 255))):
        print("  %s" % name)
        lcd.set_rgb(*color)
        time.sleep(0.8)

    print("Drawing a width test (both rows should fill exactly)...")
    lcd.write_line(0, '0123456789ABCDEF')
    lcd.write_line(1, 'FEDCBA9876543210')
    time.sleep(1.5)

    if keypad is None:
        print("\nNo keypad (gpiozero missing or GPIO unavailable).")
        print("The display works, but buttons will not.")
        return 0

    print("\nPress the buttons; Ctrl-C to finish.")
    print("Nothing happening? The HAT's jumper must be installed for the")
    print("buttons to be wired to BCM %s." % sorted(lcd_hat.BUTTON_PINS.values()))
    lcd.write_line(0, 'Press a button')
    lcd.write_line(1, '')
    try:
        while True:
            for button in keypad.get_events():
                print("  %s" % button)
                lcd.write_line(1, button)
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\nDone.")
        lcd.clear()
        lcd.write_line(0, 'JoustMania')
        lcd.set_rgb(255, 255, 255)
    return 0


if __name__ == '__main__':
    sys.exit(main())
