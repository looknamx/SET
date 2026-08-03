import configparser
import os
import time


PROFILE_DEFAULTS = {
    "GENERAL": {
        "GameWindowTitle": "Ragnarok",
        "OffsetPx": "120",
        "StopOnDeath": "True",
        "YoloEnabled": "True",
        "BuffEnabled": "True",
        "PotionEnabled": "True",
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
    "HOTKEYS": {"MasterToggle": "ctrl+f1"},
    "POTIONS": {
        "0": "HP,80,f5,50,True",
        "1": "HP,20,f6,50,True",
        "2": "SP,20,f7,50,True",
    },
    "AUTO_BUFF_ITEM": {"esc": "150.0", "u": "150.0", "i": "150.0", "o": "150.0"},
}

MASTER_DEFAULTS = {"GLOBAL": {"UIScale": "1.0", "ActiveProfile": "Profile 1"}}


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
    with open(path, "w", encoding="utf-8") as handle:
        parser.write(handle)
