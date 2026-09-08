"""Tests for the LCD front-end.

Pages are pure functions of a Ctx, so essentially the whole UI is exercised
here without an LCD, an I2C bus, or a running JoustMania.
"""

import sys
import types
import unittest

# common.py imports psmoveapi, which only exists on the Pi. The LCD front-end
# only needs the Games enum out of common, so stub the binding rather than
# skipping these tests off-hardware.
if 'psmoveapi' not in sys.modules:
    _stub = types.ModuleType('psmoveapi')
    _stub.Button = types.SimpleNamespace(
        TRIANGLE=1, CIRCLE=2, CROSS=4, SQUARE=8,
        SELECT=16, START=32, PS=64, MOVE=128, T=256,
    )
    sys.modules['psmoveapi'] = _stub

import logging

import lcd_menu
from lcd_menu import (COLS, DOWN, LEFT, RIGHT, SELECT, UP, Backlight, Command,
                      ConfirmPage, Ctx, HomeCarousel, LcdApp, ListPage,
                      LowBatteryPage, NumericPage, PageStack, Pop, Push,
                      SetSetting, build_home_pages, build_main_menu)

# Several tests deliberately exercise error and critical-battery paths, which
# log loudly. Keep the test output readable.
logging.disable(logging.CRITICAL)

ALL_BUTTONS = (UP, DOWN, LEFT, RIGHT, SELECT)

MENU_STATUS = {'game_status': 'menu', 'game_mode': 'Joust Free-for-All',
               'move_count': 5, 'ready_count': 3, 'game_count': 3}
GAME_STATUS = {'game_status': 'in_game', 'game_mode': 'Zombies',
               'total_players': 8, 'remaining_players': 4}
ENDING_STATUS = {'game_status': 'ending', 'game_mode': 'Joust Teams',
                 'winning_team': 1,
                 'winning_team_color': {'name': 'Green', 'rgb': [0, 255, 0]}}

SETTINGS = {
    'lcd_brightness': 100, 'lcd_idle_brightness': 15, 'lcd_idle_dim_secs': 120,
    'lcd_backlight_ambient': True, 'play_audio': True, 'play_instructions': True,
    'color_lock': False, 'random_teams': True, 'ups_warn_percent': 20,
    'ups_critical_percent': 5, 'ups_auto_shutdown': True,
}

UPS_OK = {'percent': 87.0, 'millivolts': 4020, 'level': 'ok'}
UPS_CRITICAL = {'percent': 3.0, 'millivolts': 3400, 'level': 'critical'}


def ctx(status=None, settings=None, ups=None, now=100.0):
    return Ctx(status=dict(status or {}), settings=dict(settings or SETTINGS),
               ups=dict(ups or {}), now=now)


# Every meaningful world-state a page might be rendered in.
CTX_MATRIX = {
    'menu': ctx(MENU_STATUS, ups=UPS_OK),
    'in_game': ctx(GAME_STATUS, ups=UPS_OK),
    'ending': ctx(ENDING_STATUS, ups=UPS_OK),
    'no_ups': ctx(MENU_STATUS),
    'ups_critical': ctx(MENU_STATUS, ups=UPS_CRITICAL),
    'empty_status': ctx({}, ups=UPS_OK),
    'empty_settings': Ctx(status={}, settings={}, ups={}, now=0.0),
}


def all_pages():
    """Every registered page, plus everything reachable from the main menu."""
    pages = list(build_home_pages())
    pages.append(HomeCarousel(build_home_pages()))
    pages.append(LowBatteryPage(shutdown_at=130.0))

    menu = build_main_menu()
    pages.append(menu)
    probe = CTX_MATRIX['menu']
    for item in menu.items:
        action = item.activate(probe)
        if isinstance(action, Push):
            pages.append(action.page)
            # One more level: Settings -> NumericPage, Power -> ConfirmPage.
            for sub in getattr(action.page, 'items', []):
                sub_action = sub.activate(probe)
                if isinstance(sub_action, Push):
                    pages.append(sub_action.page)
    return pages


