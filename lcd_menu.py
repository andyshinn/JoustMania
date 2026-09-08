"""LCD KeyPad HAT front-end for JoustMania.

Runs as a sibling process to the WebUI, sharing the same two objects the WebUI
already uses: the Manager().Namespace (`ns`) for state, and the command Queue
for actions. It is therefore a *third peer* on an existing seam rather than a
new one -- the LCD reads ns.status/ns.settings and pushes the same command
strings the web buttons push, so no game code has to know it exists.

It must be its own process: Menu.start_game() blocks the menu loop for the
whole match, which is exactly when the display matters most.

Screens are `Page` objects held in a stack. A page is a pure function of a Ctx
snapshot -- it renders two lines and returns Actions rather than touching ns or
the queue -- so the entire UI is testable off-hardware. Adding a screen later
means writing one class and adding one line to a registry; nothing in the run
loop, renderer or navigation knows what any page does.
"""

import logging
import socket
import time
from dataclasses import dataclass, field

import lcd_hat
import ups_hat

logger = logging.getLogger(__name__)

COLS = lcd_hat.COLS

# Buttons, as reported by lcd_hat.Keypad.
UP, DOWN, LEFT, RIGHT, SELECT = 'UP', 'DOWN', 'LEFT', 'RIGHT', 'SELECT'

LOOP_SLEEP_SECS = 0.05      # ~20Hz; fast enough for buttons to feel instant
UPS_POLL_SECS = 10.0

# Ambient backlight hues by game_status. Brightness is applied separately.
AMBIENT_COLORS = {
    'menu': (80, 80, 110),
    'starting': (255, 140, 0),
    'in_game': (0, 200, 60),
    'killed': (255, 0, 0),
}
DEFAULT_AMBIENT = (80, 80, 110)
CRITICAL_COLOR = (255, 0, 0)
WINNER_FLASH_SECS = 5.0
SHUTDOWN_GRACE_SECS = 30.0   # warning shown on the LCD before poweroff

DEFAULTS = {
    'lcd_brightness': 100,
    'lcd_idle_brightness': 15,
    'lcd_idle_dim_secs': 120,
    'lcd_backlight_ambient': True,
    'ups_warn_percent': 20,
    'ups_critical_percent': 5,
    'ups_auto_shutdown': True,
}

# Bounds for every numeric setting the LCD can edit: (min, max, step).
NUMERIC_RANGES = {
    'lcd_brightness': (10, 100, 5),
    'lcd_idle_brightness': (0, 100, 5),
    'lcd_idle_dim_secs': (0, 900, 30),
    'ups_warn_percent': (5, 50, 5),
    'ups_critical_percent': (1, 25, 1),
}


@dataclass(frozen=True)
class Ctx:
    """Read-only snapshot handed to pages each tick.

    Pages get this and nothing else -- no ns, no queue -- which is what keeps
    them pure and testable.
    """
    status: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    ups: dict = field(default_factory=dict)
    now: float = 0.0

    @property
    def game_status(self):
        return self.status.get('game_status', 'menu')

    @property
    def in_game(self):
        return self.game_status not in ('menu', '')

    def setting(self, key):
        value = self.settings.get(key)
        return DEFAULTS.get(key) if value is None else value


# -- Actions -----------------------------------------------------------------
# Pages return these instead of mutating anything; one dispatcher applies them.

@dataclass(frozen=True)
class Push:
    page: object


@dataclass(frozen=True)
class Pop:
    pass


@dataclass(frozen=True)
class Command:
    """Goes onto command_queue, the same bus the WebUI buttons use."""
    payload: dict


@dataclass(frozen=True)
class SetSetting:
    key: str
    value: object


# -- Page base ---------------------------------------------------------------

class Page:
    title = ''

    def on_enter(self, ctx):
        pass

    def on_exit(self, ctx):
        pass

    def render(self, ctx):
        """Return exactly two strings; the renderer pads/truncates to 16."""
        raise NotImplementedError

    def on_button(self, btn, ctx):
        return None

    def backlight(self, ctx):
        """RGB to force, or None to defer to the ambient stack."""
        return None

    def visible(self, ctx):
        return True


class StaticPage(Page):
    """Two rendered lines, no input. Base for the home carousel."""

    def render(self, ctx):
        return self.title, ''


