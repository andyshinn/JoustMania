"""The two hooks games/game.py exposes for the LCD front-end.

1. check_command_queue drains the whole queue and historically honoured only
   'killgame'. The LCD's critical-battery shutdown can fire at any moment, and
   a game can run for many minutes, so power requests have to be acted on
   immediately rather than waiting for the menu loop to regain control.
2. winner_color resolves the winning team to an RGB value, because ns.status
   carries only a team index and team_colors is not shared across processes.
"""

import queue
import sys
import types
import unittest
from unittest import mock

if 'psmoveapi' not in sys.modules:
    _stub = types.ModuleType('psmoveapi')
    _stub.Button = types.SimpleNamespace(
        TRIANGLE=1, CIRCLE=2, CROSS=4, SQUARE=8,
        SELECT=16, START=32, PS=64, MOVE=128, T=256,
    )
    sys.modules['psmoveapi'] = _stub
for name in ('numpy', 'piaudio', 'psutil'):
    if name not in sys.modules:
        try:
            __import__(name)
        except ImportError:
            stub = types.ModuleType(name)
            if name == 'piaudio':
                stub.Audio = object
            sys.modules[name] = stub

from games import game as game_module


class FakeQueue:
    """Duck-types the bits of multiprocessing.Queue that game.py uses."""

    def __init__(self, items=()):
        self._q = queue.Queue()
        for item in items:
            self._q.put(item)

    def empty(self):
        return self._q.empty()

    def get(self):
        return self._q.get()

    def put(self, item):
        self._q.put(item)

    def drain(self):
        out = []
        while not self._q.empty():
            out.append(self._q.get())
        return out


def make_game(items):
    game = game_module.Game.__new__(game_module.Game)
    game.command_queue = FakeQueue(items)
    game.kill_game = mock.Mock()
    return game


class CheckCommandQueueTest(unittest.TestCase):
    def run_with(self, items):
        game = make_game(items)
        with mock.patch.object(game_module, 'Process') as process:
            game.check_command_queue()
        return game, process

    def test_killgame_still_works(self):
        game, _ = self.run_with([{'command': 'killgame'}])
        game.kill_game.assert_called_once_with()

    def test_poweroff_is_acted_on_immediately(self):
        game, process = self.run_with([{'command': 'lcd_poweroff'}])
        process.assert_called_once()
        self.assertEqual(process.call_args.kwargs['args'], ('poweroff',))
        process.return_value.start.assert_called_once_with()

    def test_reboot_is_acted_on_immediately(self):
        _, process = self.run_with([{'command': 'lcd_reboot'}])
        self.assertEqual(process.call_args.kwargs['args'], ('reboot',))

    def test_poweroff_survives_a_later_command_in_the_same_batch(self):
        """The old code kept only the last package and dropped the rest."""
        _, process = self.run_with([
            {'command': 'lcd_poweroff'},
            {'command': 'startgame'},
        ])
        process.assert_called_once()

    def test_setting_update_is_handed_back_for_the_menu_loop(self):
        game = make_game([{'command': 'setting_update',
                           'key': 'lcd_brightness', 'value': 40}])
        with mock.patch.object(game_module, 'Process'):
            game.check_command_queue()
        self.assertEqual(game.command_queue.drain(), [
            {'command': 'setting_update', 'key': 'lcd_brightness', 'value': 40},
        ])
        game.kill_game.assert_not_called()

    def test_requeued_settings_do_not_loop_forever(self):
        game = make_game([{'command': 'setting_update', 'key': 'a', 'value': 1},
                          {'command': 'setting_update', 'key': 'b', 'value': 2}])
        with mock.patch.object(game_module, 'Process'):
            game.check_command_queue()   # must terminate
        self.assertEqual(len(game.command_queue.drain()), 2)

    def test_kill_game_not_called_without_a_command(self):
        game, _ = self.run_with([])
        game.kill_game.assert_not_called()


if __name__ == '__main__':
    unittest.main()


class WinnerColorTest(unittest.TestCase):
    """ns.status carries a team *index*; team_colors is not shared, so the
    color has to be resolved here for the LCD backlight to use it."""

    def make(self, team_colors=(), winning_moves=(), controller_colors=None):
        game = game_module.Game.__new__(game_module.Game)
        game.team_colors = list(team_colors)
        game.winning_moves = list(winning_moves)
        game.controller_colors = controller_colors or {}
        return game

    def test_resolves_a_normal_team_win(self):
        from colors import Colors
        game = self.make(team_colors=[Colors.Green, Colors.Blue])
        self.assertEqual(game.winner_color(1),
                         {'name': 'Blue', 'rgb': list(Colors.Blue.value)})

    def test_falls_back_to_a_winning_controller_color(self):
        """Zombies and Werewolf win as team -1, which has no team color."""
        game = self.make(team_colors=[], winning_moves=['aa:bb'],
                         controller_colors={'aa:bb': [12, 34, 56]})
        self.assertEqual(game.winner_color(-1),
                         {'name': None, 'rgb': [12, 34, 56]})

    def test_fallback_also_covers_an_out_of_range_index(self):
        from colors import Colors
        game = self.make(team_colors=[Colors.Green], winning_moves=['aa:bb'],
                         controller_colors={'aa:bb': [1, 2, 3]})
        self.assertEqual(game.winner_color(7)['rgb'], [1, 2, 3])

    def test_returns_none_when_there_is_no_winner(self):
        self.assertIsNone(self.make().winner_color(-1))
        self.assertIsNone(self.make().winner_color(None))

    def test_survives_a_winning_move_with_no_recorded_color(self):
        game = self.make(winning_moves=['missing'], controller_colors={})
        self.assertIsNone(game.winner_color(-1))


class CheckEndGameOrderTest(unittest.TestCase):
    def test_winning_moves_are_populated_before_status_is_published(self):
        """Otherwise the fallback above has nothing to read and the winner
        flash comes out black for Zombies/Werewolf/Tournament."""
        game = game_module.Game.__new__(game_module.Game)
        game.winning_team = -1
        game.winning_moves = []
        game.teams = {'aa:bb': -1}
        game.team_colors = []
        game.controller_colors = {'aa:bb': [9, 9, 9]}
        game.get_real_team = lambda team: team
        game.check_winner = lambda: True
        game.end_game_sound = mock.Mock()
        game.game_end = False

        seen = {}

        def capture(status, winning_team=-1):
            seen['color'] = game.winner_color(winning_team)

        game.update_status = capture
        game.check_end_game()

        self.assertEqual(seen['color'], {'name': None, 'rgb': [9, 9, 9]})
