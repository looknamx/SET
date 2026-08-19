import argparse
import os
import threading
import time

import mss
import numpy as np
import win32api

from bot_utils import (
    configure_keyboard_device,
    configure_working_mouse_device,
    game_is_active,
    get_safe_window_rect,
    interception_driver_ready,
    safe_move_to,
    safe_press,
    safe_teleport_sequence,
)


def run_live_input_diagnostics(
    game_title,
    offset_px=120,
    keyboard_device="Auto",
    mouse_device="Auto",
    test_key=None,
    teleport_key=None,
    teleport_mode="Fly Wing",
):
    report = {"driver": interception_driver_ready()}
    if not report["driver"]:
        return report

    keyboard_id, keyboard_hwid = configure_keyboard_device(keyboard_device)
    mouse_id, mouse_detail = configure_working_mouse_device(mouse_device)
    report.update(
        keyboard_device=keyboard_id,
        keyboard_hwid=keyboard_hwid,
        mouse_device=mouse_id,
        mouse_detail=mouse_detail,
        foreground=game_is_active(game_title),
    )
    if not report["foreground"]:
        return report

    monitor = get_safe_window_rect(game_title, offset_px)
    report["capture_rect"] = monitor
    if not monitor:
        return report
    with mss.mss() as sct:
        frame = np.asarray(sct.grab(monitor))
    report["capture_shape"] = tuple(frame.shape)
    report["capture_nonblank"] = bool(frame.size and np.ptp(frame[:, :, :3]) > 0)

    original = win32api.GetCursorPos()
    target = (
        monitor["left"] + monitor["width"] // 2,
        monitor["top"] + monitor["height"] // 2,
    )
    try:
        report["mouse_move"] = safe_move_to(game_title, *target)
        time.sleep(0.1)
        actual = win32api.GetCursorPos()
        report["mouse_position_ok"] = (
            abs(actual[0] - target[0]) <= 3 and abs(actual[1] - target[1]) <= 3
        )
    finally:
        win32api.SetCursorPos(original)

    input_lock = threading.RLock()
    if test_key:
        report["keyboard_press"] = safe_press(game_title, test_key, input_lock)
    if teleport_key:
        report["teleport"] = safe_teleport_sequence(
            game_title,
            teleport_key,
            teleport_mode,
            input_lock,
        )
    return report


def main():
    parser = argparse.ArgumentParser(description="Live Ragnarok input diagnostics")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--game-title", default="Ragnarok")
    parser.add_argument("--offset", type=int, default=120)
    parser.add_argument("--keyboard-device", default="Auto")
    parser.add_argument("--mouse-device", default="Auto")
    parser.add_argument("--test-key")
    parser.add_argument("--teleport-key")
    parser.add_argument("--teleport-mode", choices=("Fly Wing", "Skill"), default="Fly Wing")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required because this test sends real input")
    print("Keep the game in the foreground. Starting live input test in 3 seconds...")
    time.sleep(3)
    report = run_live_input_diagnostics(
        args.game_title,
        args.offset,
        args.keyboard_device,
        args.mouse_device,
        args.test_key,
        args.teleport_key,
        args.teleport_mode,
    )
    for name, value in report.items():
        print(f"{name}: {value}")
    required = ("driver", "foreground", "capture_nonblank", "mouse_move", "mouse_position_ok")
    if not all(report.get(name) for name in required):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
