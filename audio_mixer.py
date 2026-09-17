"""ALSA output volume for JoustMania.

Kept apart from piaudio on purpose: the WebUI, the LCD front-end and the menu
process all want to change the volume, and none of them should drag pygame,
numpy and scipy into its process to do one ioctl on a mixer control.

Which control to drive is not fixed. setup.sh points ALSA at the Pi 4's
headphone jack or at a USB audio adapter on a Pi 5 (see update_asound.sh), and
those expose different mixer names on different card numbers -- so the control
is discovered once and cached, rather than hardcoded to a name that is right on
one Pi and missing on the next.

Every path here degrades to "no mixer" off-Linux, without pyalsaaudio, or when
ALSA has nothing playable to offer, so callers can set the volume
unconditionally and ignore the result.
"""

import contextlib
import logging
from sys import platform

logger = logging.getLogger(__name__)

if platform == "linux" or platform == "linux2":
    try:
        import alsaaudio
    except ImportError:          # pyalsaaudio is a setup.sh install, not a given
        alsaaudio = None
else:
    alsaaudio = None

MIN_VOLUME = 0
MAX_VOLUME = 100
VOLUME_STEP = 5
DEFAULT_VOLUME = 80

# Cards worth trying first, mirroring update_asound.sh's own search: the Pi 4
# plays out of 'Headphones', the Pi 5 out of a USB audio adapter.
PREFERRED_CARDS = ('headphone', 'usb')
# Cards that can never be the game's output, so not worth probing at all.
SKIP_CARDS = ('hdmi', 'modem')
# Playback controls we know how to drive, best first. A USB adapter usually
# offers 'PCM' or 'Speaker'; the Pi's own jack offers 'Headphone' on Bookworm
# and 'PCM' on older images.
PREFERRED_CONTROLS = ('PCM', 'Master', 'Speaker', 'Headphone', 'Digital')

# (control, cardindex) once found, False once discovery has failed, None
# before the first look. Cached because set_volume() is called ~8x a second
# while someone holds a button on the LCD.
_control = None


def reset():
    """Forget the discovered control, so the next call rescans."""
    global _control
    _control = None


def clamp(percent):
    """Coerce anything a settings file might hold into 0-100."""
    try:
        value = int(round(float(percent)))
    except (TypeError, ValueError):
        return DEFAULT_VOLUME
    return max(MIN_VOLUME, min(MAX_VOLUME, value))


# -- discovery ---------------------------------------------------------------

def _card_indexes():
    if hasattr(alsaaudio, 'card_indexes'):
        try:
            return list(alsaaudio.card_indexes())
        except Exception:
            return []
    try:
        return list(range(len(alsaaudio.cards())))
    except Exception:
        return []


def _card_name(index):
    try:
        name = alsaaudio.card_name(index)
    except Exception:
        return ''
    # card_name() returns (name, longname) on pyalsaaudio >= 0.9 and a plain
    # string before that.
    if isinstance(name, (tuple, list)):
        name = ' '.join(str(part) for part in name)
    return str(name)


def _card_rank(index):
    """Sort key: preferred cards first, in PREFERRED_CARDS order."""
    name = _card_name(index).lower()
    for rank, wanted in enumerate(PREFERRED_CARDS):
        if wanted in name:
            return (rank, index)
    return (len(PREFERRED_CARDS), index)


def _candidate_cards():
    cards = [i for i in _card_indexes()
             if not any(skip in _card_name(i).lower() for skip in SKIP_CARDS)]
    return sorted(cards, key=_card_rank)


@contextlib.contextmanager
def _open(control, cardindex):
    handle = alsaaudio.Mixer(control=control, cardindex=cardindex)
    try:
        yield handle
    finally:
        try:
            handle.close()
        except Exception:
            pass


def _plays_back(control, cardindex):
    """True if this control actually has a playback level to set.

    Asking for the level is a better test than reading volumecap(): it rules
    out capture-only controls on every pyalsaaudio version, without having to
    know how that version spells its capability strings.
    """
    try:
        with _open(control, cardindex) as mixer:
            return bool(mixer.getvolume())
    except Exception:
        return False


def _discover():
    for cardindex in _candidate_cards():
        try:
            controls = alsaaudio.mixers(cardindex=cardindex)
        except Exception:
            continue
        for wanted in PREFERRED_CONTROLS:
            if wanted in controls and _plays_back(wanted, cardindex):
                return wanted, cardindex
    return None


def find_control(rescan=False):
    """The (control, cardindex) to drive, or None if there is no mixer."""
    global _control
    if alsaaudio is None:
        return None
    if _control is not None and not rescan:
        return _control or None

    found = _discover()
    _control = found or False
    if found:
        logger.info("Audio volume: mixer control %r on card %d (%s)",
                    found[0], found[1], _card_name(found[1]))
    else:
        logger.warning("No usable ALSA playback mixer; output volume is fixed")
    return found


def available():
    return find_control() is not None


def description():
    """How the control reads on the settings page, or None when there is none."""
    found = find_control()
    if not found:
        return None
    control, cardindex = found
    return '{} on {}'.format(control, _card_name(cardindex) or
                             'card {}'.format(cardindex))


# -- get / set ---------------------------------------------------------------

def get_volume():
    """Current output volume as a percent, or None if there is no mixer."""
    found = find_control()
    if not found:
        return None
    try:
        with _open(*found) as mixer:
            levels = mixer.getvolume()
    except Exception:
        logger.debug("Could not read the mixer volume", exc_info=True)
        return None
    if not levels:
        return None
    # Channels can sit at different levels if something else moved them; the
    # menu shows one number, so average them.
    return clamp(sum(levels) / len(levels))


def _unmute(mixer):
    """Clear the mute switch where the control carries one.

    A saved volume means nothing if the switch is off, and nothing else in
    JoustMania ever turns it back on.
    """
    try:
        if any('Mute' in cap for cap in mixer.switchcap()):
            mixer.setmute(0)
    except Exception:
        logger.debug("Could not unmute the mixer", exc_info=True)


def set_volume(percent):
    """Set the output volume. Returns True if the mixer took it."""
    found = find_control()
    if not found:
        return False
    value = clamp(percent)
    try:
        with _open(*found) as mixer:
            mixer.setvolume(value)
            _unmute(mixer)
    except Exception:
        logger.warning("Could not set the output volume to %d%%", value,
                       exc_info=True)
        return False
    return True


def apply(settings):
    """Push the saved volume at the mixer. Used at startup and after a save."""
    try:
        saved = settings.get('audio_volume', DEFAULT_VOLUME)
    except AttributeError:
        saved = DEFAULT_VOLUME
    return set_volume(saved)
