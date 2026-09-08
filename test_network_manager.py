"""Tests for the NetworkManager facade.

Runs entirely off-hardware: no NetworkManager, no subprocess, no Pi. The
facade's whole job is to turn a flaky external command into something the rest
of JoustMania can call without ever being taken down, so most of these assert
what happens when nmcli misbehaves.

Two layers:
  * FacadeTest stubs the nmcli module with plain namespaces, so it runs even
    where the nmcli package is not installed (a dev laptop, Windows).
  * RealParserTest feeds canned nmcli output through the *real* library
    parsers, proving the shapes the facade reads are the shapes nmcli actually
    produces. Skipped when the package is absent.
"""

import os
import shutil
import tempfile
import types
import unittest
import unittest.mock

import network_manager


def device(name, kind, state='connected', connection='Net'):
    return types.SimpleNamespace(device=name, device_type=kind,
                                 state=state, connection=connection)


def access_point(ssid, signal, security='WPA2', in_use=False):
    return types.SimpleNamespace(ssid=ssid, signal=signal,
                                 security=security, in_use=in_use)


def connection(name, conn_type='wifi', device_name='--', uuid='uuid'):
    return types.SimpleNamespace(name=name, uuid=uuid,
                                 conn_type=conn_type, device=device_name)


class Boom(Exception):
    """Stands in for any of nmcli's ten unrelated exception types."""


class FakeApi:
    """Minimal stand-in for the nmcli module's singletons."""

    def __init__(self, devices=None, details=None, points=None,
                 connections=None, active=None):
        self._devices = devices or []
        self._details = details or {}
        self._points = points or []
        self._connections = connections or []
        self._active = active or []
        self.calls = []
        self.raises = set()

        api = self

        class Device:
            def status(self):
                api._record('device.status')
                return list(api._devices)

            def show(self, ifname):
                api._record('device.show', ifname)
                return dict(api._details.get(ifname, {}))

            def wifi(self, rescan=None):
                api._record('device.wifi', rescan)
                return list(api._points)

            def wifi_connect(self, ssid, password=None, ifname=None):
                api._record('device.wifi_connect', ssid, password, ifname)

            def wifi_hotspot(self, ifname=None, con_name=None, ssid=None,
                             password=None):
                api._record('device.wifi_hotspot', ifname, con_name, ssid, password)

        class Connection:
            def __call__(self):
                api._record('connection.list')
                return list(api._connections)

            def show_all(self, active=False):
                api._record('connection.show_all', active)
                return list(api._active)

            def modify(self, name, options):
                api._record('connection.modify', name, options)

            def delete(self, name):
                api._record('connection.delete', name)

            def up(self, name):
                api._record('connection.up', name)

            def show(self, name):
                api._record('connection.show', name)
                return dict(api._details.get(name, {}))

        self.device = Device()
        self.connection = Connection()

    def _record(self, name, *args):
        self.calls.append((name,) + args)
        if name in self.raises:
            raise Boom(name)

    def called(self, name):
        return [call for call in self.calls if call[0] == name]


