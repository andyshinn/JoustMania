#!/usr/bin/env python3
"""Standalone check for the captive portal / network settings prerequisites.

Run this on the Pi when the LCD says the portal is unavailable:

    sudo ~/JoustMania/venv/bin/python ~/JoustMania/network_test.py

It reports each requirement separately, because "unavailable" on the display
cannot say which one is missing.
"""

import os
import shutil
import subprocess
import sys


def check(label, ok, detail=''):
    print('{:<34} {}{}'.format(label, 'OK' if ok else 'FAILED',
                               '  ' + detail if detail else ''))
    return ok


def main():
    print('JoustMania network prerequisites\n' + '-' * 52)
    ok = True

    ok &= check('running as root', os.geteuid() == 0,
                '' if os.geteuid() == 0 else '(re-run with sudo)')

    binary = shutil.which('nmcli')
    ok &= check('nmcli binary on PATH', binary is not None, binary or
                '(install network-manager)')

    try:
        import nmcli
        package = True
        detail = getattr(nmcli, '__file__', '')
    except Exception as exc:
        package = False
        detail = '({}) -- run: sudo ./setup.sh'.format(exc)
    ok &= check('nmcli python package', package, detail)

    if binary:
        try:
            result = subprocess.run(['nmcli', '-t', '-f', 'RUNNING', 'general'],
                                    capture_output=True, timeout=10)
            running = result.stdout.decode(errors='replace').strip()
            ok &= check('NetworkManager running', running == 'running',
                        running or result.stderr.decode(errors='replace').strip())
        except Exception as exc:
            ok &= check('NetworkManager running', False, str(exc))

    # The facade's own view, which is exactly what the LCD and web page show.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import network_manager
        state = network_manager.status()
        ok &= check('network_manager.status()', state.get('available'),
                    'reason={}'.format(state.get('reason')))
        print('\nReported state:')
        for key in ('available', 'reason', 'portal', 'primary_ip'):
            print('  {:<12} {}'.format(key, state.get(key)))
        for key in ('wifi', 'ethernet'):
            print('  {:<12} {}'.format(key, state.get(key)))
    except Exception as exc:
        ok &= check('network_manager.status()', False, str(exc))

    print('\n' + ('All checks passed.' if ok else
                  'Something above needs fixing; see the FAILED lines.'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
