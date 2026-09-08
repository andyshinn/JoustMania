"""NetworkManager front-end for JoustMania's captive portal and network settings.

Wraps the `nmcli` PyPI package (a typed wrapper over the same nmcli binary
enable_ap.sh already shells out to). This module exists rather than calling
nmcli directly from webui/lcd_menu because it owns three things the library
does not:

  * portal semantics -- the JoustPortal connection, kept deliberately distinct
    from the permanent `Hotspot` that enable_ap.sh creates, so the two features
    never collide;
  * the dnsmasq drop-in that makes the portal a *real* captive portal rather
    than a hotspot you have to know the URL of;
  * graceful degradation. JoustMania must run normally with no NetworkManager
    at all -- on Windows, on the Steam Deck, and on a dev box -- so nothing in
    here may raise into a caller.

Read functions therefore return an empty/None-ish default on any failure.
Write functions return an (ok, message) pair instead, because the WebUI has to
tell the user *why* joining a network did not work.
"""

import logging
import os
import subprocess

logger = logging.getLogger(__name__)

WIFI_IFNAME = 'wlan0'

# Deliberately not 'Hotspot': that name belongs to enable_ap.sh's permanent AP.
PORTAL_CON_NAME = 'JoustPortal'
# NetworkManager's default gateway for a shared (hotspot) connection.
PORTAL_ADDRESS = '10.42.0.1'
PORTAL_HOSTNAME = 'joust.mania'

DEFAULT_PORTAL_SSID = 'JoustMania'      # lowercase does not work; see enable_ap.sh
DEFAULT_PORTAL_PASSWORD = 'joustpass'

DNSMASQ_DIR = '/etc/NetworkManager/dnsmasq-shared.d'
PORTAL_DNSMASQ_FILE = os.path.join(DNSMASQ_DIR, 'joustmania-portal.conf')
# Wildcard: every name resolves to us, which is what turns the phone's
# connectivity probe into a "Sign in to network" popup.
PORTAL_DNSMASQ_BODY = 'address=/#/{}\n'.format(PORTAL_ADDRESS)

try:
    import nmcli as _nmcli
except Exception:                                    # pragma: no cover
    _nmcli = None
    logger.info("nmcli package not available; networking features disabled")
else:
    # The library prefixes every call with sudo by default. Under supervisor we
    # are already root, and sudo may not even be present in that environment.
    if hasattr(os, 'geteuid') and os.geteuid() == 0:
        _nmcli.disable_use_sudo()


def _api():
    """The nmcli module, or None when networking is unavailable."""
    return _nmcli


def available():
    """True when we can talk to NetworkManager at all.

    Importing nmcli succeeds even on Windows -- it only fails when a command
    actually runs -- so this probes rather than trusting the import.
    """
    if _nmcli is None:
        return False
    try:
        _nmcli.device.status()
    except Exception:
        return False
    return True


# -- reads -------------------------------------------------------------------
# None of these raise. A failure looks the same as "no network", which is the
# behaviour every caller wants anyway.

def _device_ip(ifname):
    """First IPv4 address on a device, without the /prefix. None if unset."""
    try:
        details = _nmcli.device.show(ifname)
    except Exception:
        return None
    for key, value in (details or {}).items():
        if key.startswith('IP4.ADDRESS') and value:
            return value.split('/')[0]
    return None


def status():
    """Snapshot of both interfaces plus portal state. Never raises."""
    snapshot = {
        'available': False,
        'portal': False,
        'wifi': None,
        'ethernet': None,
        'primary_ip': None,
    }
    if _nmcli is None:
        return snapshot

    try:
        devices = _nmcli.device.status()
    except Exception as exc:
        logger.debug("nmcli device status failed: %s", exc)
        return snapshot

    snapshot['available'] = True
    for device in devices:
        if device.device_type not in ('wifi', 'ethernet'):
            continue
        # First device of each type wins; a Pi has one of each.
        if snapshot[device.device_type] is not None:
            continue
        snapshot[device.device_type] = {
            'device': device.device,
            'state': device.state,
            'connection': device.connection,
            'ip': _device_ip(device.device) if device.state == 'connected' else None,
        }

    snapshot['portal'] = portal_active()
    if snapshot['portal']:
        snapshot['primary_ip'] = PORTAL_ADDRESS
    else:
        for key in ('ethernet', 'wifi'):
            entry = snapshot[key]
            if entry and entry['ip']:
                snapshot['primary_ip'] = entry['ip']
                break
    return snapshot


def scan(rescan=False):
    """Visible access points, strongest first. Never raises; [] on failure.

    `rescan` forces a fresh sweep, which takes seconds -- only pass it when the
    user explicitly asked to refresh.
    """
    if _nmcli is None:
        return []
    try:
        points = _nmcli.device.wifi(rescan=True if rescan else None)
    except Exception as exc:
        logger.debug("wifi scan failed: %s", exc)
        return []

    seen = {}
    for point in points:
        ssid = (point.ssid or '').strip()
        if not ssid:
            continue                       # hidden network; nothing to show
        # The same SSID appears once per BSSID; keep the strongest.
        if ssid not in seen or point.signal > seen[ssid]['signal']:
            seen[ssid] = {
                'ssid': ssid,
                'signal': point.signal,
                'security': point.security or '',
                'in_use': point.in_use,
            }
    return sorted(seen.values(), key=lambda ap: ap['signal'], reverse=True)