class FacadeBase(unittest.TestCase):
    def install(self, api):
        """Point the facade at a fake nmcli and a scratch dnsmasq dir."""
        patcher = unittest.mock.patch.object(network_manager, '_nmcli', api)
        patcher.start()
        self.addCleanup(patcher.stop)

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.dnsmasq = os.path.join(tmp, 'dnsmasq-shared.d', 'portal.conf')
        for name, value in (('DNSMASQ_DIR', os.path.dirname(self.dnsmasq)),
                            ('PORTAL_DNSMASQ_FILE', self.dnsmasq)):
            patcher = unittest.mock.patch.object(network_manager, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return api



class StatusTest(FacadeBase):
    def test_reports_unavailable_without_nmcli(self):
        self.install(None)
        snapshot = network_manager.status()
        self.assertFalse(snapshot['available'])
        self.assertIsNone(snapshot['primary_ip'])

    def test_reports_addresses_for_both_interfaces(self):
        api = self.install(FakeApi(
            devices=[device('wlan0', 'wifi', connection='HomeNet'),
                     device('eth0', 'ethernet', connection='Wired')],
            details={'wlan0': {'IP4.ADDRESS[1]': '192.168.1.40/24'},
                     'eth0': {'IP4.ADDRESS[1]': '10.0.0.5/24'}}))
        snapshot = network_manager.status()
        self.assertTrue(snapshot['available'])
        self.assertEqual(snapshot['wifi']['ip'], '192.168.1.40')
        self.assertEqual(snapshot['ethernet']['ip'], '10.0.0.5')
        # Wired is the more reliable route, so it wins as the address to show.
        self.assertEqual(snapshot['primary_ip'], '10.0.0.5')
        del api

    def test_ignores_loopback_and_unconnected_devices(self):
        self.install(FakeApi(
            devices=[device('lo', 'loopback'),
                     device('eth0', 'ethernet', state='unavailable',
                            connection=None)],
            details={'eth0': {'IP4.ADDRESS[1]': '10.0.0.5/24'}}))
        snapshot = network_manager.status()
        self.assertIsNone(snapshot['wifi'])
        # Not connected, so the stale address must not be reported.
        self.assertIsNone(snapshot['ethernet']['ip'])
        self.assertIsNone(snapshot['primary_ip'])

    def test_portal_address_outranks_client_address(self):
        self.install(FakeApi(
            devices=[device('wlan0', 'wifi')],
            details={'wlan0': {'IP4.ADDRESS[1]': '10.42.0.1/24'}},
            active=[connection(network_manager.PORTAL_CON_NAME)]))
        snapshot = network_manager.status()
        self.assertTrue(snapshot['portal'])
        self.assertEqual(snapshot['primary_ip'], network_manager.PORTAL_ADDRESS)

    def test_degrades_when_nmcli_raises(self):
        api = self.install(FakeApi(devices=[device('wlan0', 'wifi')]))
        api.raises.add('device.status')
        snapshot = network_manager.status()      # must not raise
        self.assertFalse(snapshot['available'])


class ScanTest(FacadeBase):
    def test_dedupes_by_ssid_keeping_strongest_and_sorts(self):
        self.install(FakeApi(points=[
            access_point('HomeNet', 40),
            access_point('Cafe', 80),
            access_point('HomeNet', 92),        # same SSID, better radio
        ]))
        results = network_manager.scan()
        self.assertEqual([ap['ssid'] for ap in results], ['HomeNet', 'Cafe'])
        self.assertEqual(results[0]['signal'], 92)

    def test_drops_hidden_networks(self):
        # A hidden AP reports an empty SSID, which is not something a user can
        # pick from a list.
        self.install(FakeApi(points=[access_point('', 90),
                                     access_point('  ', 80),
                                     access_point('Real', 10)]))
        self.assertEqual([ap['ssid'] for ap in network_manager.scan()], ['Real'])

    def test_rescan_is_only_requested_when_asked(self):
        api = self.install(FakeApi(points=[]))
        network_manager.scan()
        network_manager.scan(rescan=True)
        self.assertEqual([call[1] for call in api.called('device.wifi')],
                         [None, True])

    def test_returns_empty_when_scanning_fails(self):
        api = self.install(FakeApi(points=[access_point('X', 1)]))
        api.raises.add('device.wifi')
        self.assertEqual(network_manager.scan(), [])


class SavedConnectionsTest(FacadeBase):
    def test_hides_portal_and_non_wifi_connections(self):
        self.install(FakeApi(connections=[
            connection('HomeNet', 'wifi', 'wlan0'),
            connection(network_manager.PORTAL_CON_NAME, 'wifi'),
            connection('Wired connection 1', 'ethernet', 'eth0'),
        ]))
        saved = network_manager.saved_connections()
        self.assertEqual([c['name'] for c in saved], ['HomeNet'])
        self.assertTrue(saved[0]['active'])


class ConnectionMethodTest(FacadeBase):
    def test_reports_the_configured_method(self):
        self.install(FakeApi(details={'Wired': {'ipv4.method': 'manual'}}))
        self.assertEqual(network_manager.connection_method('Wired'), 'manual')

    def test_returns_none_for_an_unknown_connection(self):
        api = self.install(FakeApi())
        api.raises.add('connection.show')
        self.assertIsNone(network_manager.connection_method('Nope'))

    def test_returns_none_without_a_name(self):
        api = self.install(FakeApi())
        self.assertIsNone(network_manager.connection_method(None))
        self.assertEqual(api.called('connection.show'), [])


class JoinWifiTest(FacadeBase):
    def test_joins_and_reports_success(self):
        api = self.install(FakeApi())
        ok, message = network_manager.join_wifi('HomeNet', 'hunter2')
        self.assertTrue(ok)
        self.assertIn('HomeNet', message)
        self.assertEqual(api.called('device.wifi_connect')[0][1:3],
                         ('HomeNet', 'hunter2'))

    def test_takes_the_portal_down_first(self):
        # The Pi's radio cannot be an AP and a client at once, so joining while
        # the portal is up has to tear it down or the join silently fails.
        api = self.install(FakeApi(
            active=[connection(network_manager.PORTAL_CON_NAME)]))
        network_manager.join_wifi('HomeNet', 'hunter2')
        names = [call[0] for call in api.calls]
        self.assertLess(names.index('connection.delete'),
                        names.index('device.wifi_connect'))

    def test_reports_failure_instead_of_raising(self):
        api = self.install(FakeApi())
        api.raises.add('device.wifi_connect')
        ok, message = network_manager.join_wifi('HomeNet', 'hunter2')
        self.assertFalse(ok)
        self.assertIn('HomeNet', message)

    def test_rejects_empty_ssid(self):
        api = self.install(FakeApi())
        ok, _ = network_manager.join_wifi('', 'pw')
        self.assertFalse(ok)
        self.assertEqual(api.called('device.wifi_connect'), [])

    def test_reports_unavailable_without_nmcli(self):
        self.install(None)
        ok, _ = network_manager.join_wifi('HomeNet', 'pw')
        self.assertFalse(ok)


class ForgetTest(FacadeBase):
    def test_forgets_a_saved_network(self):
        api = self.install(FakeApi())
        ok, _ = network_manager.forget('HomeNet')
        self.assertTrue(ok)
        self.assertEqual(api.called('connection.delete')[0][1], 'HomeNet')

    def test_refuses_to_forget_the_portal(self):
        # Deleting it here would leave the dnsmasq drop-in behind; the portal
        # control is the only thing that should remove it.
        api = self.install(FakeApi())
        ok, _ = network_manager.forget(network_manager.PORTAL_CON_NAME)
        self.assertFalse(ok)
        self.assertEqual(api.called('connection.delete'), [])


class EthernetTest(FacadeBase):
    def wired(self):
        return FakeApi(devices=[device('eth0', 'ethernet', connection='Wired')])

    def test_static_requires_a_prefix_length(self):
        # NetworkManager rejects a bare address, and a silently broken static
        # config is the easiest way to make the Pi unreachable.
        api = self.install(self.wired())
        ok, message = network_manager.set_ethernet('manual', '192.168.1.50')
        self.assertFalse(ok)
        self.assertIn('/24', message)
        self.assertEqual(api.called('connection.modify'), [])

    def test_applies_static_settings(self):
        api = self.install(self.wired())
        ok, _ = network_manager.set_ethernet(
            'manual', '192.168.1.50/24', '192.168.1.1', '1.1.1.1')
        self.assertTrue(ok)
        name, options = api.called('connection.modify')[0][1:3]
        self.assertEqual(name, 'Wired')
        self.assertEqual(options['ipv4.method'], 'manual')
        self.assertEqual(options['ipv4.addresses'], '192.168.1.50/24')
        self.assertEqual(options['ipv4.gateway'], '192.168.1.1')
        self.assertEqual(api.called('connection.up')[0][1], 'Wired')

    def test_dhcp_clears_the_static_fields(self):
        # Leaving a stale ipv4.addresses behind makes NM ignore the DHCP lease.
        api = self.install(self.wired())
        network_manager.set_ethernet('auto')
        options = api.called('connection.modify')[0][2]
        self.assertEqual(options['ipv4.method'], 'auto')
        self.assertEqual(options['ipv4.addresses'], '')
        self.assertEqual(options['ipv4.gateway'], '')

    def test_reports_when_there_is_no_wired_connection(self):
        api = self.install(FakeApi(devices=[device('wlan0', 'wifi')]))
        ok, message = network_manager.set_ethernet('auto')
        self.assertFalse(ok)
        self.assertIn('wired', message.lower())
        self.assertEqual(api.called('connection.modify'), [])

    def test_rejects_unknown_mode(self):
        api = self.install(self.wired())
        ok, _ = network_manager.set_ethernet('sideways')
        self.assertFalse(ok)
        self.assertEqual(api.called('connection.modify'), [])


class PortalTest(FacadeBase):
    def test_start_creates_hotspot_dns_and_pins_autoconnect_off(self):
        api = self.install(FakeApi())
        ok, _ = network_manager.portal_start('MySSID', 'mypass')
        self.assertTrue(ok)

        hotspot = api.called('device.wifi_hotspot')[0]
        self.assertEqual(hotspot[2], network_manager.PORTAL_CON_NAME)
        self.assertEqual(hotspot[3:5], ('MySSID', 'mypass'))

        # Wildcard DNS is what makes a phone pop the page by itself.
        with open(self.dnsmasq) as handle:
            self.assertIn('address=/#/10.42.0.1', handle.read())

        # A reboot must return to client mode rather than stranding an AP.
        options = api.called('connection.modify')[0][2]
        self.assertEqual(options['connection.autoconnect'], 'no')

    def test_start_is_idempotent(self):
        api = self.install(FakeApi(
            active=[connection(network_manager.PORTAL_CON_NAME)]))
        ok, _ = network_manager.portal_start()
        self.assertTrue(ok)
        self.assertEqual(api.called('device.wifi_hotspot'), [])

    def test_start_cleans_up_dns_when_the_hotspot_fails(self):
        # Otherwise a failed start leaves wildcard DNS breaking the LAN.
        api = self.install(FakeApi())
        api.raises.add('device.wifi_hotspot')
        ok, _ = network_manager.portal_start()
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(self.dnsmasq))

    def test_start_survives_a_failed_modify(self):
        # The AP is already up at that point; losing the autoconnect tweak is
        # not worth reporting the whole thing as a failure.
        api = self.install(FakeApi())
        api.raises.add('connection.modify')
        ok, _ = network_manager.portal_start()
        self.assertTrue(ok)

    def test_stop_removes_dns_and_connection(self):
        api = self.install(FakeApi())
        network_manager.portal_start()
        ok, _ = network_manager.portal_stop()
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(self.dnsmasq))
        self.assertEqual(api.called('connection.delete')[0][1],
                         network_manager.PORTAL_CON_NAME)

    def test_stop_is_safe_when_nothing_is_running(self):
        api = self.install(FakeApi())
        api.raises.add('connection.delete')
        ok, _ = network_manager.portal_stop()       # must not raise
        self.assertFalse(ok)

    def test_active_is_false_when_nmcli_raises(self):
        api = self.install(FakeApi())
        api.raises.add('connection.show_all')
        self.assertFalse(network_manager.portal_active())

    def test_toggle_dispatches_both_ways(self):
        api = self.install(FakeApi())
        network_manager.portal_toggle('start', 'S', 'P')
        self.assertEqual(len(api.called('device.wifi_hotspot')), 1)
        network_manager.portal_toggle('stop')
        self.assertEqual(len(api.called('connection.delete')), 1)


