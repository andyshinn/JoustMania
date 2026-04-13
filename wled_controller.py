"""WLED LED strip integration.

Runs the WLED HTTP/JSON client in its own subprocess with an asyncio loop,
fed by a multiprocessing.Queue from the main game/menu processes. The public
WledClient handle is fully fire-and-forget: send() never blocks the caller and
never raises if WLED is unreachable.
"""

import asyncio
import logging
import time
from multiprocessing import Process, Queue
from queue import Empty

import aiohttp

logger = logging.getLogger(__name__)

# Message types pushed onto the worker queue.
MSG_GLOBAL_EVENT = 'global_event'
MSG_PLAYER_EVENT = 'player_event'
MSG_BUILD_SEGMENTS = 'build_segments'
MSG_TEAR_SEGMENTS = 'tear_segments'
MSG_TEMPO = 'tempo'
MSG_SHUTDOWN = 'shutdown'

# Fallback effect-name to WLED effect-id map. Names match the WLED FX list;
# anything not here is treated as Solid (0). The user can also pass an int.
EFFECT_IDS = {
    'Solid': 0, 'Blink': 1, 'Breathe': 2, 'Wipe': 3, 'Wipe Random': 4,
    'Random Colors': 5, 'Sweep': 6, 'Dynamic': 7, 'Colorloop': 8,
    'Rainbow': 9, 'Scan': 10, 'Dual Scan': 11, 'Fade': 12, 'Theater': 13,
    'Theater Rainbow': 14, 'Running': 15, 'Saw': 16, 'Twinkle': 17,
    'Dissolve': 18, 'Dissolve Rnd': 19, 'Sparkle': 20, 'Sparkle Dark': 21,
    'Sparkle+': 22, 'Strobe': 23, 'Strobe Rainbow': 24, 'Strobe Mega': 25,
    'Blink Rainbow': 26, 'Android': 27, 'Chase': 28, 'Chase Random': 29,
    'Chase Rainbow': 30, 'Chase Flash': 31, 'Chase Flash Rnd': 32,
    'Rainbow Runner': 33, 'Colorful': 34, 'Traffic Light': 35,
    'Sweep Random': 36, 'Chase 2': 37, 'Aurora': 38, 'Stream': 39,
    'Scanner': 40, 'Lighthouse': 41, 'Fireworks': 42, 'Rain': 43,
    'Tetrix': 44, 'Fire Flicker': 45, 'Gradient': 46, 'Loading': 47,
    'Police': 48, 'Fairy': 49, 'Two Dots': 50, 'Fairytwinkle': 51,
    'Running Dual': 52, 'Halloween': 53, 'Chase 3': 54, 'Tri Wipe': 55,
    'Tri Fade': 56, 'Lightning': 57, 'ICU': 58, 'Multi Comet': 59,
    'Scanner Dual': 60, 'Stream 2': 61, 'Oscillate': 62, 'Pride 2015': 63,
    'Juggle': 64, 'Palette': 65, 'Fire 2012': 66, 'Colorwaves': 67,
    'Bpm': 68, 'Fill Noise': 69, 'Noise 1': 70, 'Noise 2': 71,
    'Noise 3': 72, 'Noise 4': 73, 'Colortwinkles': 74, 'Lake': 75,
    'Meteor': 76, 'Meteor Smooth': 77, 'Railway': 78, 'Ripple': 79,
    'Twinklefox': 80, 'Twinklecat': 81, 'Halloween Eyes': 82,
    'Solid Pattern': 83, 'Solid Pattern Tri': 84, 'Spots': 85,
    'Spots Fade': 86, 'Glitter': 87, 'Candle': 88, 'Fireworks Starburst': 89,
    'Fireworks 1D': 90, 'Bouncing Balls': 91, 'Sinelon': 92,
    'Sinelon Dual': 93, 'Sinelon Rainbow': 94, 'Popcorn': 95,
    'Drip': 96, 'Plasma': 97, 'Percent': 98, 'Ripple Rainbow': 99,
    'Heartbeat': 100, 'Pacifica': 101, 'Candle Multi': 102,
    'Solid Glitter': 103, 'Sunrise': 104, 'Phased': 105, 'Twinkleup': 106,
    'Noise Pal': 107, 'Sine': 108, 'Phased Noise': 109, 'Flow': 110,
    'Chunchun': 111, 'Dancing Shadows': 112, 'Washing Machine': 113,
    'Pulse': 47,
}


def make_client(settings):
    """Construct either a real or no-op client based on settings."""
    if not settings.get('wled_enabled'):
        logger.info("WLED disabled in settings; using NoopWledClient")
        return NoopWledClient()
    cfg = {
        'host': str(settings.get('wled_host', '127.0.0.1')),
        'brightness': int(settings.get('wled_brightness', 180)),
        'strip_length': int(settings.get('wled_strip_length', 300)),
        'track_music_speed': bool(settings.get('wled_track_music_speed', True)),
        'events': dict(settings.get('wled_events', {}) or {}),
    }
    logger.info("WLED enabled: host=%s brightness=%s strip=%s track_tempo=%s events=%d",
                cfg['host'], cfg['brightness'], cfg['strip_length'],
                cfg['track_music_speed'], len(cfg['events']))
    return WledClient(cfg)