class Item:
    """An entry in a ListPage."""

    label = ''

    def render_label(self, ctx):
        return self.label

    def value_text(self, ctx):
        return ''

    def activate(self, ctx):
        return None

    def visible(self, ctx):
        return True


class ListPage(Page):
    """Scrolling list with a cursor. Base for every menu."""

    show_counter = True

    def __init__(self, title, items):
        self.title = title
        self.items = items
        self.index = 0

    def on_enter(self, ctx):
        self.index = 0

    def _visible_items(self, ctx):
        return [item for item in self.items if item.visible(ctx)]

    def render(self, ctx):
        items = self._visible_items(ctx)
        if not items:
            return self.title, '(none)'
        self.index = min(self.index, len(items) - 1)
        item = items[self.index]

        header = self.title
        if self.show_counter and len(items) > 1:
            counter = '{}/{}'.format(self.index + 1, len(items))
            header = _pad_between(self.title, counter)

        label = item.render_label(ctx)
        value = item.value_text(ctx)
        line = _pad_between('>' + label, value) if value else '>' + label
        # Long labels (several game modes exceed the panel width) are clipped
        # here rather than in each subclass, so any future item is safe too.
        return header[:COLS], line[:COLS]

    def on_button(self, btn, ctx):
        items = self._visible_items(ctx)
        if not items:
            return Pop() if btn == LEFT else None
        if btn == UP:
            self.index = (self.index - 1) % len(items)
        elif btn == DOWN:
            self.index = (self.index + 1) % len(items)
        elif btn in (SELECT, RIGHT):
            return items[self.index].activate(ctx)
        elif btn == LEFT:
            return Pop()
        return None


def _bar(fraction, width):
    """A [####----] gauge filling `width` columns including its brackets."""
    cells = max(1, width - 2)
    filled = max(0, min(cells, int(round(fraction * cells))))
    return '[' + '#' * filled + '-' * (cells - filled) + ']'


def _pad_between(left, right, width=COLS):
    """Left-align one string and right-align another on the same row."""
    left = str(left)
    right = str(right)
    gap = width - len(left) - len(right)
    if gap < 1:
        left = left[:max(0, width - len(right) - 1)]
        gap = max(1, width - len(left) - len(right))
    return left + ' ' * gap + right


class ActionItem(Item):
    """Fires a command onto the queue."""

    def __init__(self, label, payload, visible_when=None):
        self.label = label
        self.payload = payload
        self._visible_when = visible_when

    def activate(self, ctx):
        return Command(dict(self.payload))

    def visible(self, ctx):
        return self._visible_when(ctx) if self._visible_when else True


class SubmenuItem(Item):
    """Pushes another page. `factory` is lazy so submenus can depend on Ctx."""

    def __init__(self, label, factory):
        self.label = label
        self._factory = factory

    def activate(self, ctx):
        return Push(self._factory())


class ToggleItem(Item):
    """A boolean in ns.settings; Select flips it."""

    def __init__(self, label, key):
        self.label = label
        self.key = key

    def value_text(self, ctx):
        return 'On' if ctx.setting(self.key) else 'Off'

    def activate(self, ctx):
        return SetSetting(self.key, not bool(ctx.setting(self.key)))


class NumericItem(Item):
    """Opens a NumericPage for one numeric setting."""

    def __init__(self, label, key, unit='', preview=False):
        self.label = label
        self.key = key
        self.unit = unit
        self.preview = preview

    def value_text(self, ctx):
        return '{}{}'.format(ctx.setting(self.key), self.unit)

    def activate(self, ctx):
        return Push(NumericPage(self.label, self.key, self.unit, self.preview))