try:
    import nmcli as _real_nmcli
except Exception:
    _real_nmcli = None


@unittest.skipIf(_real_nmcli is None, "nmcli package is not installed")
class RealParserTest(FacadeBase):
    """Feed canned nmcli output through the real library parsers.

    The stubs above assume a shape; this proves the shape is right, and in
    particular that an SSID containing a colon survives -- the exact case that
    made hand-rolling the terse parser a bad idea.
    """

    WIFI_LIST = (
        "*:HomeNet:AA\\:BB\\:CC\\:DD\\:EE\\:FF:Infra:6:2437 MHz:270 Mbit/s:78:WPA2\n"
        " :Guest\\: Wifi:11\\:22\\:33\\:44\\:55\\:66:Infra:1:2412 MHz:130 Mbit/s:45:WPA2\n"
    )
    DEVICE_STATUS = (
        "DEVICE  TYPE      STATE         CONNECTION\n"
        "wlan0   wifi      connected     HomeNet\n"
        "eth0    ethernet  unavailable   --\n"
    )
    DEVICE_SHOW = (
        "GENERAL.DEVICE:                         wlan0\n"
        "IP4.ADDRESS[1]:                         192.168.1.40/24\n"
        "IP4.GATEWAY:                            192.168.1.1\n"
    )

    def build(self, table):
        outer = self

        class Syscmd:
            def nmcli(self, params):
                key = ' '.join([params] if isinstance(params, str) else params)
                outer.assertIn(key, table)
                return table[key]

        syscmd = Syscmd()
        return types.SimpleNamespace(
            device=_real_nmcli.DeviceControl(syscmd),
            connection=_real_nmcli.ConnectionControl(syscmd))

    def test_status_reads_real_device_output(self):
        self.install(self.build({
            'device status': self.DEVICE_STATUS,
            'device show wlan0': self.DEVICE_SHOW,
            'connection show --active': "NAME  UUID  TYPE  DEVICE\n",
        }))
        snapshot = network_manager.status()
        self.assertEqual(snapshot['wifi']['ip'], '192.168.1.40')
        self.assertEqual(snapshot['ethernet']['state'], 'unavailable')
        self.assertEqual(snapshot['primary_ip'], '192.168.1.40')

    def test_scan_reads_an_ssid_containing_a_colon(self):
        self.install(self.build({
            '-t -f IN-USE,SSID,BSSID,MODE,CHAN,FREQ,RATE,SIGNAL,SECURITY '
            'device wifi list': self.WIFI_LIST,
        }))
        results = network_manager.scan()
        self.assertEqual([ap['ssid'] for ap in results],
                         ['HomeNet', 'Guest: Wifi'])
        self.assertTrue(results[0]['in_use'])