def saved_connections():
    """Saved wifi connections, so the page can offer a Forget button."""
    if _nmcli is None:
        return []
    try:
        connections = _nmcli.connection()
    except Exception as exc:
        logger.debug("connection list failed: %s", exc)
        return []
    return [
        {'name': con.name, 'uuid': con.uuid, 'active': bool(con.device and con.device != '--')}
        for con in connections
        if con.conn_type in ('wifi', '802-11-wireless') and con.name != PORTAL_CON_NAME
    ]


def connection_method(name):
    """Current ipv4.method for a saved connection ('auto', 'manual', ...).

    Read on demand rather than folded into status(): the settings page is the
    only caller, and status() runs on a timer where an extra nmcli call per
    tick is not worth it.
    """
    if _nmcli is None or not name:
        return None
    try:
        details = _nmcli.connection.show(name)
    except Exception as exc:
        logger.debug("Could not read %r: %s", name, exc)
        return None
    return (details or {}).get('ipv4.method')


def portal_active():
    """True when our portal connection is up. Never raises."""
    if _nmcli is None:
        return False
    try:
        active = _nmcli.connection.show_all(active=True)
    except Exception:
        return False
    return any(con.name == PORTAL_CON_NAME for con in active)


# -- writes ------------------------------------------------------------------
# These return (ok, message). The WebUI shows the message; the LCD ignores it.

def _unavailable():
    return False, 'NetworkManager is not available on this system.'


def join_wifi(ssid, password=None, ifname=WIFI_IFNAME):
    """Connect to an access point, saving it for next boot."""
    if _nmcli is None:
        return _unavailable()
    if not ssid:
        return False, 'No network name was given.'
    try:
        # Bringing the portal down first frees wlan0; the Pi's radio cannot be
        # an AP and a client at the same time.
        if portal_active():
            portal_stop()
        _nmcli.device.wifi_connect(ssid, password or None, ifname=ifname)
    except Exception as exc:
        logger.warning("Could not join %r: %s", ssid, exc)
        return False, 'Could not join "{}": {}'.format(ssid, exc)
    logger.info("Joined wifi network %r", ssid)
    return True, 'Joined "{}".'.format(ssid)


def forget(name):
    """Delete a saved connection."""
    if _nmcli is None:
        return _unavailable()
    if name == PORTAL_CON_NAME:
        return False, 'Use the captive portal control to remove that connection.'
    try:
        _nmcli.connection.delete(name)
    except Exception as exc:
        logger.warning("Could not forget %r: %s", name, exc)
        return False, 'Could not forget "{}": {}'.format(name, exc)
    return True, 'Forgot "{}".'.format(name)


def set_ethernet(mode, address=None, gateway=None, dns=None):
    """Switch the wired connection between DHCP and a static address.

    `address` must carry a prefix length (192.168.1.50/24); NetworkManager
    rejects a bare address, and getting that wrong is the easiest way to end up
    with an unreachable Pi.
    """
    if _nmcli is None:
        return _unavailable()

    connection_name = None
    try:
        for device in _nmcli.device.status():
            if device.device_type == 'ethernet' and device.connection:
                connection_name = device.connection
                break
    except Exception as exc:
        return False, 'Could not read the wired connection: {}'.format(exc)
    if connection_name is None:
        return False, 'No wired connection was found. Is the cable plugged in?'

    if mode == 'auto':
        options = {
            'ipv4.method': 'auto',
            'ipv4.addresses': '',
            'ipv4.gateway': '',
            'ipv4.dns': '',
        }
    elif mode == 'manual':
        if not address or '/' not in address:
            return False, 'Enter the address with a prefix, e.g. 192.168.1.50/24.'
        options = {
            'ipv4.method': 'manual',
            'ipv4.addresses': address,
            'ipv4.gateway': gateway or '',
            'ipv4.dns': dns or '',
        }
    else:
        return False, 'Unknown wired mode: {}'.format(mode)

    try:
        _nmcli.connection.modify(connection_name, options)
        _nmcli.connection.up(connection_name)
    except Exception as exc:
        logger.warning("Could not reconfigure %r: %s", connection_name, exc)
        return False, 'Could not apply the wired settings: {}'.format(exc)
    return True, 'Wired settings applied.'


def _write_dnsmasq_dropin():
    """Point every hostname at us, so phones pop the portal automatically."""
    try:
        os.makedirs(DNSMASQ_DIR, exist_ok=True)
        with open(PORTAL_DNSMASQ_FILE, 'w') as handle:
            handle.write(PORTAL_DNSMASQ_BODY)
    except OSError as exc:
        # Not fatal: the AP still works, you just have to type the address.
        logger.warning("Could not write %s: %s", PORTAL_DNSMASQ_FILE, exc)
        return False
    return True