class NumericPage(Page):
    """Bar-graph adjust screen.

    Up/Down step (auto-repeating while held), Left cancels back to the value we
    opened with, Select saves. Brightness settings preview live via backlight(),
    so you are adjusting the thing you are looking at.
    """

    # 9 cells plus both brackets is 11 chars, leaving room for a space and a
    # 4-char value like "100%" or "900s" on a 16-column panel.
    BAR_WIDTH = 9

    def __init__(self, title, key, unit='', preview=False):
        self.title = title
        self.key = key
        self.unit = unit
        self.preview = preview
        self.value = None
        self._original = None

    def on_enter(self, ctx):
        self.value = self._clamp(ctx, int(ctx.setting(self.key)))
        self._original = self.value

    def _bounds(self, ctx):
        low, high, step = NUMERIC_RANGES[self.key]
        # The dim level is meaningless above the normal level, so cap it there.
        if self.key == 'lcd_idle_brightness':
            high = min(high, int(ctx.setting('lcd_brightness')))
        return low, high, step

    def _clamp(self, ctx, value):
        low, high, _ = self._bounds(ctx)
        return max(low, min(high, value))

    def render(self, ctx):
        low, high, _ = self._bounds(ctx)
        span = max(1, high - low)
        bar = _bar((self.value - low) / span, self.BAR_WIDTH + 2)
        return self.title, _pad_between(bar, '{}{}'.format(self.value, self.unit))

    def on_button(self, btn, ctx):
        _, _, step = self._bounds(ctx)
        if btn == UP:
            self.value = self._clamp(ctx, self.value + step)
        elif btn == DOWN:
            self.value = self._clamp(ctx, self.value - step)
        elif btn == LEFT:
            self.value = self._original      # cancel restores what we opened with
            return Pop()
        elif btn in (SELECT, RIGHT):
            return [SetSetting(self.key, self.value), Pop()]
        return None

    def backlight(self, ctx):
        """Live preview: only meaningful for the brightness settings."""
        if not self.preview:
            return None
        base = AMBIENT_COLORS.get(ctx.game_status, DEFAULT_AMBIENT)
        return tuple(v * self.value // 100 for v in base)


class ConfirmPage(ListPage):
    """Yes/No guard in front of a destructive action. Defaults to No."""

    # No "1/2" counter: it is noise on a two-option prompt, and it crowds out
    # the question, which is the part that matters here.
    show_counter = False

    def __init__(self, prompt, on_yes):
        # Plain Items: this page handles Select itself, and an ActionItem with
        # no payload would be a trap for anyone reusing these later.
        no, yes = Item(), Item()
        no.label, yes.label = 'No', 'Yes'
        super().__init__(prompt, [no, yes])
        self._on_yes = on_yes

    def on_button(self, btn, ctx):
        if btn == LEFT:
            return Pop()
        if btn == UP or btn == DOWN:
            self.index = 1 - self.index
            return None
        if btn in (SELECT, RIGHT):
            if self.index == 1:
                return [self._on_yes(), Pop()]
            return Pop()
        return None


class ChoicePage(ListPage):
    """Pick one from a list. Marks the current selection with a '*'."""

    def __init__(self, title, choices, current, on_choose):
        self._choices = list(choices)
        self._current = current
        self._on_choose = on_choose
        super().__init__(title, [_Choice(c, self) for c in self._choices])

    def on_enter(self, ctx):
        # Open on whatever is currently selected rather than the top.
        try:
            self.index = self._choices.index(self._current(ctx))
        except (ValueError, KeyError):
            self.index = 0


class _Choice(Item):
    def __init__(self, value, page):
        self.label = str(value)
        self.value = value
        self._page = page

    def render_label(self, ctx):
        # '*' marks the mode that is currently active. No extra padding: game
        # mode names are long and every column counts on a 16x2.
        return ('*' if self.value == self._page._current(ctx) else '') + self.label

    def activate(self, ctx):
        return [self._page._on_choose(self.value), Pop()]


# -- Home carousel -----------------------------------------------------------

class StatusPage(StaticPage):
    """Game mode and live counts.

    Renders both ns.status shapes: the menu shape (move_count/ready_count) that
    piparty publishes, and the in-game shape (total/remaining_players) that
    games/game.py publishes. Both always carry game_status and game_mode.
    """

    title = 'Status'

    def render(self, ctx):
        status = ctx.status
        mode = status.get('game_mode') or 'JoustMania'
        state = ctx.game_status

        if not status:
            return mode[:COLS], 'Starting up...'

        if state == 'menu':
            ready = status.get('ready_count', 0)
            total = status.get('move_count', 0)
            return mode[:COLS], _pad_between('Ready', '{}/{}'.format(ready, total))

        if state == 'ending':
            winner = status.get('winning_team_color') or {}
            name = winner.get('name')
            return mode[:COLS], '{} wins!'.format(name) if name else 'Game over!'

        alive = status.get('remaining_players', 0)
        total = status.get('total_players', 0)
        label = {'starting': 'Starting', 'killed': 'Killed'}.get(state, 'Alive')
        return mode[:COLS], _pad_between(label, '{}/{}'.format(alive, total))


class BatteryPage(StaticPage):
    """UPS charge. Hides itself entirely when no UPS HAT is present.

    Shows only what the gauge actually measures -- state of charge and cell
    voltage. It deliberately does not say whether the pack is charging: the
    MAX17043 has no current sense and the HAT exposes no charge-status
    register, so that could only ever be guessed at.
    """

    title = 'Battery'

    def visible(self, ctx):
        return bool(ctx.ups)

    def render(self, ctx):
        percent = ctx.ups.get('percent')
        if percent is None:
            return 'Battery', 'No reading'
        millivolts = ctx.ups.get('millivolts') or 0
        top = _pad_between('Batt {:>3.0f}%'.format(percent),
                           '{:.2f}V'.format(millivolts / 1000.0))
        level = ctx.ups.get('level', 'ok')
        if level == 'critical':
            return top, 'CRITICAL - low!'
        if level == 'warn':
            return top, 'Low battery'
        return top, _bar(percent / 100.0, COLS)


class NetworkPage(StaticPage):
    """Where to find the admin page."""

    title = 'Network'

    def render(self, ctx):
        return 'Admin page at', _local_ip() or 'no network'


class ControllersPage(StaticPage):
    """PS Move controller count and how many are charging/out."""

    title = 'Controllers'

    def render(self, ctx):
        status = ctx.status
        count = status.get('move_count', status.get('total_players', 0))
        top = _pad_between('Controllers', str(count))
        if ctx.in_game:
            return top, _pad_between('In game', str(status.get('total_players', 0)))
        return top, _pad_between('Ready', str(status.get('ready_count', 0)))


class SyncPage(Page):
    """Walks you through pairing a controller.

    Pairing itself is automatic: plugging a controller in over USB makes
    piparty write this Pi's Bluetooth address to it (Menu.pair_usb_move), flash
    it white, and record it in paired_moves. The controller then has to be
    unplugged and woken with the PS button to connect over Bluetooth.

    None of that needs a button here -- what was missing is being able to see
    it happening without squinting at controller LEDs. So this page reports
    live counts and says what to do next.
    """

    title = 'Sync'

    def render(self, ctx):
        status = ctx.status
        bt_count = status.get('bt_count', 0)
        usb_count = status.get('usb_count', 0)
        top = 'Sync  BT{} USB{}'.format(bt_count, usb_count)

        if usb_count:
            # Already written to; the controller flashes white when it takes.
            hint = 'Unplug, press PS'
        elif bt_count:
            hint = 'Or plug in USB'
        else:
            hint = 'Plug in via USB'
        return top[:COLS], hint

    def on_button(self, btn, ctx):
        if btn == LEFT:
            return Pop()
        if btn in (SELECT, RIGHT):
            return Push(ConfirmPage(
                'Reset Bluetooth?',
                lambda: Command({'command': 'lcd_reset_bt'})))
        return None


def _local_ip():
    """Best-effort LAN address, same trick webui.web_urls() uses.

    Re-implemented here rather than imported so this process does not have to
    pull in Flask.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.5)
            sock.connect(('8.8.8.8', 80))
            address = sock.getsockname()[0]
        if not address.startswith('127.'):
            return address
    except OSError:
        pass
    return None


# -- Menu construction -------------------------------------------------------

def _game_mode_page():
    # Imported lazily: common pulls in psmoveapi, which only exists on the Pi,
    # and everything else in this module stays importable for tests without it.
    from common import Games

    return ChoicePage(
        'Game Mode',
        [game.pretty_name for game in Games],
        current=lambda ctx: ctx.status.get('game_mode'),
        on_choose=lambda name: Command({'command': 'changemodestr_' + name}),
    )


def _settings_page():
    return ListPage('Settings', [
        NumericItem('Bright', 'lcd_brightness', '%', preview=True),
        NumericItem('Dim lvl', 'lcd_idle_brightness', '%', preview=True),
        NumericItem('Dim after', 'lcd_idle_dim_secs', 's'),
        ToggleItem('Ambient', 'lcd_backlight_ambient'),
        ToggleItem('Audio', 'play_audio'),
        ToggleItem('Instruct', 'play_instructions'),
        ToggleItem('ColorLock', 'color_lock'),
        ToggleItem('RandTeams', 'random_teams'),
        NumericItem('Warn at', 'ups_warn_percent', '%'),
        NumericItem('Shut at', 'ups_critical_percent', '%'),
        ToggleItem('AutoShut', 'ups_auto_shutdown'),
    ])


def _power_page():
    return ListPage('Power', [
        SubmenuItem('Shutdown', lambda: ConfirmPage(
            'Shut down?', lambda: Command({'command': 'lcd_poweroff'}))),
        SubmenuItem('Reboot', lambda: ConfirmPage(
            'Reboot?', lambda: Command({'command': 'lcd_reboot'}))),
    ])


def build_main_menu():
    return ListPage('Main Menu', [
        SubmenuItem('Game Mode', _game_mode_page),
        SubmenuItem('Sync Ctrls', SyncPage),
        ActionItem('Start Game', {'command': 'startgame'},
                   visible_when=lambda ctx: not ctx.in_game),
        ActionItem('Kill Game', {'command': 'killgame'},
                   visible_when=lambda ctx: ctx.in_game),
        SubmenuItem('Settings', _settings_page),
        SubmenuItem('Power', _power_page),
    ])


# Registries. Adding a screen is a one-liner in either of these.
def build_home_pages():
    return [StatusPage(), BatteryPage(), NetworkPage(), ControllersPage()]


class HomeCarousel(Page):
    """Bottom of the stack: Up/Down cycles home pages, Select opens the menu.

    Pages that hide themselves (BatteryPage with no UPS) drop out of the
    rotation automatically, so the no-HAT case needs no special casing.
    """

    def __init__(self, pages):
        self.pages = pages
        self.index = 0

    def _visible(self, ctx):
        return [p for p in self.pages if p.visible(ctx)] or [self.pages[0]]

    def current(self, ctx):
        pages = self._visible(ctx)
        return pages[min(self.index, len(pages) - 1)]

    def render(self, ctx):
        return self.current(ctx).render(ctx)

    def backlight(self, ctx):
        return self.current(ctx).backlight(ctx)

    def on_button(self, btn, ctx):
        pages = self._visible(ctx)
        if btn == UP:
            self.index = (self.index - 1) % len(pages)
        elif btn == DOWN:
            self.index = (self.index + 1) % len(pages)
        elif btn in (SELECT, RIGHT):
            return Push(build_main_menu())
        return None


class LowBatteryPage(Page):
    """Takes over the display when the UPS is critically low.

    Shown instead of whatever page you were on, and it swallows navigation --
    an imminent shutdown is not a good moment to be browsing menus. Select
    cancels the shutdown for this discharge cycle.
    """

    title = 'Low battery'

    def __init__(self, shutdown_at):
        self.shutdown_at = shutdown_at
        self.cancelled = False

    def render(self, ctx):
        percent = ctx.ups.get('percent')
        top = 'BATTERY {:.0f}%'.format(percent) if percent is not None else 'BATTERY LOW'
        remaining = max(0, int(round(self.shutdown_at - ctx.now)))
        return top, 'Shutdown in {:2d}s'.format(remaining)

    def on_button(self, btn, ctx):
        if btn in (SELECT, LEFT):
            self.cancelled = True
        return None

    def backlight(self, ctx):
        return CRITICAL_COLOR


# -- Navigation --------------------------------------------------------------

class PageStack:
    def __init__(self, root):
        self._stack = [root]

    def top(self):
        return self._stack[-1]

    def push(self, page, ctx):
        page.on_enter(ctx)
        self._stack.append(page)

    def pop(self, ctx):
        # Left at the bottom is a no-op: the carousel is always reachable.
        if len(self._stack) > 1:
            self._stack.pop().on_exit(ctx)

    def depth(self):
        return len(self._stack)


# -- Backlight ---------------------------------------------------------------

class Backlight:
    """Priority stack, highest wins:

      1. critical battery   red, never dimmed
      2. winner flash       winning team's color, never dimmed
      3. page override      page.backlight(), e.g. the brightness preview
      4. ambient game state
      5. idle dim           scales whatever the above chose

    Hue and brightness are separate axes: the stack picks a hue, then
    brightness is applied once, at the end.
    """

    def __init__(self, lcd):
        self.lcd = lcd
        self._last_input = time.time()
        self._flash_until = 0.0
        self._flash_color = None
        self._last_status = None
        self._blinking = False

    def note_input(self, now=None):
        self._last_input = time.time() if now is None else now

    def note_status(self, ctx):
        """Latch a winner flash on the transition into 'ending'."""
        status = ctx.game_status
        if status == 'ending' and self._last_status != 'ending':
            winner = (ctx.status.get('winning_team_color') or {}).get('rgb')
            if winner:
                self._flash_color = tuple(winner)
                self._flash_until = ctx.now + WINNER_FLASH_SECS
        self._last_status = status

    def resolve(self, ctx, page):
        """Return (rgb, brightness, blink) without touching hardware."""
        normal = int(ctx.setting('lcd_brightness'))

        if ctx.ups.get('level') == 'critical':
            return CRITICAL_COLOR, normal, True

        if ctx.now < self._flash_until and self._flash_color:
            return self._flash_color, normal, True

        override = page.backlight(ctx)
        if override is not None:
            return tuple(override), normal, False

        if ctx.setting('lcd_backlight_ambient'):
            color = AMBIENT_COLORS.get(ctx.game_status, DEFAULT_AMBIENT)
        else:
            color = DEFAULT_AMBIENT

        level = normal
        idle_after = int(ctx.setting('lcd_idle_dim_secs'))
        if idle_after > 0 and ctx.now - self._last_input >= idle_after:
            level = min(int(ctx.setting('lcd_idle_brightness')), normal)
        return color, level, False

    def update(self, ctx, page):
        color, level, blink = self.resolve(ctx, page)
        scaled = tuple(max(0, min(255, v * level // 100)) for v in color)
        try:
            if blink != self._blinking:
                self._blinking = blink
                self.lcd.blink() if blink else self.lcd.no_blink()
            self.lcd.set_rgb(*scaled)
        except (OSError, IOError) as exc:
            logger.debug("Backlight write failed: %s", exc)


# -- Run loop ----------------------------------------------------------------

class LcdApp:
    """Ties the pieces together. Owns the only mutable state in this module."""

    def __init__(self, command_queue, ns, lcd, keypad, ups=None, policy=None):
        self.command_queue = command_queue
        self.ns = ns
        self.lcd = lcd
        self.keypad = keypad
        self.ups = ups
        self.policy = policy
        self.stack = PageStack(HomeCarousel(build_home_pages()))
        self.backlight = Backlight(lcd)
        self._last_lines = None
        self._ups_state = {}
        self._next_ups_poll = 0.0
        self._low_battery_page = None
        self._shutdown_sent = False

    # -- state -------------------------------------------------------------

    def poll_ups(self, now):
        if self.ups is None or now < self._next_ups_poll:
            return
        self._next_ups_poll = now + UPS_POLL_SECS
        reading = self.ups.read()
        if not reading:
            return
        level = self.policy.update(reading)
        self._ups_state = {
            'percent': reading['percent'],
            'millivolts': reading['millivolts'],
            'level': level,
        }
        try:
            self.ns.ups_status = dict(self._ups_state)
        except Exception:
            logger.debug("Could not publish ups_status", exc_info=True)

    def sync_policy(self, ctx):
        """Track threshold changes made in the WebUI or on the LCD.

        Without this the thresholds would be frozen at process start while
        every other setting updates live, so changing them would silently
        require a restart.
        """
        if self.policy is None:
            return
        self.policy.warn_percent = float(ctx.setting('ups_warn_percent'))
        self.policy.critical_percent = float(ctx.setting('ups_critical_percent'))

    def check_battery(self, ctx):
        """Raise or clear the critical-battery takeover, and fire the shutdown.

        The countdown is deliberately visible before anything happens: this is
        the one genuinely destructive path in the LCD front-end, so it needs
        both a debounced CRITICAL from BatteryPolicy and a grace period the
        user can cancel.
        """
        critical = ctx.ups.get('level') == 'critical'

        if not critical:
            if self._low_battery_page is not None:
                logger.info("Battery recovered; cancelling shutdown")
            self._low_battery_page = None
            self._shutdown_sent = False
            return

        if self._low_battery_page is None:
            logger.warning("Battery critical at %.1f%%", ctx.ups.get('percent', -1))
            self._low_battery_page = LowBatteryPage(ctx.now + SHUTDOWN_GRACE_SECS)

        page = self._low_battery_page
        if page.cancelled or self._shutdown_sent:
            return
        if not ctx.setting('ups_auto_shutdown'):
            return
        if ctx.now >= page.shutdown_at:
            logger.warning("Battery critical; requesting poweroff")
            self._shutdown_sent = True
            self._send({'command': 'lcd_poweroff'})

    def active_page(self):
        """The critical-battery page outranks whatever is on the stack."""
        if self._low_battery_page is not None and not self._low_battery_page.cancelled:
            return self._low_battery_page
        return self.stack.top()

    def build_ctx(self, now):
        try:
            status = dict(self.ns.status or {})
        except Exception:
            status = {}
        try:
            settings = dict(self.ns.settings or {})
        except Exception:
            settings = {}
        return Ctx(status=status, settings=settings,
                   ups=dict(self._ups_state), now=now)

    # -- actions -----------------------------------------------------------

    def apply(self, action, ctx):
        if action is None:
            return
        if isinstance(action, (list, tuple)):
            for item in action:
                self.apply(item, ctx)
            return
        if isinstance(action, Push):
            self.stack.push(action.page, ctx)
        elif isinstance(action, Pop):
            self.stack.pop(ctx)
        elif isinstance(action, Command):
            self._send(action.payload)
        elif isinstance(action, SetSetting):
            # piparty owns the settings file; going through the queue keeps a
            # single writer and avoids racing the WebUI.
            self._send({'command': 'setting_update',
                        'key': action.key, 'value': action.value})
        else:
            logger.warning("Unknown action %r", action)

    def _send(self, payload):
        try:
            self.command_queue.put(payload)
        except Exception:
            logger.exception("Could not queue %r", payload)

    # -- render ------------------------------------------------------------

    def render(self, ctx):
        try:
            lines = self.active_page().render(ctx)
        except Exception:
            logger.exception("Page render failed")
            lines = ('JoustMania', 'display error')
        lines = tuple(str(line)[:COLS] for line in lines)
        if lines == self._last_lines:
            return
        self._last_lines = lines
        try:
            for row, line in enumerate(lines):
                self.lcd.write_line(row, line)
        except (OSError, IOError) as exc:
            logger.debug("LCD write failed: %s", exc)
            self._last_lines = None  # force a redraw once the bus recovers

    def tick(self, now=None):
        now = time.time() if now is None else now
        self.poll_ups(now)
        ctx = self.build_ctx(now)
        self.sync_policy(ctx)
        self.backlight.note_status(ctx)
        self.check_battery(ctx)

        if self.keypad is not None:
            for btn in self.keypad.get_events(now):
                self.backlight.note_input(now)
                try:
                    action = self.active_page().on_button(btn, ctx)
                except Exception:
                    logger.exception("Page button handler failed")
                    action = None
                self.apply(action, ctx)

        self.render(ctx)
        self.backlight.update(ctx, self.active_page())

    def run(self):
        while True:
            try:
                self.tick()
            except Exception:
                logger.exception("LCD tick failed")
            time.sleep(LOOP_SLEEP_SECS)


def _enabled(settings, key):
    """Tri-state: 'auto' probes the bus (detect() decides), on/off force it."""
    value = settings.get(key, 'auto')
    if isinstance(value, str):
        return value.strip().lower() not in ('off', 'false', 'no', '0')
    return bool(value)


def start_lcd(command_queue, ns):
    """Process entry point, spawned alongside webui.start_web.

    Every failure path here degrades to "no LCD" rather than taking the game
    down: JoustMania must run normally with no HAT attached, on Windows, and
    on the Steam Deck.
    """
    try:
        import setproctitle
        setproctitle.setproctitle("JoustMania-LCD")
    except Exception:
        pass

    try:
        settings = dict(ns.settings or {})
    except Exception:
        settings = {}

    lcd = keypad = ups = None
    bus = lcd_hat.open_bus()

    if bus is not None and _enabled(settings, 'lcd_enabled'):
        lcd, keypad = lcd_hat.detect(bus)
    if bus is not None and _enabled(settings, 'ups_enabled'):
        ups = ups_hat.detect(bus)

    if lcd is None:
        logger.info("No LCD HAT; LCD front-end not starting")
        return

    policy = ups_hat.BatteryPolicy(
        warn_percent=settings.get('ups_warn_percent', DEFAULTS['ups_warn_percent']),
        critical_percent=settings.get('ups_critical_percent',
                                      DEFAULTS['ups_critical_percent']),
    )
    logger.info("LCD front-end starting (ups=%s keypad=%s)",
                ups is not None, keypad is not None)
    LcdApp(command_queue, ns, lcd, keypad, ups, policy).run()