class PageContractTest(unittest.TestCase):
    """Applies to every registered page, including ones added later.

    A new page is covered the moment it lands in a registry, which is the
    point of keeping the registries flat lists.
    """

    def test_render_returns_two_lines_within_the_panel(self):
        for page in all_pages():
            for name, context in CTX_MATRIX.items():
                page.on_enter(context)
                with self.subTest(page=type(page).__name__, ctx=name):
                    lines = page.render(context)
                    self.assertEqual(len(lines), 2)
                    for line in lines:
                        self.assertIsInstance(line, str)
                        self.assertLessEqual(len(line), COLS)

    def test_every_button_is_handled_without_raising(self):
        for page in all_pages():
            for name, context in CTX_MATRIX.items():
                page.on_enter(context)
                for button in ALL_BUTTONS:
                    with self.subTest(page=type(page).__name__, ctx=name,
                                      button=button):
                        page.on_button(button, context)

    def test_backlight_is_none_or_a_valid_rgb_triple(self):
        for page in all_pages():
            for name, context in CTX_MATRIX.items():
                page.on_enter(context)
                with self.subTest(page=type(page).__name__, ctx=name):
                    color = page.backlight(context)
                    if color is None:
                        continue
                    self.assertEqual(len(color), 3)
                    for channel in color:
                        self.assertGreaterEqual(channel, 0)
                        self.assertLessEqual(channel, 255)


class HomePageTest(unittest.TestCase):
    def test_status_page_renders_the_menu_shape(self):
        top, bottom = lcd_menu.StatusPage().render(CTX_MATRIX['menu'])
        self.assertIn('Joust', top)
        self.assertIn('3/5', bottom)

    def test_status_page_renders_the_in_game_shape(self):
        """games/game.py publishes a different dict than piparty.py does."""
        top, bottom = lcd_menu.StatusPage().render(CTX_MATRIX['in_game'])
        self.assertIn('Zombies', top)
        self.assertIn('4/8', bottom)

    def test_status_page_names_the_winner(self):
        _, bottom = lcd_menu.StatusPage().render(CTX_MATRIX['ending'])
        self.assertIn('Green', bottom)

    def test_status_page_survives_an_empty_status(self):
        top, bottom = lcd_menu.StatusPage().render(CTX_MATRIX['empty_status'])
        self.assertTrue(top)

    def test_battery_page_hides_itself_without_a_ups(self):
        page = lcd_menu.BatteryPage()
        self.assertTrue(page.visible(CTX_MATRIX['menu']))
        self.assertFalse(page.visible(CTX_MATRIX['no_ups']))

    def test_battery_page_shows_percent_and_volts(self):
        top, bottom = lcd_menu.BatteryPage().render(CTX_MATRIX['menu'])
        self.assertIn('87%', top)
        self.assertIn('4.02V', top)
        self.assertTrue(bottom.startswith('['))

    def test_carousel_skips_hidden_pages(self):
        """With no UPS the battery page drops out of the rotation entirely."""
        carousel = HomeCarousel(build_home_pages())
        visible = carousel._visible(CTX_MATRIX['no_ups'])
        self.assertNotIn(lcd_menu.BatteryPage,
                         [type(page) for page in visible])

    def test_carousel_cycles_and_wraps(self):
        carousel = HomeCarousel(build_home_pages())
        context = CTX_MATRIX['menu']
        count = len(carousel._visible(context))
        for _ in range(count):
            carousel.on_button(DOWN, context)
        self.assertEqual(carousel.index, 0)

    def test_carousel_select_opens_the_main_menu(self):
        action = HomeCarousel(build_home_pages()).on_button(
            SELECT, CTX_MATRIX['menu'])
        self.assertIsInstance(action, Push)
        self.assertIsInstance(action.page, ListPage)


if __name__ == '__main__':
    unittest.main()


class NavigationTest(unittest.TestCase):
    def setUp(self):
        self.ctx = CTX_MATRIX['menu']
        self.stack = PageStack(HomeCarousel(build_home_pages()))

    def test_left_at_the_bottom_is_a_no_op(self):
        """The carousel must always stay reachable."""
        self.stack.pop(self.ctx)
        self.assertEqual(self.stack.depth(), 1)

    def test_push_and_pop(self):
        page = build_main_menu()
        self.stack.push(page, self.ctx)
        self.assertIs(self.stack.top(), page)
        self.stack.pop(self.ctx)
        self.assertEqual(self.stack.depth(), 1)

    def test_kill_game_only_appears_during_a_game(self):
        menu = build_main_menu()
        labels = [i.label for i in menu._visible_items(CTX_MATRIX['menu'])]
        self.assertIn('Start Game', labels)
        self.assertNotIn('Kill Game', labels)

        labels = [i.label for i in menu._visible_items(CTX_MATRIX['in_game'])]
        self.assertIn('Kill Game', labels)
        self.assertNotIn('Start Game', labels)

    def test_list_wraps_in_both_directions(self):
        menu = build_main_menu()
        count = len(menu._visible_items(self.ctx))
        menu.on_button(UP, self.ctx)
        self.assertEqual(menu.index, count - 1)
        menu.on_button(DOWN, self.ctx)
        self.assertEqual(menu.index, 0)


