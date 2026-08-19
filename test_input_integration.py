import os
import threading
import unittest
from unittest import mock

import bot_utils
from input_diagnostics import run_live_input_diagnostics


class SimulatedInputIntegrationTests(unittest.TestCase):
    def test_keyboard_mouse_click_and_skill_teleport_share_input_pipeline(self):
        events = []
        input_lock = threading.RLock()

        with mock.patch.object(bot_utils, "game_is_active", return_value=True), \
                mock.patch.object(
                    bot_utils,
                    "_key_down_layout_independent",
                    side_effect=lambda key: events.append(("down", key)),
                ), \
                mock.patch.object(
                    bot_utils,
                    "_key_up_layout_independent",
                    side_effect=lambda key: events.append(("up", key)),
                ), \
                mock.patch.object(
                    bot_utils.interception,
                    "move_to",
                    side_effect=lambda x, y: events.append(("move", x, y)),
                ), \
                mock.patch.object(
                    bot_utils.interception,
                    "click",
                    side_effect=lambda **kwargs: events.append(("click", kwargs["x"], kwargs["y"])),
                ):
            self.assertTrue(bot_utils.safe_press("Ragnarok", "i", input_lock))
            self.assertTrue(bot_utils.safe_move_to("Ragnarok", 400, 300))
            self.assertTrue(bot_utils.safe_click("Ragnarok", 400, 300, input_lock=input_lock))
            self.assertTrue(
                bot_utils.safe_teleport_sequence(
                    "Ragnarok", "f8", "Skill", input_lock, confirm_delay=0
                )
            )

        self.assertIn(("down", "i"), events)
        self.assertIn(("move", 400, 300), events)
        self.assertIn(("click", 400, 300), events)
        teleport_events = [event for event in events if event in {
            ("down", "f8"), ("up", "f8"), ("down", "enter"), ("up", "enter")
        }]
        self.assertEqual(
            teleport_events,
            [("down", "f8"), ("up", "f8"), ("down", "enter"), ("up", "enter")],
        )

    def test_cancelled_skill_teleport_does_not_send_enter(self):
        cancel = threading.Event()
        keys = []

        def wait_and_cancel(_seconds):
            cancel.set()
            return True

        cancel.wait = wait_and_cancel
        with mock.patch.object(bot_utils, "safe_press", side_effect=lambda _title, key, _lock: keys.append(key) or True):
            self.assertFalse(
                bot_utils.safe_teleport_sequence(
                    "Ragnarok", "f8", "Skill", threading.RLock(), cancel_event=cancel
                )
            )
        self.assertEqual(keys, ["f8"])


@unittest.skipUnless(
    os.environ.get("BOT_LIVE_INPUT_TEST") == "1",
    "set BOT_LIVE_INPUT_TEST=1 to send input to the foreground game",
)
class LiveInputIntegrationTests(unittest.TestCase):
    def test_live_driver_capture_mouse_keyboard_and_optional_teleport(self):
        report = run_live_input_diagnostics(
            os.environ.get("BOT_TEST_GAME_TITLE", "Ragnarok"),
            int(os.environ.get("BOT_TEST_OFFSET", "120")),
            os.environ.get("BOT_TEST_KEYBOARD_DEVICE", "Auto"),
            os.environ.get("BOT_TEST_MOUSE_DEVICE", "Auto"),
            os.environ.get("BOT_TEST_KEY"),
            os.environ.get("BOT_LIVE_TELEPORT_KEY"),
            os.environ.get("BOT_LIVE_TELEPORT_MODE", "Fly Wing"),
        )
        for field in ("driver", "foreground", "capture_nonblank", "mouse_move", "mouse_position_ok"):
            self.assertTrue(report.get(field), f"{field} failed: {report}")
        if os.environ.get("BOT_TEST_KEY"):
            self.assertTrue(report.get("keyboard_press"), report)
        if os.environ.get("BOT_LIVE_TELEPORT_KEY"):
            self.assertTrue(report.get("teleport"), report)


if __name__ == "__main__":
    unittest.main()
