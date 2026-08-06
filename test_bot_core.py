import configparser
import os
import tempfile
import threading
import unittest
from unittest import mock

import bot_utils
from config_manager import get_config_dir, load_profile, migrate_legacy_config, save_profile
from runtime_systems import (
    EngagementTimer,
    SmartTargetManager,
    StuckRecoveryManager,
    evaluate_worker_health,
    load_with_single_recovery,
    select_due_buff,
    select_due_skill,
    select_potion_action,
)
from updater import (
    download_app_update,
    download_validated_model,
    fetch_model_manifest,
    fetch_release_manifest,
    is_newer_version,
    launch_updater,
)


class BotCoreTests(unittest.TestCase):
    def test_buff_is_due_immediately_then_respects_cooldown(self):
        settings = {"i": 150.0, "o": 30.0}
        self.assertEqual(select_due_buff(settings, {}, now=5.0), ("i", 150.0))
        last_cast = {"i": 5.0}
        self.assertEqual(select_due_buff(settings, last_cast, now=6.0), ("o", 30.0))
        last_cast["o"] = 6.0
        self.assertIsNone(select_due_buff(settings, last_cast, now=20.0))
        self.assertEqual(select_due_buff(settings, last_cast, now=36.0), ("o", 30.0))

    def test_buff_rotation_waits_half_second_between_keys(self):
        settings = {"i": 150.0, "o": 30.0}
        last_cast = {"i": 10.0}
        self.assertIsNone(
            select_due_buff(settings, last_cast, now=10.49, last_global_cast=10.0)
        )
        self.assertEqual(
            select_due_buff(settings, last_cast, now=10.5, last_global_cast=10.0),
            ("o", 30.0),
        )

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
            self.assertTrue(profile.has_section("SMART_TARGET"))
            self.assertTrue(profile.has_section("STUCK_RECOVERY"))
            self.assertTrue(profile.has_section("WATCHDOG"))
            self.assertTrue(profile.has_section("SKILL_ROTATION"))

    def test_profile_backs_up_invalid_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "profile.ini")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("this is not an ini file")
            profile, backup = load_profile(path)
            self.assertTrue(backup and os.path.exists(backup))
            self.assertTrue(profile.has_section("GENERAL"))

    def test_frozen_config_uses_stable_local_app_data_directory(self):
        path = get_config_dir(
            "C:\\PortableBot",
            frozen=True,
            local_app_data="C:\\Users\\Test\\AppData\\Local",
        )
        self.assertEqual(
            path,
            os.path.join("C:\\Users\\Test\\AppData\\Local", "AI-looknam-Promax"),
        )

    def test_source_and_frozen_app_share_the_same_config_directory(self):
        local_app_data = "C:\\Users\\Test\\AppData\\Local"
        source_path = get_config_dir(
            "C:\\Source", frozen=False, local_app_data=local_app_data
        )
        frozen_path = get_config_dir(
            "C:\\PortableBot", frozen=True, local_app_data=local_app_data
        )
        self.assertEqual(source_path, frozen_path)

    def test_legacy_config_migrates_once_without_overwriting_saved_values(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = os.path.join(directory, "legacy", "config.ini")
            destination = os.path.join(directory, "stable", "config.ini")
            os.makedirs(os.path.dirname(legacy))
            with open(legacy, "w", encoding="utf-8") as handle:
                handle.write("old")
            self.assertTrue(migrate_legacy_config(legacy, destination))
            with open(destination, "w", encoding="utf-8") as handle:
                handle.write("saved")
            self.assertFalse(migrate_legacy_config(legacy, destination))
            with open(destination, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "saved")

    def test_profile_save_is_atomic_and_reloadable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "profile.ini")
            profile, _ = load_profile(path)
            profile["GENERAL"]["AttackClick"] = "True"
            save_profile(profile, path)
            reloaded, _ = load_profile(path)
            self.assertTrue(reloaded.getboolean("GENERAL", "AttackClick"))
            self.assertFalse(os.path.exists(f"{path}.tmp"))

    def test_deleted_user_managed_rows_are_not_restored_from_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "profile.ini")
            profile, _ = load_profile(path)
            profile.remove_section("AUTO_BUFF_ITEM")
            profile.add_section("AUTO_BUFF_ITEM")
            profile.remove_option("POTIONS", "1")
            save_profile(profile, path)

            reloaded, _ = load_profile(path)

            self.assertEqual(dict(reloaded["AUTO_BUFF_ITEM"]), {})
            self.assertNotIn("1", reloaded["POTIONS"])
            self.assertIn("0", reloaded["POTIONS"])

    def test_safe_press_releases_keys_after_error(self):
        released = []
        with mock.patch.object(bot_utils, "game_is_active", return_value=True), \
                mock.patch.object(bot_utils.interception, "key_down", side_effect=[None, RuntimeError("fail")]), \
                mock.patch.object(bot_utils.interception, "key_up", side_effect=lambda key: released.append(key)):
            success = bot_utils.safe_press("Ragnarok", "ctrl+f1", threading.Lock())
        self.assertFalse(success)
        self.assertEqual(released, ["ctrl"])

    def test_safe_press_uses_physical_scan_code_for_letter_keys(self):
        events = []
        with mock.patch.object(bot_utils, "game_is_active", return_value=True), \
                mock.patch.object(
                    bot_utils,
                    "_send_physical_alphanumeric",
                    side_effect=lambda key, key_up=False: events.append((key, key_up)) or True,
                ), \
                mock.patch.object(bot_utils.interception, "key_down") as library_key_down:
            success = bot_utils.safe_press("Ragnarok", "i", threading.Lock())
        self.assertTrue(success)
        self.assertEqual(events, [("i", False), ("i", True)])
        library_key_down.assert_not_called()

    def test_physical_letter_scan_codes_ignore_active_keyboard_layout(self):
        context = mock.Mock()
        with mock.patch.object(bot_utils.win32api, "MapVirtualKey", return_value=0x17), \
                mock.patch.dict(
                    bot_utils.interception.key_down.__globals__, {"_g_context": context}
                ):
            self.assertTrue(bot_utils._send_physical_alphanumeric("i"))
        stroke = context.send.call_args.args[1]
        self.assertEqual(stroke.code, 0x17)

    def test_target_lock_prefers_existing_target(self):
        monsters = [(110, 100, 0.5), (500, 500, 0.99)]
        target, is_new = bot_utils.select_locked_target(monsters, (100, 100), (500, 500), 800)
        self.assertEqual(target, monsters[0])
        self.assertFalse(is_new)

    def test_smart_target_avoids_edges_and_blacklisted_targets(self):
        manager = SmartTargetManager(blacklist_seconds=10)
        monsters = [(10, 10, 0.99), (500, 500, 0.75)]
        decision = manager.select(monsters, (500, 500), 1000, 1000, now=1)
        self.assertEqual(decision.target, monsters[1])
        manager.mark_failed(monsters[1], now=2)
        decision = manager.select(monsters, (500, 500), 1000, 1000, now=3)
        self.assertEqual(decision.target, monsters[0])

    def test_smart_target_blacklist_expires(self):
        manager = SmartTargetManager(blacklist_seconds=5)
        target = (500, 500, 0.9)
        manager.mark_failed(target, now=1)
        self.assertIsNone(manager.select([target], (500, 500), 1000, 800, now=2).target)
        self.assertEqual(manager.select([target], (500, 500), 1000, 800, now=7).target, target)

    def test_stuck_recovery_repositions_before_teleport(self):
        manager = StuckRecoveryManager(attempts_before_teleport=2)
        monitor = {"left": 0, "top": 0, "width": 1000, "height": 800}
        first = manager.register_failure((700, 400, 0.9), (500, 400), monitor, now=1)
        second = manager.register_failure((700, 400, 0.9), (500, 400), monitor, now=2)
        self.assertEqual(first.action, "reposition")
        self.assertLess(first.escape_point[0], 500)
        self.assertEqual(second.action, "teleport")

    def test_engagement_timeout_is_not_reset_by_continuous_target_movement(self):
        timer = EngagementTimer(missing_reset_seconds=0.5)
        self.assertEqual(timer.observe(True, now=1.0), 0.0)
        self.assertEqual(timer.observe(True, now=5.0), 4.0)
        self.assertEqual(timer.observe(True, now=11.0), 10.0)

    def test_engagement_resets_only_after_target_is_missing_long_enough(self):
        timer = EngagementTimer(missing_reset_seconds=0.5)
        timer.observe(True, now=1.0)
        timer.observe(False, now=2.0)
        self.assertAlmostEqual(timer.observe(True, now=2.2), 1.2)
        timer.observe(False, now=3.0)
        self.assertEqual(timer.observe(True, now=3.6), 0.0)

    def test_potion_priority_uses_emergency_hp_item_only(self):
        potions = [
            {"type": "HP", "pct": "80", "key": "f5", "dly": "50", "en": True},
            {"type": "HP", "pct": "20", "key": "f6", "dly": "50", "en": True},
            {"type": "SP", "pct": "30", "key": "f7", "dly": "50", "en": True},
        ]
        action = select_potion_action(potions, 10, 10, {}, now=5)
        self.assertEqual(action[1]["key"], "f6")

    def test_potion_action_respects_per_item_delay(self):
        potions = [{"type": "HP", "pct": "80", "key": "f5", "dly": "1000", "en": True}]
        self.assertIsNone(select_potion_action(potions, 50, 100, {"p_0_f5": 5}, now=5.5))
        self.assertIsNotNone(select_potion_action(potions, 50, 100, {"p_0_f5": 5}, now=6.1))

    def test_skill_rotation_respects_combat_and_sp(self):
        skills = [
            {"key": "f1", "cooldown": 2, "min_sp": 50, "target_only": True, "en": True},
            {"key": "f2", "cooldown": 1, "min_sp": 10, "target_only": False, "en": True},
        ]
        self.assertEqual(select_due_skill(skills, 20, False, {}, now=5)[1]["key"], "f2")

    def test_skill_rotation_respects_cooldown(self):
        skills = [{"key": "f1", "cooldown": 5, "min_sp": 0, "target_only": False, "en": True}]
        self.assertIsNone(select_due_skill(skills, 100, True, {"s_0_f1": 5}, now=9))
        self.assertIsNotNone(select_due_skill(skills, 100, True, {"s_0_f1": 5}, now=10))

    def test_model_loader_recovers_once_then_retries(self):
        calls = []

        def loader():
            calls.append("load")
            if calls.count("load") == 1:
                raise RuntimeError("corrupt model")
            return "model"

        model, recovered = load_with_single_recovery(
            loader, lambda error: calls.append(f"recover:{error}")
        )
        self.assertEqual(model, "model")
        self.assertTrue(recovered)
        self.assertEqual(calls, ["load", "recover:corrupt model", "load"])

    def test_watchdog_detects_stale_worker_and_error_burst(self):
        reason = evaluate_worker_health(20, {"bot": 1, "potion": 19}, {}, 10, 5)
        self.assertIn("bot worker", reason)
        reason = evaluate_worker_health(20, {"bot": 19}, {"input": 5}, 10, 5)
        self.assertIn("input reached 5 errors", reason)

    def test_device_id_parser(self):
        self.assertIsNone(bot_utils.parse_device_id("Auto", 10, 19))
        self.assertIsNone(bot_utils.parse_device_id("bad", 10, 19))
        self.assertIsNone(bot_utils.parse_device_id("20", 10, 19))
        self.assertEqual(bot_utils.parse_device_id("12", 10, 19), 12)

    def test_mouse_candidates_prefer_physical_devices(self):
        hwids = {
            11: "HID\\VID_0B05&PID_1ABE",
            15: "HID\\VID_1038&PID_182E",
            16: "LdVMouse",
        }
        with mock.patch.object(bot_utils, "_get_mouse_hwid", side_effect=lambda device: hwids.get(device, "")):
            self.assertEqual(bot_utils._mouse_device_candidates(), [15, 11, 16])

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

    def test_model_manifest_validation(self):
        response = mock.Mock()
        response.json.return_value = {
            "model_version": "2026.08.05.1",
            "download_url": "https://example.test/best.pt",
            "sha256": "c" * 64,
            "min_app_version": "2.6.2",
        }
        with mock.patch("updater.requests.get", return_value=response):
            manifest = fetch_model_manifest("https://example.test/model.json")
        self.assertEqual(manifest["model_version"], "2026.08.05.1")
        self.assertEqual(manifest["sha256"], "c" * 64)

    def test_validated_model_keeps_old_file_when_validation_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = os.path.join(directory, "best.pt")
            with open(destination, "wb") as handle:
                handle.write(b"old model")

            def fake_download(_url, candidate, _expected):
                with open(candidate, "wb") as handle:
                    handle.write(b"bad model")
                return "d" * 64

            with mock.patch("updater.download_file", side_effect=fake_download):
                with self.assertRaisesRegex(RuntimeError, "invalid model"):
                    download_validated_model(
                        "https://example.test/best.pt",
                        destination,
                        "d" * 64,
                        lambda _path: (_ for _ in ()).throw(RuntimeError("invalid model")),
                    )
            with open(destination, "rb") as handle:
                self.assertEqual(handle.read(), b"old model")

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
            self.assertIn("PYINSTALLER_RESET_ENVIRONMENT=1", script)
            self.assertLess(script.index(":wait_for_app_exit"), script.index(":install_update"))

    def test_updater_launch_resets_pyinstaller_environment(self):
        with mock.patch.dict(
            os.environ,
            {"_PYI_APPLICATION_HOME_DIR": "stale", "NORMAL_VALUE": "kept"},
            clear=True,
        ), mock.patch("updater.subprocess.Popen") as popen:
            launch_updater(os.path.join("C:\\", "bot", "updater.bat"))
        environment = popen.call_args.kwargs["env"]
        self.assertNotIn("_PYI_APPLICATION_HOME_DIR", environment)
        self.assertEqual(environment["NORMAL_VALUE"], "kept")
        self.assertEqual(environment["PYINSTALLER_RESET_ENVIRONMENT"], "1")


if __name__ == "__main__":
    unittest.main()
