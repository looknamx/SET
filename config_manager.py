import configparser
import os
import shutil
import time


PROFILE_DEFAULTS = {
    "GENERAL": {
        "GameWindowTitle": "Ragnarok",
        "OffsetPx": "120",
        "StopOnDeath": "True",
        "YoloEnabled": "True",
        "BuffEnabled": "True",
        "PotionEnabled": "True",
        "SkillRotationEnabled": "False",
        "AttackClick": "False",
        "AttackIntervalSec": "0.8",
        "RareItemAction": "Log",
        "RareItemKey": "",
        "MouseDevice": "Auto",
        "KeyboardDevice": "Auto",
    },
    "AI_CONFIDENCE": {"ConfMonster": "0.4", "ConfRareItem": "0.8"},
    "TELEPORT": {
        "EnableTeleport": "False",
        "TeleportMode": "Fly Wing",
        "TeleportKey": "f8",
        "WaitTimeSec": "1.0",
        "EnableAutoTpStuck": "False",
        "AutoTpStuckSec": "10.0",
    },
    "SMART_TARGET": {
        "Enabled": "True",
        "BlacklistSec": "15.0",
        "EdgeMarginPct": "8",
    },
    "STUCK_RECOVERY": {
        "MoveBeforeTeleport": "True",
        "AttemptsBeforeTeleport": "2",
        "FailureWindowSec": "30.0",
        "ProgressDistancePx": "50",
    },
    "WATCHDOG": {
        "Enabled": "True",
        "WorkerTimeoutSec": "12.0",
        "GameMissingTimeoutSec": "30.0",
        "MaxLoopErrors": "5",
        "DiscordWebhookURL": "",
        "NotifyCooldownSec": "60.0",
    },
    "HOTKEYS": {"MasterToggle": "ctrl+f1"},
    "POTIONS": {
        "0": "HP,80,f5,50,True",
        "1": "HP,20,f6,50,True",
        "2": "SP,20,f7,50,True",
    },
    "AUTO_BUFF_ITEM": {"esc": "150.0", "u": "150.0", "i": "150.0", "o": "150.0"},
    "SKILL_ROTATION": {},
}

MASTER_DEFAULTS = {"GLOBAL": {"UIScale": "1.0", "ActiveProfile": "Profile 1"}}


def get_config_dir(app_dir, frozen=False, local_app_data=None):
    if not frozen:
        return os.path.abspath(app_dir)
    base_dir = local_app_data or os.environ.get("LOCALAPPDATA") or app_dir
    return os.path.join(os.path.abspath(base_dir), "AI-looknam-Promax")


def migrate_legacy_config(legacy_path, destination_path):
    legacy_path = os.path.abspath(legacy_path)
    destination_path = os.path.abspath(destination_path)
    if legacy_path == destination_path or os.path.exists(destination_path):
        return False
    if not os.path.isfile(legacy_path):
        return False
    os.makedirs(os.path.dirname(destination_path), exist_ok=True)
    shutil.copy2(legacy_path, destination_path)
    return True


def _load_with_defaults(path, defaults):
    parser = configparser.ConfigParser()
    backup_path = None

    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                parser.read_file(handle)
        except (configparser.Error, UnicodeError, OSError):
            backup_path = f"{path}.broken-{int(time.time())}"
            os.replace(path, backup_path)
            parser = configparser.ConfigParser()

    changed = backup_path is not None or not os.path.exists(path)
    for section, options in defaults.items():
        if not parser.has_section(section):
            parser.add_section(section)
            changed = True
        for key, value in options.items():
            if not parser.has_option(section, key):
                parser.set(section, key, value)
                changed = True

    if changed:
        save_profile(parser, path)
    return parser, backup_path


def load_profile(path):
    return _load_with_defaults(path, PROFILE_DEFAULTS)


def load_master(path):
    return _load_with_defaults(path, MASTER_DEFAULTS)


def save_profile(parser, path):
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = f"{path}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            parser.write(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