class ActionTest(unittest.TestCase):
    """The LCD must push exactly the commands the WebUI buttons push."""

    def activate(self, label, context):
        menu = build_main_menu()
        for item in menu._visible_items(context):
            if item.label == label:
                return item.activate(context)
        self.fail('no item labelled %r' % label)

    def test_start_game(self):
        action = self.activate('Start Game', CTX_MATRIX['menu'])
        self.assertEqual(action, Command({'command': 'startgame'}))

    def test_kill_game(self):
        action = self.activate('Kill Game', CTX_MATRIX['in_game'])
        self.assertEqual(action, Command({'command': 'killgame'}))

    def test_choosing_a_mode_sends_changemodestr(self):
        page = lcd_menu._game_mode_page()
        context = CTX_MATRIX['menu']
        page.on_enter(context)
        actions = page.items[0].activate(context)
        self.assertEqual(actions[0],
                         Command({'command': 'changemodestr_Joust Free-for-All'}))

    def test_toggle_flips_the_setting(self):
        item = lcd_menu.ToggleItem('Audio', 'play_audio')
        self.assertEqual(item.value_text(CTX_MATRIX['menu']), 'On')
        self.assertEqual(item.activate(CTX_MATRIX['menu']),
                         SetSetting('play_audio', False))

    def test_confirm_defaults_to_no_and_yes_fires(self):
        """Shutdown must never be one stray button press away."""
        page = ConfirmPage('Shut down?',
                           lambda: Command({'command': 'lcd_poweroff'}))
        context = CTX_MATRIX['menu']
        page.on_enter(context)
        self.assertEqual(page.index, 0)
        self.assertIsInstance(page.on_button(SELECT, context), Pop)

        page.on_button(DOWN, context)
        actions = page.on_button(SELECT, context)
        self.assertEqual(actions[0], Command({'command': 'lcd_poweroff'}))


class NumericPageTest(unittest.TestCase):
    def page(self, key='lcd_brightness', preview=True, context=None):
        page = NumericPage('Bright', key, '%', preview=preview)
        page.on_enter(context or CTX_MATRIX['menu'])
        return page

    def test_opens_on_the_current_value(self):
        self.assertEqual(self.page().value, 100)

    def test_steps_and_clamps_to_the_range(self):
        page = self.page()
        for _ in range(30):
            page.on_button(DOWN, CTX_MATRIX['menu'])
        self.assertEqual(page.value, 10)      # lcd_brightness floor
        for _ in range(50):
            page.on_button(UP, CTX_MATRIX['menu'])
        self.assertEqual(page.value, 100)

    def test_brightness_floor_keeps_the_screen_readable(self):
        """0% would leave no way to navigate back out of this screen."""
        low, _, _ = lcd_menu.NUMERIC_RANGES['lcd_brightness']
        self.assertGreater(low, 0)

    def test_dim_level_cannot_exceed_the_normal_level(self):
        context = ctx(MENU_STATUS, settings=dict(SETTINGS, lcd_brightness=50))
        page = self.page('lcd_idle_brightness', context=context)
        for _ in range(50):
            page.on_button(UP, context)
        self.assertEqual(page.value, 50)

    def test_dim_level_may_be_zero(self):
        low, _, _ = lcd_menu.NUMERIC_RANGES['lcd_idle_brightness']
        self.assertEqual(low, 0)

    def test_cancel_restores_the_original_value(self):
        page = self.page()
        page.on_button(DOWN, CTX_MATRIX['menu'])
        self.assertEqual(page.value, 95)
        action = page.on_button(LEFT, CTX_MATRIX['menu'])
        self.assertIsInstance(action, Pop)
        self.assertEqual(page.value, 100)

    def test_select_saves_exactly_one_setting_update(self):
        page = self.page()
        page.on_button(DOWN, CTX_MATRIX['menu'])
        actions = page.on_button(SELECT, CTX_MATRIX['menu'])
        updates = [a for a in actions if isinstance(a, SetSetting)]
        self.assertEqual(updates, [SetSetting('lcd_brightness', 95)])

    def test_preview_tracks_the_value_live(self):
        page = self.page()
        full = page.backlight(CTX_MATRIX['menu'])
        for _ in range(10):
            page.on_button(DOWN, CTX_MATRIX['menu'])
        dimmed = page.backlight(CTX_MATRIX['menu'])
        self.assertLess(sum(dimmed), sum(full))

    def test_non_preview_pages_do_not_touch_the_backlight(self):
        page = self.page('ups_warn_percent', preview=False)
        self.assertIsNone(page.backlight(CTX_MATRIX['menu']))


