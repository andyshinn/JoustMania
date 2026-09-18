"""CPU frequency scaling for JoustMania.

Lets the LCD pick a cpufreq governor and cap the top clock, mainly so a
battery-powered box can trade some headroom for run time. Lowering the cap
also lowers the core voltage: the firmware picks the voltage for the clock
it is running, so this gets most of what undervolting would, with none of
the stability risk.

Everything goes through the kernel's cpufreq sysfs files, which piparty can
write because JoustMania runs as root. None of it survives a reboot, so the
saved settings are pushed at the kernel again at startup.

The defaults ('auto' and no cap) touch nothing. JoustMania also runs on the
Steam Deck and on desktop Linux, where the governor is not ours to change
unless someone asks. Going back to a default restores whatever the kernel
had before JoustMania first changed it, not a guess at what the OS would
have picked.

Every path here degrades to "unavailable" off-Linux or without cpufreq, so
callers can apply settings unconditionally and ignore the result.
"""

import glob
import logging
import os

logger = logging.getLogger(__name__)

CPUFREQ_ROOT = '/sys/devices/system/cpu/cpufreq'
THERMAL_PATH = '/sys/class/thermal/thermal_zone0/temp'

AUTO_GOVERNOR = 'auto'
NO_CAP = 0
SETTING_KEYS = ('cpu_governor', 'cpu_max_mhz')

# 'userspace' only moves the clock when something writes scaling_setspeed,
# and nothing here does, so offering it would just freeze the clock wherever
# it happened to be.
HIDDEN_GOVERNORS = ('userspace',)

# Fallback step when the driver does not list its operating points.
FALLBACK_STEP_MHZ = 100

# {policy dir: {sysfs name: value}} as found before JoustMania first wrote
# to it, so choosing a default puts things back exactly.
_original = {}


def reset():
    """Forget what the kernel was set to before we touched it."""
    _original.clear()


# -- sysfs -------------------------------------------------------------------

def _policies():
    return sorted(glob.glob(os.path.join(CPUFREQ_ROOT, 'policy*')))


def _read(path):
    try:
        with open(path) as handle:
            return handle.read().strip()
    except OSError:
        return None


def _read_int(path):
    try:
        return int(_read(path))
    except (TypeError, ValueError):
        return None


def _write(path, value):
    try:
        with open(path, 'w') as handle:
            handle.write(str(value))
    except OSError as exc:
        logger.warning("Could not write %s to %s: %s", value, path, exc)
        return False
    return True


def _first_policy():
    policies = _policies()
    return policies[0] if policies else None


# -- discovery ---------------------------------------------------------------

def available():
    policy = _first_policy()
    return policy is not None and _read(
        os.path.join(policy, 'scaling_governor')) is not None


def governors():
    """Governors worth offering, in the order the kernel lists them."""
    policy = _first_policy()
    if policy is None:
        return []
    listed = _read(os.path.join(policy, 'scaling_available_governors')) or ''
    return [g for g in listed.split() if g not in HIDDEN_GOVERNORS]


def frequencies_mhz():
    """The clocks the CPU can run at, lowest first.

    The Pi's driver lists its operating points. Drivers that do not (e.g.
    intel_pstate) get 100 MHz steps between the hardware limits instead.
    """
    policy = _first_policy()
    if policy is None:
        return []
    listed = _read(os.path.join(policy, 'scaling_available_frequencies'))
    if listed:
        try:
            return sorted({int(khz) // 1000 for khz in listed.split()})
        except ValueError:
            pass
    low = _read_int(os.path.join(policy, 'cpuinfo_min_freq'))
    high = _read_int(os.path.join(policy, 'cpuinfo_max_freq'))
    if not low or not high:
        return []
    low, high = low // 1000, high // 1000
    steps = list(range(low, high, FALLBACK_STEP_MHZ))
    return steps + [high]


def normalize_mhz(value):
    """Coerce whatever the settings file holds into a cap in MHz, 0 for none."""
    try:
        mhz = int(value)
    except (TypeError, ValueError):
        return NO_CAP
    return max(NO_CAP, mhz)


def status():
    """Live readings for the LCD, or {} when there is no cpufreq."""
    policy = _first_policy()
    if policy is None:
        return {}
    governor = _read(os.path.join(policy, 'scaling_governor'))
    if governor is None:
        return {}
    reading = {'governor': governor}
    for key, name in (('cur_mhz', 'scaling_cur_freq'),
                      ('max_mhz', 'scaling_max_freq'),
                      ('hw_max_mhz', 'cpuinfo_max_freq')):
        khz = _read_int(os.path.join(policy, name))
        reading[key] = khz // 1000 if khz else None
    millidegrees = _read_int(THERMAL_PATH)
    reading['temp_c'] = millidegrees / 1000.0 if millidegrees is not None else None
    return reading


# -- set ---------------------------------------------------------------------

def _remember(policy):
    if policy not in _original:
        _original[policy] = {
            name: _read(os.path.join(policy, name))
            for name in ('scaling_governor', 'scaling_max_freq')
        }


def _restore(name):
    ok = True
    for policy, saved in _original.items():
        value = saved.get(name)
        if value is not None:
            ok = _write(os.path.join(policy, name), value) and ok
    return ok


def set_governor(name):
    """Switch every policy to `name`. Returns True if all of them took it."""
    policies = _policies()
    if not policies:
        return False
    ok = True
    for policy in policies:
        listed = (_read(os.path.join(policy, 'scaling_available_governors'))
                  or '').split()
        if name not in listed or name in HIDDEN_GOVERNORS:
            logger.warning("CPU governor %r is not available on %s", name, policy)
            ok = False
            continue
        _remember(policy)
        ok = _write(os.path.join(policy, 'scaling_governor'), name) and ok
    return ok


def set_max_mhz(mhz):
    """Cap every policy at `mhz`, clamped to what the hardware can do."""
    policies = _policies()
    if not policies:
        return False
    ok = True
    for policy in policies:
        khz = int(mhz) * 1000
        low = _read_int(os.path.join(policy, 'cpuinfo_min_freq'))
        high = _read_int(os.path.join(policy, 'cpuinfo_max_freq'))
        if low:
            khz = max(low, khz)
        if high:
            khz = min(high, khz)
        _remember(policy)
        ok = _write(os.path.join(policy, 'scaling_max_freq'), khz) and ok
    return ok


def apply(settings):
    """Push the saved governor and cap at the kernel. Used at startup and on save."""
    if not available():
        return False
    try:
        governor = settings.get('cpu_governor') or AUTO_GOVERNOR
        mhz = normalize_mhz(settings.get('cpu_max_mhz', NO_CAP))
    except AttributeError:
        return False

    if governor == AUTO_GOVERNOR:
        ok = _restore('scaling_governor')
    else:
        ok = set_governor(governor)
    if mhz == NO_CAP:
        ok = _restore('scaling_max_freq') and ok
    else:
        ok = set_max_mhz(mhz) and ok
    return ok