class FallbackTest(unittest.TestCase):
    """The one path that changes network state with nobody watching."""

    SETTINGS = {'portal_auto_fallback': True, 'portal_fallback_delay_secs': 90}

    def state(self, **overrides):
        base = {'available': True, 'portal': False,
                'wifi': {'ip': None}, 'ethernet': {'ip': None}}
        base.update(overrides)
        return base

    def test_starts_when_nothing_has_an_address(self):
        self.assertTrue(network_manager.should_start_portal(
            self.state(), self.SETTINGS, elapsed=120))

    def test_waits_for_networkmanager_to_finish_trying(self):
        # NetworkManager can take a while to associate; jumping in early would
        # tear down a connection that was about to succeed.
        self.assertFalse(network_manager.should_start_portal(
            self.state(), self.SETTINGS, elapsed=30))

    def test_does_not_start_when_wifi_is_up(self):
        self.assertFalse(network_manager.should_start_portal(
            self.state(wifi={'ip': '192.168.1.40'}), self.SETTINGS, elapsed=120))

    def test_does_not_start_when_only_ethernet_is_up(self):
        self.assertFalse(network_manager.should_start_portal(
            self.state(ethernet={'ip': '10.0.0.5'}), self.SETTINGS, elapsed=120))

    def test_does_not_start_when_the_portal_is_already_running(self):
        self.assertFalse(network_manager.should_start_portal(
            self.state(portal=True), self.SETTINGS, elapsed=120))

    def test_does_not_start_when_networkmanager_is_unavailable(self):
        # Nothing to fall back to, and the reading is not trustworthy.
        self.assertFalse(network_manager.should_start_portal(
            self.state(available=False), self.SETTINGS, elapsed=120))

    def test_respects_the_opt_out(self):
        self.assertFalse(network_manager.should_start_portal(
            self.state(), {'portal_auto_fallback': False}, elapsed=999))

    def test_missing_interfaces_count_as_no_address(self):
        self.assertTrue(network_manager.should_start_portal(
            {'available': True, 'portal': False, 'wifi': None, 'ethernet': None},
            self.SETTINGS, elapsed=120))

    def test_falls_back_to_a_default_delay(self):
        settings = {'portal_auto_fallback': True}
        self.assertFalse(network_manager.should_start_portal(
            self.state(), settings, elapsed=10))
        self.assertTrue(network_manager.should_start_portal(
            self.state(), settings,
            elapsed=network_manager.DEFAULT_FALLBACK_DELAY_SECS + 1))


if __name__ == '__main__':
    unittest.main()