class FakeLcd:
    def __init__(self):
        self.lines = {}
        self.rgb = None
        self.blinking = False
        self.writes = 0

    def write_line(self, row, text):
        self.lines[row] = text
        self.writes += 1

    def set_rgb(self, r, g, b):
        self.rgb = (r, g, b)

    def blink(self, **kwargs):
        self.blinking = True

    def no_blink(self):
        self.blinking = False


class FakeQueue:
    def __init__(self):
        self.items = []

    def put(self, item):
        self.items.append(item)


class FakeNs:
    def __init__(self, status=None, settings=None):
        self.status = dict(status or MENU_STATUS)
        self.settings = dict(settings or SETTINGS)
        self.ups_status = {}


class BlankPage(lcd_menu.Page):
    def render(self, ctx):
        return '', ''


class BacklightTest(unittest.TestCase):
    def setUp(self):
        self.lcd = FakeLcd()
        self.backlight = Backlight(self.lcd)
        self.page = BlankPage()

    def resolve(self, context, page=None):
        return self.backlight.resolve(context, page or self.page)

    def test_ambient_follows_game_state(self):
        menu_color, _, _ = self.resolve(CTX_MATRIX['menu'])
        game_color, _, _ = self.resolve(CTX_MATRIX['in_game'])
        self.assertNotEqual(menu_color, game_color)
        self.assertEqual(game_color, lcd_menu.AMBIENT_COLORS['in_game'])

    def test_ambient_can_be_disabled(self):
        context = ctx(GAME_STATUS, settings=dict(SETTINGS,
                                                 lcd_backlight_ambient=False))
        color, _, _ = self.resolve(context)
        self.assertEqual(color, lcd_menu.DEFAULT_AMBIENT)

    def test_winner_flash_uses_the_winning_team_color(self):
        context = CTX_MATRIX['ending']
        self.backlight.note_status(context)
        color, _, blink = self.resolve(context)
        self.assertEqual(color, (0, 255, 0))
        self.assertTrue(blink)

    def test_winner_flash_expires(self):
        context = CTX_MATRIX['ending']
        self.backlight.note_status(context)
        later = ctx(ENDING_STATUS, ups=UPS_OK,
                    now=context.now + lcd_menu.WINNER_FLASH_SECS + 1)
        color, _, _ = self.resolve(later)
        self.assertNotEqual(color, (0, 255, 0))

    def test_winner_flash_survives_a_missing_color(self):
        """Older status dicts, or a game that resolved no winner at all."""
        context = ctx({'game_status': 'ending', 'game_mode': 'Zombies'})
        self.backlight.note_status(context)
        color, _, _ = self.resolve(context)
        self.assertIsNotNone(color)

    def test_critical_battery_outranks_the_winner_flash(self):
        ending = CTX_MATRIX['ending']
        self.backlight.note_status(ending)
        critical = ctx(ENDING_STATUS, ups=UPS_CRITICAL, now=ending.now)
        color, _, _ = self.resolve(critical)
        self.assertEqual(color, lcd_menu.CRITICAL_COLOR)

    def test_page_override_outranks_ambient(self):
        class OverridePage(BlankPage):
            def backlight(self, ctx):
                return (1, 2, 3)

        color, _, _ = self.resolve(CTX_MATRIX['menu'], OverridePage())
        self.assertEqual(color, (1, 2, 3))

    def test_critical_battery_outranks_a_page_override(self):
        class OverridePage(BlankPage):
            def backlight(self, ctx):
                return (1, 2, 3)

        color, _, _ = self.resolve(CTX_MATRIX['ups_critical'], OverridePage())
        self.assertEqual(color, lcd_menu.CRITICAL_COLOR)

    def test_dims_after_the_idle_timeout(self):
        self.backlight.note_input(now=0)
        _, level, _ = self.resolve(ctx(MENU_STATUS, now=10))
        self.assertEqual(level, 100)
        _, level, _ = self.resolve(ctx(MENU_STATUS, now=500))
        self.assertEqual(level, 15)

    def test_input_wakes_the_backlight(self):
        self.backlight.note_input(now=0)
        _, level, _ = self.resolve(ctx(MENU_STATUS, now=500))
        self.assertEqual(level, 15)
        self.backlight.note_input(now=500)
        _, level, _ = self.resolve(ctx(MENU_STATUS, now=501))
        self.assertEqual(level, 100)

    def test_zero_timeout_never_dims(self):
        self.backlight.note_input(now=0)
        context = ctx(MENU_STATUS, settings=dict(SETTINGS,
                                                 lcd_idle_dim_secs=0), now=99999)
        _, level, _ = self.resolve(context)
        self.assertEqual(level, 100)

    def test_idle_dim_never_swallows_a_warning_or_a_win(self):
        """A dying battery must be visible on a panel that has gone dim."""
        self.backlight.note_input(now=0)
        late = ctx(MENU_STATUS, ups=UPS_CRITICAL, now=99999)
        _, level, _ = self.resolve(late)
        self.assertEqual(level, 100)

        ending = ctx(ENDING_STATUS, ups=UPS_OK, now=99999)
        self.backlight.note_status(ending)
        _, level, _ = self.resolve(ending)
        self.assertEqual(level, 100)

    def test_brightness_scaling(self):
        self.backlight.update(ctx(MENU_STATUS), self.page)
        full = self.lcd.rgb
        context = ctx(MENU_STATUS, settings=dict(SETTINGS, lcd_brightness=50))
        self.backlight.update(context, self.page)
        self.assertEqual(self.lcd.rgb,
                         tuple(v * 50 // 100 for v in full))

    def test_zero_dim_level_turns_the_backlight_off(self):
        self.backlight.note_input(now=0)
        context = ctx(MENU_STATUS,
                      settings=dict(SETTINGS, lcd_idle_brightness=0), now=99999)
        self.backlight.update(context, self.page)
        self.assertEqual(self.lcd.rgb, (0, 0, 0))


class FakeUps:
    def __init__(self, readings):
        self.readings = list(readings)

    def read(self):
        return self.readings.pop(0) if self.readings else None


class LcdAppTest(unittest.TestCase):
    def build(self, status=None, settings=None, ups=None, policy=None):
        self.queue = FakeQueue()
        self.ns = FakeNs(status, settings)
        self.lcd = FakeLcd()
        return LcdApp(self.queue, self.ns, self.lcd, keypad=None,
                      ups=ups, policy=policy)

    def test_renders_only_when_the_content_changes(self):
        """The panel is slow; redrawing every tick would make it flicker."""
        app = self.build()
        app.tick(now=1.0)
        first = self.lcd.writes
        self.assertGreater(first, 0)
        app.tick(now=1.1)
        self.assertEqual(self.lcd.writes, first)

        self.ns.status = dict(MENU_STATUS, ready_count=4)
        app.tick(now=1.2)
        self.assertGreater(self.lcd.writes, first)

    def test_runs_without_a_keypad(self):
        """A failed gpiozero init leaves a read-only display, not a crash."""
        app = self.build()
        app.tick(now=1.0)
        self.assertTrue(self.lcd.lines)

    def test_survives_an_unreadable_namespace(self):
        app = self.build()

        class Exploding:
            @property
            def status(self):
                raise RuntimeError("manager died")

            @property
            def settings(self):
                raise RuntimeError("manager died")

        app.ns = Exploding()
        app.tick(now=1.0)   # must not raise

    def test_setting_update_goes_through_the_queue(self):
        """piparty owns the yaml; the LCD must not write it directly."""
        app = self.build()
        app.apply(SetSetting('lcd_brightness', 40), app.build_ctx(1.0))
        self.assertEqual(self.queue.items, [
            {'command': 'setting_update', 'key': 'lcd_brightness', 'value': 40},
        ])

    def test_commands_reach_the_queue(self):
        app = self.build()
        app.apply(Command({'command': 'startgame'}), app.build_ctx(1.0))
        self.assertEqual(self.queue.items, [{'command': 'startgame'}])

    def test_action_lists_are_applied_in_order(self):
        app = self.build()
        context = app.build_ctx(1.0)
        app.apply([Command({'command': 'a'}), Command({'command': 'b'})], context)
        self.assertEqual(self.queue.items,
                         [{'command': 'a'}, {'command': 'b'}])

    def test_ups_readings_are_published_for_the_webui(self):
        ups = FakeUps([{'percent': 55.0, 'millivolts': 3900}])
        app = self.build(ups=ups, policy=lcd_menu.ups_hat.BatteryPolicy())
        app.tick(now=1.0)
        self.assertEqual(self.ns.ups_status['percent'], 55.0)
        self.assertEqual(self.ns.ups_status['level'], 'ok')

    def test_a_page_that_raises_does_not_kill_the_process(self):
        app = self.build()

        class BadPage(lcd_menu.Page):
            def render(self, ctx):
                raise ValueError("boom")

        app.stack.push(BadPage(), app.build_ctx(1.0))
        app.tick(now=1.0)
        self.assertIn('error', self.lcd.lines[1])


class AutoShutdownTest(unittest.TestCase):
    """The one destructive path, so it gets its own tests."""

    def build(self, settings=None):
        self.queue = FakeQueue()
        self.ns = FakeNs(settings=settings)
        self.lcd = FakeLcd()
        app = LcdApp(self.queue, self.ns, self.lcd, keypad=None)
        return app

    def critical_ctx(self, now, settings=None):
        return ctx(MENU_STATUS, settings=settings or SETTINGS,
                   ups=UPS_CRITICAL, now=now)

    def test_warns_before_it_acts(self):
        app = self.build()
        app.check_battery(self.critical_ctx(now=0))
        self.assertIsInstance(app.active_page(), LowBatteryPage)
        self.assertEqual(self.queue.items, [])

    def test_fires_after_the_grace_period(self):
        app = self.build()
        app.check_battery(self.critical_ctx(now=0))
        app.check_battery(self.critical_ctx(now=lcd_menu.SHUTDOWN_GRACE_SECS + 1))
        self.assertEqual(self.queue.items, [{'command': 'lcd_poweroff'}])

    def test_fires_only_once(self):
        app = self.build()
        app.check_battery(self.critical_ctx(now=0))
        for now in range(31, 40):
            app.check_battery(self.critical_ctx(now=now))
        self.assertEqual(len(self.queue.items), 1)

    def test_select_cancels_the_shutdown(self):
        app = self.build()
        context = self.critical_ctx(now=0)
        app.check_battery(context)
        app.active_page().on_button(SELECT, context)
        app.check_battery(self.critical_ctx(now=100))
        self.assertEqual(self.queue.items, [])

    def test_disabled_by_setting(self):
        settings = dict(SETTINGS, ups_auto_shutdown=False)
        app = self.build(settings)
        app.check_battery(self.critical_ctx(now=0, settings=settings))
        app.check_battery(self.critical_ctx(now=100, settings=settings))
        self.assertEqual(self.queue.items, [])
        # The warning is still shown -- only the shutdown is suppressed.
        self.assertIsInstance(app.active_page(), LowBatteryPage)

    def test_recovery_clears_the_warning(self):
        app = self.build()
        app.check_battery(self.critical_ctx(now=0))
        app.check_battery(ctx(MENU_STATUS, ups=UPS_OK, now=5))
        self.assertNotIsInstance(app.active_page(), LowBatteryPage)
        self.assertEqual(self.queue.items, [])

    def test_takeover_page_swallows_navigation(self):
        """Not a good moment to be browsing menus."""
        app = self.build()
        context = self.critical_ctx(now=0)
        app.check_battery(context)
        page = app.active_page()
        self.assertIsNone(page.on_button(DOWN, context))
        self.assertIsNone(page.on_button(RIGHT, context))


class SettingsAllowListTest(unittest.TestCase):
    """piparty.initialize_settings() drops any key not in REQUIRED_SETTINGS.

    Without this test a new lcd_*/ups_* setting silently reverts to its default
    on the next restart, which is a miserable thing to debug by hand.
    """

    def required(self):
        import common
        return set(common.REQUIRED_SETTINGS)

    def test_every_setting_the_lcd_reads_is_allow_listed(self):
        required = self.required()
        for key in lcd_menu.DEFAULTS:
            with self.subTest(key=key):
                self.assertIn(key, required)

    def test_every_editable_numeric_is_allow_listed(self):
        required = self.required()
        for key in lcd_menu.NUMERIC_RANGES:
            with self.subTest(key=key):
                self.assertIn(key, required)

    def test_enable_flags_are_allow_listed(self):
        required = self.required()
        self.assertIn('lcd_enabled', required)
        self.assertIn('ups_enabled', required)


class NumericPageRenderTest(unittest.TestCase):
    """The bar must never be clipped -- a missing ']' looks like a glitch."""

    def render(self, key, value, context=None):
        context = context or CTX_MATRIX['menu']
        page = NumericPage('Bright', key, '%' if 'percent' in key
                           or 'brightness' in key else 's')
        page.on_enter(context)
        page.value = value
        return page.render(context)

    def test_bar_is_complete_at_both_extremes(self):
        for key, values in (('lcd_brightness', (10, 100)),
                            ('lcd_idle_brightness', (0, 100)),
                            ('ups_warn_percent', (5, 50)),
                            ('ups_critical_percent', (1, 25))):
            for value in values:
                with self.subTest(key=key, value=value):
                    _, line = self.render(key, value)
                    self.assertLessEqual(len(line), COLS)
                    self.assertTrue(line.startswith('['))
                    self.assertIn(']', line)
                    self.assertIn(str(value), line)

    def test_longest_possible_value_still_fits(self):
        page = NumericPage('Dim after', 'lcd_idle_dim_secs', 's')
        page.on_enter(CTX_MATRIX['menu'])
        page.value = 900
        _, line = page.render(CTX_MATRIX['menu'])
        self.assertLessEqual(len(line), COLS)
        self.assertIn(']', line)
        self.assertIn('900s', line)

    def test_bar_fills_proportionally(self):
        empty = self.render('lcd_idle_brightness', 0)[1]
        full = self.render('lcd_idle_brightness', 100)[1]
        self.assertEqual(empty.count('#'), 0)
        self.assertEqual(full.count('-'), 0)


class PolicySyncTest(unittest.TestCase):
    """Thresholds must track settings changes without a restart, the same way
    brightness does."""

    def build(self, settings):
        ns = FakeNs(settings=settings)
        policy = lcd_menu.ups_hat.BatteryPolicy(warn_percent=20,
                                                critical_percent=5)
        app = LcdApp(FakeQueue(), ns, FakeLcd(), keypad=None,
                     ups=None, policy=policy)
        return app, policy

    def test_thresholds_follow_the_settings(self):
        settings = dict(SETTINGS, ups_warn_percent=40, ups_critical_percent=12)
        app, policy = self.build(settings)
        app.tick(now=1.0)
        self.assertEqual(policy.warn_percent, 40.0)
        self.assertEqual(policy.critical_percent, 12.0)

    def test_no_policy_is_not_an_error(self):
        app = LcdApp(FakeQueue(), FakeNs(), FakeLcd(), keypad=None)
        app.tick(now=1.0)   # must not raise


class BatteryLabelTest(unittest.TestCase):
    """The page reports only what the gauge measures.

    It used to try to infer charging from a voltage trend and got it wrong --
    reporting "On battery" while plugged in. The MAX17043 has no current sense
    and the HAT exposes no charge-status register, so that was never reliably
    knowable and the display no longer claims it.
    """

    def render(self, **kwargs):
        data = {'percent': 70.0, 'millivolts': 3850, 'level': 'ok'}
        data.update(kwargs)
        return lcd_menu.BatteryPage().render(ctx(MENU_STATUS, ups=data))

    def test_shows_charge_and_voltage(self):
        top, _ = self.render(percent=72.3, millivolts=3855)
        self.assertIn('72%', top)
        self.assertIn('3.85V', top)

    def test_makes_no_claim_about_charging(self):
        for millivolts in (3580, 3855, 4075, 4200):
            top, bottom = self.render(millivolts=millivolts)
            joined = (top + bottom).lower()
            for word in ('charg', 'battery ok', 'on battery'):
                with self.subTest(millivolts=millivolts, word=word):
                    self.assertNotIn(word, joined)

    def test_second_line_is_a_gauge(self):
        _, bottom = self.render(percent=100.0)
        self.assertEqual(bottom, '[' + '#' * (COLS - 2) + ']')
        _, bottom = self.render(percent=0.0)
        self.assertEqual(bottom, '[' + '-' * (COLS - 2) + ']')

    def test_warnings_replace_the_gauge(self):
        self.assertEqual(self.render(level='warn')[1], 'Low battery')
        self.assertEqual(self.render(level='critical')[1], 'CRITICAL - low!')

    def test_handles_a_missing_reading(self):
        page = lcd_menu.BatteryPage()
        top, bottom = page.render(ctx(MENU_STATUS, ups={'level': 'ok'}))
        self.assertEqual(bottom, 'No reading')

    def test_every_state_fits_the_panel(self):
        for level in ('ok', 'warn', 'critical'):
            for percent in (0.0, 50.0, 100.0):
                top, bottom = self.render(level=level, percent=percent)
                self.assertLessEqual(len(top), COLS)
                self.assertLessEqual(len(bottom), COLS)


if __name__ == '__main__':
    unittest.main()


class SyncPageTest(unittest.TestCase):
    """Pairing is automatic; the page's job is to show it working and say
    what to do next."""

    def render(self, **counts):
        status = dict(MENU_STATUS, **counts)
        return lcd_menu.SyncPage().render(ctx(status))

    def test_prompts_for_usb_when_nothing_is_connected(self):
        top, hint = self.render(bt_count=0, usb_count=0)
        self.assertIn('BT0', top)
        self.assertIn('USB', hint)

    def test_prompts_to_unplug_once_a_controller_is_on_usb(self):
        """USB means piparty has written the host address; the next step is
        to unplug and wake it over Bluetooth."""
        _, hint = self.render(bt_count=0, usb_count=1)
        self.assertIn('Unplug', hint)
        self.assertIn('PS', hint)

    def test_shows_live_counts(self):
        top, _ = self.render(bt_count=3, usb_count=1)
        self.assertIn('BT3', top)
        self.assertIn('USB1', top)

    def test_select_guards_the_bluetooth_reset(self):
        """The reset stops and restarts JoustMania, so it needs a confirm."""
        action = lcd_menu.SyncPage().on_button(SELECT, CTX_MATRIX['menu'])
        self.assertIsInstance(action, Push)
        self.assertIsInstance(action.page, ConfirmPage)

        confirm = action.page
        confirm.on_enter(CTX_MATRIX['menu'])
        self.assertEqual(confirm.index, 0)                    # defaults to No
        self.assertIsInstance(confirm.on_button(SELECT, CTX_MATRIX['menu']), Pop)

        confirm.on_button(DOWN, CTX_MATRIX['menu'])
        actions = confirm.on_button(SELECT, CTX_MATRIX['menu'])
        self.assertEqual(actions[0], Command({'command': 'lcd_reset_bt'}))

    def test_left_goes_back(self):
        self.assertIsInstance(
            lcd_menu.SyncPage().on_button(LEFT, CTX_MATRIX['menu']), Pop)

    def test_is_reachable_from_the_main_menu(self):
        menu = build_main_menu()
        labels = [i.label for i in menu._visible_items(CTX_MATRIX['menu'])]
        self.assertIn('Sync Ctrls', labels)

    def test_survives_the_in_game_status_shape(self):
        """games/game.py does not publish pairing counts."""
        top, hint = lcd_menu.SyncPage().render(CTX_MATRIX['in_game'])
        self.assertIn('BT0', top)
        self.assertTrue(hint)

    def test_fits_the_panel_with_large_counts(self):
        for bt, usb in ((0, 0), (9, 9), (12, 4), (99, 99)):
            top, hint = self.render(bt_count=bt, usb_count=usb)
            self.assertLessEqual(len(top), COLS)
            self.assertLessEqual(len(hint), COLS)


class ConfirmPageRenderTest(unittest.TestCase):
    def test_question_is_not_crowded_out_by_a_counter(self):
        """A '1/2' counter would truncate 'Reset Bluetooth?' to 'Reset Blueto'."""
        page = ConfirmPage('Reset Bluetooth?', lambda: None)
        page.on_enter(CTX_MATRIX['menu'])
        top, bottom = page.render(CTX_MATRIX['menu'])
        self.assertEqual(top, 'Reset Bluetooth?')
        self.assertEqual(bottom, '>No')

    def test_menus_still_show_their_counter(self):
        menu = build_main_menu()
        menu.on_enter(CTX_MATRIX['menu'])
        top, _ = menu.render(CTX_MATRIX['menu'])
        self.assertIn('/', top)
