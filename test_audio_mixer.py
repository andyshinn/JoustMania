"""Tests for ALSA mixer discovery.

The control JoustMania has to drive differs between a Pi 4 (headphone jack)
and a Pi 5 (USB adapter), and picking the wrong one is silent -- the volume
setting simply does nothing. So the search order is pinned down here against a
fake alsaaudio, which also lets these run off-hardware.
"""

import logging
import unittest
import unittest.mock

import audio_mixer

# Several tests exercise the "no mixer" and "mixer refused" paths, which log
# loudly by design. Keep the test output readable.
logging.disable(logging.CRITICAL)


class FakeMixer:
    def __init__(self, volume=None, switchcap=()):
        self._volume = volume
        self._switchcap = list(switchcap)
        self.set_to = None
        self.muted = True
        self.closed = False

    def getvolume(self):
        if self._volume is None:
            raise RuntimeError('capture-only control')
        return list(self._volume)

    def setvolume(self, value):
        self.set_to = value

    def switchcap(self):
        return list(self._switchcap)

    def setmute(self, mute):
        self.muted = bool(mute)

    def close(self):
        self.closed = True


class FakeAlsa:
    """Just the surface audio_mixer touches."""

    def __init__(self, cards):
        # cards: {index: (name, {control: FakeMixer or None})}
        self._cards = cards
        self.opened = []

    def card_indexes(self):
        return sorted(self._cards)

    def card_name(self, index):
        return self._cards[index][0]

    def mixers(self, cardindex=-1):
        return list(self._cards[cardindex][1])

    def Mixer(self, control, cardindex):
        mixer = self._cards[cardindex][1][control]
        if mixer is None:
            raise RuntimeError('no such control')
        self.opened.append((control, cardindex))
        return mixer


PI4 = {0: ('Headphones', {'PCM': FakeMixer([70, 70])}),
       1: ('vc4hdmi', {})}

PI5 = {0: ('vc4hdmi0', {'PCM': FakeMixer([50])}),
       1: ('vc4hdmi1', {}),
       2: ('USB Audio Device', {'PCM': FakeMixer([60, 60])})}

CAPTURE_ONLY = {0: ('USB Microphone', {'PCM': FakeMixer(None)}),
                1: ('USB Audio Device', {'Speaker': FakeMixer([40, 40])})}


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        audio_mixer.reset()
        self.addCleanup(audio_mixer.reset)

    def use(self, cards):
        fake = FakeAlsa(cards)
        patcher = unittest.mock.patch.object(audio_mixer, 'alsaaudio', fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def test_pi4_finds_the_headphone_jack(self):
        self.use(PI4)
        self.assertEqual(audio_mixer.find_control(), ('PCM', 0))

    def test_pi5_skips_hdmi_for_the_usb_adapter(self):
        """HDMI is never the game's output, and on a Pi 5 it is card 0 --
        taking the first card with a PCM control would pick it every time."""
        self.use(PI5)
        self.assertEqual(audio_mixer.find_control(), ('PCM', 2))

    def test_capture_only_controls_are_rejected(self):
        self.use(CAPTURE_ONLY)
        self.assertEqual(audio_mixer.find_control(), ('Speaker', 1))

    def test_no_playable_card_is_not_an_error(self):
        self.use({0: ('vc4hdmi0', {}), 1: ('Loopback', {})})
        self.assertIsNone(audio_mixer.find_control())
        self.assertFalse(audio_mixer.available())
        self.assertFalse(audio_mixer.set_volume(50))
        self.assertIsNone(audio_mixer.get_volume())

    def test_discovery_runs_once(self):
        fake = self.use(PI4)
        for _ in range(5):
            audio_mixer.set_volume(50)
        # One probe during discovery, then one open per set.
        self.assertEqual(fake.opened.count(('PCM', 0)), 6)

    def test_rescan_looks_again(self):
        fake = self.use(PI4)
        self.assertEqual(audio_mixer.find_control(), ('PCM', 0))
        fake._cards = PI5
        self.assertEqual(audio_mixer.find_control(rescan=True), ('PCM', 2))

    def test_description_names_the_card(self):
        self.use(PI5)
        self.assertEqual(audio_mixer.description(), 'PCM on USB Audio Device')


class VolumeTest(unittest.TestCase):
    def setUp(self):
        audio_mixer.reset()
        self.addCleanup(audio_mixer.reset)
        self.mixer = FakeMixer([60, 60], switchcap=['Playback Mute'])
        cards = {0: ('USB Audio Device', {'PCM': self.mixer})}
        patcher = unittest.mock.patch.object(audio_mixer, 'alsaaudio',
                                             FakeAlsa(cards))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_set_volume_reaches_the_mixer(self):
        self.assertTrue(audio_mixer.set_volume(35))
        self.assertEqual(self.mixer.set_to, 35)

    def test_set_volume_clears_a_mute_switch(self):
        """A level nobody can hear is the most confusing possible outcome."""
        audio_mixer.set_volume(35)
        self.assertFalse(self.mixer.muted)

    def test_values_are_clamped_into_range(self):
        for given, expected in ((150, 100), (-5, 0), ('60', 60), (None, 80),
                                (33.4, 33)):
            with self.subTest(given=given):
                audio_mixer.set_volume(given)
                self.assertEqual(self.mixer.set_to, expected)

    def test_get_volume_averages_the_channels(self):
        self.mixer._volume = [40, 50]
        self.assertEqual(audio_mixer.get_volume(), 45)

    def test_handles_are_always_closed(self):
        audio_mixer.set_volume(35)
        self.assertTrue(self.mixer.closed)

    def test_apply_uses_the_saved_setting(self):
        audio_mixer.apply({'audio_volume': 25})
        self.assertEqual(self.mixer.set_to, 25)

    def test_apply_falls_back_when_the_setting_is_missing(self):
        audio_mixer.apply({})
        self.assertEqual(self.mixer.set_to, audio_mixer.DEFAULT_VOLUME)

    def test_a_failing_mixer_does_not_raise(self):
        def explode(value):
            raise RuntimeError('device busy')
        self.mixer.setvolume = explode
        self.assertFalse(audio_mixer.set_volume(35))


class NoAlsaTest(unittest.TestCase):
    """Windows, macOS, and any Pi where pyalsaaudio did not install."""

    def setUp(self):
        audio_mixer.reset()
        self.addCleanup(audio_mixer.reset)
        patcher = unittest.mock.patch.object(audio_mixer, 'alsaaudio', None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_call_degrades_quietly(self):
        self.assertFalse(audio_mixer.available())
        self.assertIsNone(audio_mixer.find_control())
        self.assertIsNone(audio_mixer.get_volume())
        self.assertIsNone(audio_mixer.description())
        self.assertFalse(audio_mixer.set_volume(50))
        self.assertFalse(audio_mixer.apply({'audio_volume': 50}))


if __name__ == '__main__':
    unittest.main()