def _remove_dnsmasq_dropin():
    try:
        os.remove(PORTAL_DNSMASQ_FILE)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Could not remove %s: %s", PORTAL_DNSMASQ_FILE, exc)


def portal_start(ssid=DEFAULT_PORTAL_SSID, password=DEFAULT_PORTAL_PASSWORD):
    """Raise the setup access point and its captive-portal DNS."""
    if _nmcli is None:
        return _unavailable()
    if portal_active():
        return True, 'The captive portal is already running.'

    _write_dnsmasq_dropin()
    try:
        _nmcli.device.wifi_hotspot(
            ifname=WIFI_IFNAME,
            con_name=PORTAL_CON_NAME,
            ssid=ssid or DEFAULT_PORTAL_SSID,
            password=password or DEFAULT_PORTAL_PASSWORD,
        )
    except Exception as exc:
        logger.warning("Could not start the captive portal: %s", exc)
        _remove_dnsmasq_dropin()
        return False, 'Could not start the captive portal: {}'.format(exc)

    try:
        _nmcli.connection.modify(PORTAL_CON_NAME, {
            # The whole point of the portal is that it is temporary: a reboot
            # must come back as a client, never stranded as an AP.
            'connection.autoconnect': 'no',
            '802-11-wireless.powersave': '2',
        })
    except Exception as exc:
        logger.warning("Could not finish configuring the portal: %s", exc)

    logger.info("Captive portal up on SSID %r at %s", ssid, PORTAL_ADDRESS)
    return True, 'Captive portal started on "{}".'.format(ssid)


def portal_stop():
    """Tear the portal down and let NetworkManager reconnect normally."""
    if _nmcli is None:
        return _unavailable()

    _remove_dnsmasq_dropin()
    try:
        _nmcli.connection.delete(PORTAL_CON_NAME)
    except Exception as exc:
        logger.debug("Could not delete %s: %s", PORTAL_CON_NAME, exc)
        return False, 'Could not stop the captive portal: {}'.format(exc)
    logger.info("Captive portal stopped")
    return True, 'Captive portal stopped.'


def portal_toggle(action, ssid=DEFAULT_PORTAL_SSID, password=DEFAULT_PORTAL_PASSWORD):
    """Entry point for the detached Process piparty spawns for LCD commands."""
    try:
        import setproctitle
        setproctitle.setproctitle("JoustMania-Portal")
    except Exception:
        pass

    if action == 'start':
        ok, message = portal_start(ssid, password)
    else:
        ok, message = portal_stop()
    logger.info("Portal %s: %s", action, message)
    return ok


# -- monitor process ---------------------------------------------------------

MONITOR_POLL_SECS = 5.0
DEFAULT_FALLBACK_DELAY_SECS = 90


def should_start_portal(state, settings, elapsed):
    """Decide whether to raise the portal because nothing can reach us.

    Pure, so the one piece of logic that changes network state unattended is
    testable without a Pi.

    With no address on any interface the machine is unreachable, so bringing up
    an AP can only improve matters -- and because the portal connection is
    autoconnect=no, a reboot still returns to client mode. That asymmetry is
    what makes doing this automatically safe.
    """
    if not settings.get('portal_auto_fallback', True):
        return False
    if not state.get('available') or state.get('portal'):
        return False
    # Give NetworkManager time to finish its own connection attempts first.
    delay = settings.get('portal_fallback_delay_secs') or DEFAULT_FALLBACK_DELAY_SECS
    if elapsed < delay:
        return False
    return not any((state.get(key) or {}).get('ip') for key in ('wifi', 'ethernet'))


def start_network_monitor(command_queue, ns):
    """Process entry point: publish network state and run the auto-fallback.

    Its own process, and deliberately not part of the LCD front-end: that one
    exits when no HAT is attached, and a headless Pi is exactly the case where
    an automatic portal matters most. The WebUI reads what this publishes too.
    """
    try:
        import setproctitle
        setproctitle.setproctitle("JoustMania-Network")
    except Exception:
        pass

    import time

    started_at = time.time()
    fallback_sent = False

    while True:
        try:
            state = status()
            try:
                ns.network_status = dict(state)
            except Exception:
                logger.debug("Could not publish network_status", exc_info=True)

            if not fallback_sent:
                try:
                    settings = dict(ns.settings or {})
                except Exception:
                    settings = {}
                if should_start_portal(state, settings, time.time() - started_at):
                    logger.warning(
                        "No network address after %ss; starting the captive portal",
                        int(time.time() - started_at))
                    fallback_sent = True
                    command_queue.put({'command': 'lcd_portal_on'})
        except Exception:
            logger.exception("Network monitor tick failed")
        time.sleep(MONITOR_POLL_SECS)