class NoopWledClient:
    """Used when WLED is disabled — every method is a no-op."""

    def event(self, *a, **k): pass
    def player_event(self, *a, **k): pass
    def build_segments(self, *a, **k): pass
    def tear_segments(self, *a, **k): pass
    def tempo(self, *a, **k): pass
    def stop(self): pass


class WledClient:
    """Main-process handle. Pushes messages to the worker subprocess."""

    def __init__(self, config):
        self.config = config
        self.queue = Queue()
        self._proc = Process(target=_worker_entry, args=(self.queue, config), daemon=True)
        self._proc.start()
        self._last_tempo_send = 0.0

    def _put(self, msg):
        try:
            self.queue.put_nowait(msg)
        except Exception as e:  # pragma: no cover — only on extreme queue saturation
            logger.debug("WLED queue put failed: %s", e)

    def event(self, name, **kwargs):
        """Fire a configured global event (whole strip / all segments)."""
        self._put({'type': MSG_GLOBAL_EVENT, 'name': name, 'kwargs': kwargs})

    def player_event(self, name, player_index, **kwargs):
        """Fire a configured per-player event (targets that player's segment)."""
        self._put({'type': MSG_PLAYER_EVENT, 'name': name,
                   'player_index': int(player_index), 'kwargs': kwargs})

    def build_segments(self, player_colors):
        """Partition the strip into N evenly-spaced segments, one per player."""
        self._put({'type': MSG_BUILD_SEGMENTS,
                   'colors': [tuple(int(c) for c in rgb) for rgb in player_colors]})

    def tear_segments(self):
        """Collapse to a single full-strip segment."""
        self._put({'type': MSG_TEAR_SEGMENTS})

    def tempo(self, value):
        """Track music_speed for segment effect speed. Throttled to ~10Hz."""
        now = time.time()
        if now - self._last_tempo_send < 0.1:
            return
        self._last_tempo_send = now
        self._put({'type': MSG_TEMPO, 'value': float(value)})

    def stop(self):
        self._put({'type': MSG_SHUTDOWN})
        try:
            self._proc.join(timeout=2)
        finally:
            if self._proc.is_alive():
                self._proc.terminate()


def _worker_entry(queue, config):
    # Child process inherits module-level loggers on Linux fork, but we want
    # to guarantee at least a stderr handler so warnings aren't silently dropped
    # if the fileConfig handlers don't survive the fork.
    import sys
    root = logging.getLogger()
    if not root.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter(
            '%(asctime)s - %(levelname)s - %(name)s - %(message)s'))
        root.addHandler(h)
        root.setLevel(logging.INFO)
    logger.info("WLED worker started, target=http://%s/json/state", config['host'])
    try:
        asyncio.run(_worker_loop(queue, config))
    except Exception:
        logger.exception("WLED worker crashed")
    finally:
        logger.info("WLED worker exiting")


async def _worker_loop(queue, config):
    base_url = f"http://{config['host']}/json/state"
    timeout = aiohttp.ClientTimeout(total=2.0, connect=1.0)
    state = {
        'segments': [],          # list of (start, stop, (r,g,b))
        'last_tempo_sx': None,
        'cooldown_until': 0.0,
        'session': None,
        'last_ok': False,
    }

    async def post(payload):
        if time.time() < state['cooldown_until']:
            logger.debug("WLED post skipped (cooldown %.1fs left)",
                         state['cooldown_until'] - time.time())
            return
        try:
            async with state['session'].post(base_url, json=payload) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning("WLED %s returned %s: %s",
                                   base_url, resp.status, body[:200])
                    state['last_ok'] = False
                else:
                    if not state['last_ok']:
                        logger.info("WLED post OK (%s)", base_url)
                    state['last_ok'] = True
                    logger.debug("WLED post %s -> %s", payload, resp.status)
        except Exception as e:
            logger.warning("WLED post to %s failed (%s); backing off 5s",
                           base_url, e)
            state['cooldown_until'] = time.time() + 5.0
            state['last_ok'] = False

    async with aiohttp.ClientSession(timeout=timeout) as session:
        state['session'] = session
        logger.info("WLED initial brightness -> %s", config['brightness'])
        await post({'on': True, 'bri': config['brightness']})

        while True:
            try:
                msg = queue.get_nowait()
            except Empty:
                await asyncio.sleep(0.03)
                continue
            if msg.get('type') == MSG_SHUTDOWN:
                logger.info("WLED worker received shutdown")
                await post({'on': False})
                return
            try:
                await _handle(msg, state, config, post)
            except Exception:
                logger.exception("WLED handler error for %s", msg.get('type'))


