import configparser
import os
import tempfile
import threading
import unittest
from unittest import mock

import bot_utils
from config_manager import load_profile
from updater import download_app_update, fetch_release_manifest, is_newer_version


class BotCoreTests(unittest.TestCase):
    def test_version_comparison_handles_different_lengths(self):
        self.assertTrue(is_newer_version("2.10", "2.9.9"))
        self.assertFalse(is_newer_version("2.4.0", "2.4"))

    def test_profile_recovers_missing_sections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "profile.ini")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("[GENERAL]\nGameWindowTitle=TestRO\n")
            profile, backup = load_profile(path)
            self.assertIsNone(backup)
            self.assertEqual(profile["GENERAL"]["GameWindowTitle"], "TestRO")
            self.assertTrue(profile.has_section("TELEPORT"))
            self.assertTrue(profile.has_option("GENERAL", "AttackClick"))

    def test_profile_backs_up_invalid_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "profile.ini")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("this is not an ini file")
            profile, backup = load_profile(path)
            self.assertTrue(backup and os.path.exists(backup))
            self.assertTrue(profile.has_section("GENERAL"))

    def test_safe_press_releases_keys_after_error(self):
        released = []
        with mock.patch.object(bot_utils, "game_is_active", return_value=True), \
                mock.patch.object(bot_utils.interception, "key_down", side_effect=[None, RuntimeError("fail")]), \
                mock.patch.object(bot_utils.interception, "key_up", side_effect=lambda key: released.append(key)):
            success = bot_utils.safe_press("Ragnarok", "ctrl+f1", threading.Lock())
        self.assertFalse(success)
        self.assertEqual(released, ["ctrl"])

    def test_target_lock_prefers_existing_target(self):
        monsters = [(110, 100, 0.5), (500, 500, 0.99)]
        target, is_new = bot_utils.select_locked_target(monsters, (100, 100), (500, 500), 800)
        self.assertEqual(target, monsters[0])
        self.assertFalse(is_new)

    def test_device_id_parser(self):
        self.assertIsNone(bot_utils.parse_device_id("Auto", 10, 19))
        self.assertIsNone(bot_utils.parse_device_id("bad", 10, 19))
        self.assertIsNone(bot_utils.parse_device_id("20", 10, 19))
        self.assertEqual(bot_utils.parse_device_id("12", 10, 19), 12)

    def test_foreground_accepts_same_root_window(self):
        with mock.patch.object(bot_utils.win32gui, "GetForegroundWindow", return_value=20), \
                mock.patch.object(bot_utils.win32gui, "GetAncestor", side_effect=lambda hwnd, _: 10):
            self.assertTrue(bot_utils.is_foreground_window(11))

    def test_foreground_rejects_unrelated_process(self):
        with mock.patch.object(bot_utils.win32gui, "GetForegroundWindow", return_value=20), \
                mock.patch.object(bot_utils.win32gui, "GetAncestor", side_effect=lambda hwnd, _: hwnd), \
                mock.patch.object(bot_utils.win32process, "GetWindowThreadProcessId", side_effect=[(1, 200), (2, 100)]):
            self.assertFalse(bot_utils.is_foreground_window(10))

    def test_release_manifest_validation(self):
        response = mock.Mock()
        response.json.return_value = {
            "version": "2.4",
            "exe_sha256": "a" * 64,
            "model_sha256": "b" * 64,
        }
        with mock.patch("updater.requests.get", return_value=response):
            manifest = fetch_release_manifest("https://example.test/manifest.json")
        self.assertEqual(manifest["version"], "2.4")
        response.raise_for_status.assert_called_once()

    def test_updater_script_waits_for_exit_and_contains_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = os.path.join(directory, "bot.exe")
            with open(executable, "wb") as handle:
                handle.write(b"old")
            with mock.patch("updater.download_file", return_value="a" * 64):
                batch_path, _ = download_app_update("https://example.test/bot.exe", executable)
            with open(batch_path, "r", encoding="ascii") as handle:
                script = handle.read()
            self.assertIn("update_backup.exe", script)
            self.assertIn("if not exist", script)
            self.assertIn(":wait_for_app_exit", script)
            self.assertIn("update_wait_count% GEQ 60", script)
            self.assertLess(script.index(":wait_for_app_exit"), script.index(":install_update"))


if __name__ == "__main__":
    unittest.main()
