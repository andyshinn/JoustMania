"""Tests for cpufreq control.

Run against a fake sysfs tree in a temp directory, laid out like a Pi 4's
(one policy covering all four cores), so they run off-hardware.
"""

import logging
import os
import tempfile
import unittest
import unittest.mock

import cpu_power

# The "governor not available" and "write refused" paths log by design.
logging.disable(logging.CRITICAL)

PI4_FREQS = '600000 700000 800000 900000 1000000 1100000 1200000 1300000 1400000 1500000'


class FakeSysfs:
    def __init__(self, root, policies=('policy0',), governor='ondemand',
                 governors='conservative ondemand userspace powersave performance schedutil',
                 freqs=PI4_FREQS, max_khz=1500000, min_khz=600000):
        self.root = root
        self.policies = []
        for name in policies:
            policy = os.path.join(root, 'cpufreq', name)
            os.makedirs(policy)
            files = {
                'scaling_governor': governor,
                'scaling_available_governors': governors,
                'cpuinfo_max_freq': max_khz,
                'cpuinfo_min_freq': min_khz,
                'scaling_max_freq': max_khz,
                'scaling_cur_freq': min_khz,
            }
            if freqs is not None:
                files['scaling_available_frequencies'] = freqs
            for key, value in files.items():
                self.put(policy, key, value)
            self.policies.append(policy)
        self.thermal = os.path.join(root, 'temp')
        with open(self.thermal, 'w') as handle:
            handle.write('48312\n')

    @staticmethod
    def put(policy, name, value):
        with open(os.path.join(policy, name), 'w') as handle:
            handle.write('{}\n'.format(value))

    def get(self, name, policy=0):
        with open(os.path.join(self.policies[policy], name)) as handle:
            return handle.read().strip()


class CpuPowerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        cpu_power.reset()
        self.addCleanup(cpu_power.reset)

    def sysfs(self, **kwargs):
        fake = FakeSysfs(self._tmp.name, **kwargs)
        for name, value in (('CPUFREQ_ROOT', os.path.join(fake.root, 'cpufreq')),
                            ('THERMAL_PATH', fake.thermal)):
            patcher = unittest.mock.patch.object(cpu_power, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return fake


class NoCpufreqTest(CpuPowerTestCase):
    """macOS, Windows, containers: everything reads as unavailable."""

    def setUp(self):
        super().setUp()
        patcher = unittest.mock.patch.object(
            cpu_power, 'CPUFREQ_ROOT', os.path.join(self._tmp.name, 'missing'))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_everything_degrades(self):
        self.assertFalse(cpu_power.available())
        self.assertEqual(cpu_power.governors(), [])
        self.assertEqual(cpu_power.frequencies_mhz(), [])
        self.assertEqual(cpu_power.status(), {})
        self.assertFalse(cpu_power.apply({'cpu_governor': 'powersave'}))


class DiscoveryTest(CpuPowerTestCase):
    def test_userspace_is_not_offered(self):
        self.sysfs()
        self.assertNotIn('userspace', cpu_power.governors())
        self.assertIn('powersave', cpu_power.governors())

    def test_frequencies_come_from_the_driver(self):
        self.sysfs()
        self.assertEqual(cpu_power.frequencies_mhz(),
                         [600, 700, 800, 900, 1000, 1100, 1200, 1300, 1400, 1500])

    def test_frequencies_fall_back_to_steps_between_the_limits(self):
        self.sysfs(freqs=None, min_khz=400000, max_khz=1750000)
        mhz = cpu_power.frequencies_mhz()
        self.assertEqual(mhz[0], 400)
        self.assertEqual(mhz[-1], 1750)
        self.assertIn(1700, mhz)

    def test_status(self):
        self.sysfs()
        self.assertEqual(cpu_power.status(), {
            'governor': 'ondemand', 'cur_mhz': 600, 'max_mhz': 1500,
            'hw_max_mhz': 1500, 'temp_c': 48.312,
        })


class ApplyTest(CpuPowerTestCase):
    def test_defaults_touch_nothing(self):
        """The Steam Deck's governor is not ours to change unasked."""
        fake = self.sysfs(governor='schedutil', max_khz=1500000)
        with unittest.mock.patch.object(cpu_power, '_write') as write:
            cpu_power.apply({'cpu_governor': 'auto', 'cpu_max_mhz': 0})
            cpu_power.apply({})
        write.assert_not_called()
        self.assertEqual(fake.get('scaling_governor'), 'schedutil')

    def test_sets_governor_and_cap_on_every_policy(self):
        fake = self.sysfs(policies=('policy0', 'policy4'))
        self.assertTrue(cpu_power.apply(
            {'cpu_governor': 'conservative', 'cpu_max_mhz': 1200}))
        for index in (0, 1):
            self.assertEqual(fake.get('scaling_governor', index), 'conservative')
            self.assertEqual(fake.get('scaling_max_freq', index), '1200000')

    def test_cap_is_clamped_to_the_hardware(self):
        fake = self.sysfs()
        cpu_power.apply({'cpu_max_mhz': 9000})
        self.assertEqual(fake.get('scaling_max_freq'), '1500000')
        cpu_power.apply({'cpu_max_mhz': 100})
        self.assertEqual(fake.get('scaling_max_freq'), '600000')

    def test_back_to_default_restores_what_the_kernel_had(self):
        fake = self.sysfs(governor='schedutil', max_khz=1500000)
        FakeSysfs.put(fake.policies[0], 'scaling_max_freq', 1400000)
        cpu_power.apply({'cpu_governor': 'powersave', 'cpu_max_mhz': 800})
        cpu_power.apply({'cpu_governor': 'auto', 'cpu_max_mhz': 0})
        self.assertEqual(fake.get('scaling_governor'), 'schedutil')
        self.assertEqual(fake.get('scaling_max_freq'), '1400000')

    def test_unknown_governor_is_refused(self):
        fake = self.sysfs()
        self.assertFalse(cpu_power.apply({'cpu_governor': 'turbo'}))
        self.assertFalse(cpu_power.apply({'cpu_governor': 'userspace'}))
        self.assertEqual(fake.get('scaling_governor'), 'ondemand')

    def test_junk_cap_means_no_cap(self):
        self.assertEqual(cpu_power.normalize_mhz('fast'), 0)
        self.assertEqual(cpu_power.normalize_mhz(None), 0)
        self.assertEqual(cpu_power.normalize_mhz(-5), 0)
        self.assertEqual(cpu_power.normalize_mhz('1200'), 1200)

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0,
                     'root can write a read-only file')
    def test_write_refused_reports_failure(self):
        """What happens when JoustMania is not running as root."""
        fake = self.sysfs()
        os.chmod(os.path.join(fake.policies[0], 'scaling_governor'), 0o444)
        self.assertFalse(cpu_power.set_governor('powersave'))
        self.assertEqual(fake.get('scaling_governor'), 'ondemand')


if __name__ == '__main__':
    unittest.main()