async def _handle(msg, state, config, post):
    t = msg['type']
    events = config['events']

    if t == MSG_GLOBAL_EVENT:
        spec = events.get(msg['name'])
        if not spec:
            logger.debug("WLED event '%s' has no mapping; skipping", msg['name'])
            return
        logger.debug("WLED global event '%s' -> %s", msg['name'], spec)
        payload = _spec_to_state(spec, msg.get('kwargs') or {}, state, scope='all')
        if payload:
            await post(payload)

    elif t == MSG_PLAYER_EVENT:
        spec = events.get(msg['name'])
        if not spec:
            logger.debug("WLED player event '%s' has no mapping; skipping", msg['name'])
            return
        idx = msg['player_index']
        if idx < 0 or idx >= len(state['segments']):
            logger.debug("WLED player event '%s' idx=%s out of range (segs=%d)",
                         msg['name'], idx, len(state['segments']))
            return
        logger.debug("WLED player event '%s' idx=%s -> %s", msg['name'], idx, spec)
        payload = _spec_to_state(spec, msg.get('kwargs') or {}, state,
                                 scope='segment', segment_id=idx)
        if payload:
            await post(payload)
            dur_ms = spec.get('duration_ms')
            if dur_ms:
                asyncio.create_task(_revert_segment(idx, state, post, dur_ms / 1000.0))

    elif t == MSG_BUILD_SEGMENTS:
        colors = msg['colors']
        n = len(colors)
        if n == 0:
            return
        strip = config['strip_length']
        base_size = max(1, strip // n)
        rem = strip - base_size * n
        cursor = 0
        segs_state = []
        out_segs = []
        for i, c in enumerate(colors):
            size = base_size + (1 if i >= n - rem else 0)
            start = cursor
            stop = cursor + size
            cursor = stop
            segs_state.append((start, stop, tuple(c)))
            out_segs.append({
                'id': i, 'start': start, 'stop': stop, 'on': True,
                'col': [list(c), [0, 0, 0], [0, 0, 0]], 'fx': 0, 'sx': 128,
            })
        state['segments'] = segs_state
        state['last_tempo_sx'] = None
        logger.info("WLED build_segments: %d segments across %d LEDs", n, cursor)
        await post({'on': True, 'seg': out_segs})

    elif t == MSG_TEAR_SEGMENTS:
        logger.info("WLED tear_segments")
        state['segments'] = []
        state['last_tempo_sx'] = None
        await post({'on': True, 'seg': [
            {'id': 0, 'start': 0, 'stop': config['strip_length'], 'on': True}
        ]})

    elif t == MSG_TEMPO:
        if not config['track_music_speed'] or not state['segments']:
            return
        speed = msg['value']
        sx = int(max(0, min(255, (speed - 1.0) / 0.3 * 255)))
        if state['last_tempo_sx'] == sx:
            return
        state['last_tempo_sx'] = sx
        await post({'seg': [{'id': i, 'sx': sx} for i in range(len(state['segments']))]})


async def _revert_segment(idx, state, post, delay):
    await asyncio.sleep(delay)
    if idx >= len(state['segments']):
        return
    start, stop, color = state['segments'][idx]
    await post({'seg': [{
        'id': idx, 'start': start, 'stop': stop, 'on': True,
        'col': [list(color), [0, 0, 0], [0, 0, 0]], 'fx': 0,
    }]})


def _spec_to_state(spec, runtime, state, scope, segment_id=None):
    """Translate an event spec ({preset:N} or inline) to a WLED JSON payload."""
    if 'preset' in spec:
        return {'ps': int(spec['preset'])}

    seg = {}
    color = spec.get('color')
    if color == '$player_color':
        color = runtime.get('player_color')
    elif color == '$winning_team':
        color = runtime.get('winning_color')
    if isinstance(color, str) and color.startswith('#'):
        color = _hex_to_rgb(color)
    if color:
        seg['col'] = [list(color), [0, 0, 0], [0, 0, 0]]

    if 'effect' in spec:
        fx = spec['effect']
        seg['fx'] = fx if isinstance(fx, int) else EFFECT_IDS.get(fx, 0)
    if 'palette' in spec and spec['palette'] != '$player_segments':
        pal = spec['palette']
        seg['pal'] = pal if isinstance(pal, int) else 0
    if 'speed' in spec:
        seg['sx'] = int(spec['speed'])
    if 'intensity' in spec:
        seg['ix'] = int(spec['intensity'])

    if scope == 'segment':
        seg['id'] = segment_id
        if segment_id < len(state['segments']):
            start, stop, _ = state['segments'][segment_id]
            seg['start'] = start
            seg['stop'] = stop
        return {'seg': [seg]}

    # Global scope: broadcast to every player segment, or to one full-strip seg.
    if state['segments']:
        out = []
        for i, (start, stop, _) in enumerate(state['segments']):
            copy = dict(seg)
            copy['id'] = i
            copy['start'] = start
            copy['stop'] = stop
            out.append(copy)
        return {'seg': out}
    seg['id'] = 0
    seg['start'] = 0
    seg['stop'] = state.get('strip_length') or 0
    return {'seg': [seg]}


def _hex_to_rgb(h):
    h = h.lstrip('#')
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
