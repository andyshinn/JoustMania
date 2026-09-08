"""Tests for the PIN gate and captive-portal handling on the WebUI.

The gate is the only thing standing between the LAN and "rewrite this
machine's network configuration", so the cases that matter most here are the
negative ones: that it cannot be brute-forced, and that it does not
accidentally lock anyone out of the pages that were open before.
"""

import sys
import types
import unittest
import unittest.mock

# common.py imports psmoveapi, which only exists on the Pi.
if 'psmoveapi' not in sys.modules:
    _stub = types.ModuleType('psmoveapi')
    _stub.Button = types.SimpleNamespace(
        TRIANGLE=1, CIRCLE=2, CROSS=4, SQUARE=8,
        SELECT=16, START=32, PS=64, MOVE=128, T=256,
    )
    sys.modules['psmoveapi'] = _stub

import logging

import network_manager
import webui

# The lockout path deliberately logs a warning; keep output readable.
logging.disable(logging.CRITICAL)

PIN = '4271'

# Enough of a settings file for the pre-existing pages to render, so the
# "nothing else got gated" tests actually exercise them.
SETTINGS = {
    'sensitivity': 1, 'red_on_kill': False, 'random_team_size': 3,
    'force_all_start': False, 'color_lock_choices': {2: ['Magenta', 'Green'],
                                                     3: ['Orange', 'Turquoise', 'Purple'],
                                                     4: ['Yellow', 'Green', 'Blue', 'Purple']},
    'portal_ssid': 'JoustMania', 'portal_password': 'joustpass',
}

NET_CLIENT = {'available': True, 'portal': False, 'primary_ip': '192.168.1.40',
              'wifi': {'device': 'wlan0', 'state': 'connected',
                       'connection': 'HomeNet', 'ip': '192.168.1.40'},
              'ethernet': {'device': 'eth0', 'state': 'connected',
                           'connection': 'Wired', 'ip': '10.0.0.5'}}
NET_PORTAL = dict(NET_CLIENT, portal=True, primary_ip='10.42.0.1')


