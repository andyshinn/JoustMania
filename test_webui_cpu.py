"""Tests for the CPU section of the WebUI settings page.

The web saves the CPU settings but piparty applies them (see
web_settings_update), so what matters here is that a change reaches piparty
exactly once, and that a page with the section hidden cannot clobber them.
"""

import os
import sys
import tempfile
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

from werkzeug.datastructures import MultiDict

import common
import cpu_power
import webui

logging.disable(logging.CRITICAL)

GOVERNORS = ['conservative', 'ondemand', 'powersave', 'performance', 'schedutil']
FREQS = [600, 700, 800, 900, 1000, 1100, 1200, 1300, 1400, 1500]
CPU_STATUS = {'governor': 'ondemand', 'cur_mhz': 600, 'max_mhz': 1500,
              'hw_max_mhz': 1500, 'temp_c': 47.6}

# The fields the settings form always posts, as a browser would send them.
FORM = {
    'audio_volume': '80', 'red_on_kill': '', 'sensitivity': '2',
    'random_team_size': '4', 'lcd_enabled': 'auto', 'lcd_brightness': '100',
    'lcd_idle_brightness': '15', 'lcd_idle_dim_secs': '120',
    'ups_enabled': 'auto', 'ups_warn_percent': '20', 'ups_critical_percent': '5',
    'portal_ssid': 'JoustMania', 'portal_password': 'joustpass',
    'portal_fallback_delay_secs': '90',
}
COLORS = ['Magenta', 'Green', 'Orange', 'Turquoise', 'Purple',
          'Yellow', 'Green', 'Blue', 'Purple']


def settings():
    return {
        'sensitivity': 2, 'red_on_kill': True, 'random_team_size': 4,
        'force_all_start': False, 'random_modes': ['JoustFFA'],
        'play_audio': True, 'play_instructions': True, 'audio_volume': 80,
        'color_lock_choices': {2: COLORS[0:2], 3: COLORS[2:5], 4: COLORS[5:9]},
        'cpu_governor': 'auto', 'cpu_max_mhz': 0,
    }


class FakeQueue:
    def __init__(self):
        self.items = []

    def put(self, item):
        self.items.append(item)


class WebCpuTestCase(unittest.TestCase):
    cpufreq = True

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patches = [
            unittest.mock.patch.object(
                common, 'SETTINGSFILE', os.path.join(tmp.name, 'settings.yaml')),
            unittest.mock.patch.object(
                cpu_power, 'governors',
                return_value=list(GOVERNORS) if self.cpufreq else []),
            unittest.mock.patch.object(
                cpu_power, 'frequencies_mhz',
                return_value=list(FREQS) if self.cpufreq else []),
            unittest.mock.patch.object(
                cpu_power, 'status',
                return_value=dict(CPU_STATUS) if self.cpufreq else {}),
            unittest.mock.patch.object(webui.audio_mixer, 'apply'),
            unittest.mock.patch.object(webui.audio_mixer, 'description',
                                       return_value=None),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.queue = FakeQueue()
        self.ns = types.SimpleNamespace(settings=settings(), status={})
        self.ui = webui.WebUI(self.queue, self.ns)

    def post(self, **fields):
        form = MultiDict(FORM)
        for index, color in enumerate(COLORS):
            form.add('color_lock_choices-{}'.format(index), color)
        for key, value in fields.items():
            form[key] = value
        data = webui._fill_cpu_choices(webui.SettingsForm(form)).data
        with self.ui.app.test_request_context():
            self.ui.web_settings_update(data)

    def render(self):
        with self.ui.app.test_request_context('/settings', method='GET'):
            return self.ui.settings()


class WebCpuSaveTest(WebCpuTestCase):
    def test_a_change_is_saved_and_handed_to_piparty(self):
        self.post(cpu_governor='powersave', cpu_max_mhz='1200')
        self.assertEqual(self.ns.settings['cpu_governor'], 'powersave')
        self.assertEqual(self.ns.settings['cpu_max_mhz'], 1200)
        self.assertEqual(self.queue.items, [{'command': 'cpu_apply'}])

    def test_saving_other_settings_does_not_reapply(self):
        self.post(cpu_governor='auto', cpu_max_mhz='0')
        self.assertEqual(self.queue.items, [])

    def test_the_web_never_writes_cpufreq_itself(self):
        """Only piparty may, or Default could not restore the kernel's own."""
        with unittest.mock.patch.object(cpu_power, 'apply') as apply:
            self.post(cpu_governor='powersave', cpu_max_mhz='1200')
        apply.assert_not_called()

    def test_page_preselects_the_saved_values(self):
        self.ns.settings.update(cpu_governor='powersave', cpu_max_mhz=1200)
        html = self.render()
        self.assertIn('<option selected value="powersave">', html)
        self.assertIn('<option selected value="1200">', html)
        self.assertIn('600 of\n            1500 MHz', html)


class WebNoCpufreqTest(WebCpuTestCase):
    cpufreq = False

    def test_section_is_hidden(self):
        self.assertNotIn('cpu_governor', self.render())

    def test_a_post_without_the_section_keeps_saved_values(self):
        self.ns.settings.update(cpu_governor='powersave', cpu_max_mhz=1200)
        self.post()
        self.assertEqual(self.ns.settings['cpu_governor'], 'powersave')
        self.assertEqual(self.ns.settings['cpu_max_mhz'], 1200)
        self.assertEqual(self.queue.items, [])


if __name__ == '__main__':
    unittest.main()