class WebUITestCase(unittest.TestCase):
    """Builds a WebUI against a fake namespace and a stubbed network layer."""

    portal = False
    state = NET_CLIENT
    pin = PIN
    eth_method = 'auto'

    def setUp(self):
        self.queue = []
        namespace = types.SimpleNamespace(
            status={}, settings=dict(SETTINGS),
            battery_status={}, ups_status={}, network_status={},
            network_pin=self.pin)
        self.ns = namespace

        queue = types.SimpleNamespace(put=self.queue.append)
        self.web = webui.WebUI(command_queue=queue, ns=namespace)
        self.web.app.config['TESTING'] = True
        self.client = self.web.app.test_client()

        for name, value in (
                ('portal_active', lambda: self.portal),
                ('status', lambda: dict(self.state)),
                ('scan', lambda rescan=False: [
                    {'ssid': 'HomeNet', 'signal': 80, 'security': 'WPA2',
                     'in_use': True}]),
                ('saved_connections', lambda: [
                    {'name': 'HomeNet', 'uuid': 'u', 'active': True}]),
                ('join_wifi', lambda *a, **k: (True, 'ok')),
                ('forget', lambda name: (True, 'forgot ' + name)),
                ('set_ethernet', lambda *a, **k: (True, 'applied')),
                ('connection_method', lambda name: self.eth_method)):
            patcher = unittest.mock.patch.object(network_manager, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def unlock(self):
        return self.client.post('/network', data={'pin': PIN})


class PinGateTest(WebUITestCase):
    def test_network_page_is_locked_by_default(self):
        response = self.client.get('/network')
        self.assertEqual(response.status_code, 401)
        self.assertIn(b'Enter the PIN', response.data)

    def test_correct_pin_unlocks_and_persists(self):
        self.assertEqual(self.unlock().status_code, 200)
        # Session cookie carries the grant to later requests.
        self.assertEqual(self.client.get('/network').status_code, 200)

    def test_wrong_pin_is_rejected(self):
        response = self.client.post('/network', data={'pin': '0000'})
        self.assertEqual(response.status_code, 401)
        self.assertIn(b'not correct', response.data)
        self.assertEqual(self.client.get('/network').status_code, 401)

    def test_lockout_and_rotation_after_repeated_failures(self):
        """10,000 combinations is nothing to a script; the lockout is what
        actually protects this, and rotating means a captured PIN dies too."""
        for _ in range(webui.PIN_MAX_ATTEMPTS):
            self.client.post('/network', data={'pin': '0000'})
        response = self.client.post('/network', data={'pin': PIN})
        self.assertEqual(response.status_code, 401)
        self.assertIn(b'Too many attempts', response.data)
        self.assertNotEqual(self.ns.network_pin, PIN)

    def test_every_network_route_is_gated(self):
        for method, path in (('get', '/network'), ('get', '/network/scan'),
                             ('post', '/network/wifi'),
                             ('post', '/network/forget'),
                             ('post', '/network/ethernet'),
                             ('post', '/network/portal')):
            with self.subTest(path=path):
                response = getattr(self.client, method)(path)
                self.assertEqual(response.status_code, 401)

    def test_existing_pages_are_not_gated(self):
        # The regression that would matter most: a too-broad before_request or
        # prefix match locking people out of the pages they use today.
        self.unlock()
        self.client.delete_cookie('session')
        for path in ('/', '/settings', '/debug', '/power'):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_secret_key_is_not_the_published_constant(self):
        # A hardcoded key in a public repo would let anyone forge the session
        # cookie that the gate relies on.
        self.assertNotEqual(self.web.app.secret_key, "MAGFest is a donut")
        self.assertGreaterEqual(len(self.web.app.secret_key), 16)


class NoPinTest(WebUITestCase):
    pin = None

    def test_fails_open_when_no_pin_was_ever_set(self):
        """Standalone WebUI, or a namespace piparty never populated. Locking
        someone out of their own Pi is worse than the exposure here."""
        self.assertEqual(self.client.get('/network').status_code, 200)


class NetworkPageTest(WebUITestCase):
    def setUp(self):
        super().setUp()
        self.unlock()

    def test_renders_both_interfaces(self):
        page = self.client.get('/network').data
        self.assertIn(b'192.168.1.40', page)
        self.assertIn(b'10.0.0.5', page)
        self.assertIn(b'HomeNet', page)

    def test_scan_returns_json(self):
        payload = self.client.get('/network/scan').get_json()
        self.assertEqual(payload['networks'][0]['ssid'], 'HomeNet')

    def test_join_answers_before_switching_networks(self):
        """The AP the browser arrived over disappears mid-join, so the page
        has to be delivered first and say where to reconnect."""
        with unittest.mock.patch.object(webui, 'Process') as process:
            response = self.client.post('/network/wifi',
                                        data={'ssid': 'HomeNet',
                                              'password': 'hunter2'})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'HomeNet', response.data)
        self.assertIn(b'192.168.1.40', response.data)     # previous address
        args = process.call_args.kwargs['args']
        self.assertEqual(args, ('HomeNet', 'hunter2'))

    def test_empty_ssid_is_refused_rather_than_applied(self):
        # Otherwise the "joining..." page renders with a blank network name and
        # nothing actually happens.
        with unittest.mock.patch.object(webui, 'Process') as process:
            response = self.client.post('/network/wifi', data={'ssid': ''},
                                        follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        process.assert_not_called()

    def test_wired_form_reflects_a_static_configuration(self):
        """Defaulting to DHCP while a static address is in force would invite
        someone to save the page and silently wipe it."""
        self.eth_method = 'manual'
        page = self.client.get('/network').data
        self.assertIn(b'<option selected value="manual">', page)

    def test_hidden_ssid_wins_over_the_dropdown(self):
        with unittest.mock.patch.object(webui, 'Process') as process:
            self.client.post('/network/wifi',
                             data={'ssid': 'HomeNet', 'hidden_ssid': ' Secret ',
                                   'password': 'pw'})
        self.assertEqual(process.call_args.kwargs['args'][0], 'Secret')

    def test_portal_toggle_goes_through_the_command_queue(self):
        # piparty owns starting and stopping the portal, so both front-ends
        # take the same path.
        self.client.post('/network/portal')
        self.assertEqual(self.queue, [{'command': 'lcd_portal_on'}])

    def test_portal_toggle_stops_when_running(self):
        self.portal = True
        self.web._portal_checked_at = 0.0
        self.client.post('/network/portal')
        self.assertEqual(self.queue, [{'command': 'lcd_portal_off'}])

    def test_forget_and_ethernet_flash_their_result(self):
        self.assertEqual(
            self.client.post('/network/forget', data={'name': 'HomeNet'},
                             follow_redirects=True).status_code, 200)
        self.assertEqual(
            self.client.post('/network/ethernet', data={'method': 'auto'},
                             follow_redirects=True).status_code, 200)


class CaptivePortalTest(WebUITestCase):
    def probe(self, path='/generate_204'):
        return self.client.get(path)

    def test_probes_succeed_quietly_when_the_portal_is_off(self):
        # Answering a probe with a redirect on a normal LAN would make every
        # client think the network is broken.
        self.assertEqual(self.probe().status_code, 204)

    def test_probes_answer_what_a_working_network_answers(self):
        """These paths used to 404, and a 404 reads as "a portal is
        intercepting me" -- which would pop a spurious sign-in browser for
        anyone using the permanent access point from enable_ap.sh."""
        self.assertIn(b'Success', self.probe('/hotspot-detect.html').data)
        self.assertEqual(self.probe('/ncsi.txt').data, b'Microsoft NCSI')
        self.assertEqual(self.probe('/gen_204').status_code, 204)

    def test_probes_redirect_to_the_setup_page_when_the_portal_is_on(self):
        self.portal = True
        self.web._portal_checked_at = 0.0
        response = self.probe()
        self.assertEqual(response.status_code, 302)
        self.assertIn('joust.mania', response.headers['Location'])
        self.assertIn('/network', response.headers['Location'])

    def test_all_known_probe_urls_are_answered(self):
        self.portal = True
        self.web._portal_checked_at = 0.0
        for path in webui.CAPTIVE_PROBE_PATHS:
            with self.subTest(path=path):
                self.assertEqual(self.probe(path).status_code, 302)

    def test_stray_requests_redirect_while_the_portal_is_up(self):
        self.portal = True
        self.web._portal_checked_at = 0.0
        self.assertEqual(self.client.get('/settings').status_code, 302)

    def test_the_setup_page_itself_is_never_redirected(self):
        # Redirecting /network to /network would loop forever.
        self.portal = True
        self.web._portal_checked_at = 0.0
        self.assertEqual(self.client.get('/network').status_code, 401)
        static = self.client.get('/static/jouststyle.css')
        self.assertEqual(static.status_code, 200)
        static.close()

    def test_nothing_is_redirected_on_a_normal_lan(self):
        for path in ('/', '/settings', '/debug'):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)


if __name__ == '__main__':
    unittest.main()
