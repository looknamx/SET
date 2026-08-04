import hashlib
import os
import tempfile
import time

import interception
import requests
import win32api
import win32gui
import win32process


DOWNLOAD_TIMEOUT = (10, 120)
USER_AGENT = "Mozilla/5.0 Chrome/120.0.0.0"
VIRTUAL_MOUSE_MARKERS = (
    "LDVMOUSE",
    "VIRTUAL",
    "VMWARE",
    "VBOX",
    "RDP_MOU",
    "VMBUS",
    "PARALLELS",
    "QEMU",
)


def _get_mouse_hwid(device_id):
    try:
        context = interception.set_devices.__globals__["_g_context"]
        return context.devices[device_id].get_HWID() or ""
    except (AttributeError, IndexError, KeyError):
        return ""


def _mouse_device_candidates():
    physical = []
    virtual = []
    for device_id in range(19, 9, -1):
        hwid = _get_mouse_hwid(device_id)
        if not hwid:
            continue
        destination = virtual if any(marker in hwid.upper() for marker in VIRTUAL_MOUSE_MARKERS) else physical
        destination.append(device_id)
    return physical + virtual if physical or virtual else list(range(19, 9, -1))


def configure_working_mouse_device(preferred=None):
    device_id = parse_device_id(preferred, 10, 19)
    if device_id is not None:
        interception.set_devices(mouse=device_id)
        return device_id, "manual setting"

    # Composite and virtual devices can move the Windows cursor but be ignored by games.
    candidates = _mouse_device_candidates()
    original = win32api.GetCursorPos()
    screen_width = max(2, win32api.GetSystemMetrics(0))
    screen_height = max(2, win32api.GetSystemMetrics(1))
    target = (
        original[0] + 2 if original[0] < screen_width - 3 else original[0] - 2,
        original[1] + 1 if original[1] < screen_height - 2 else original[1] - 1,
    )

    try:
        for device_id in candidates:
            try:
                interception.set_devices(mouse=device_id)
                interception.move_to(*target)
                time.sleep(0.05)
                actual = win32api.GetCursorPos()
                win32api.SetCursorPos(original)
                if abs(actual[0] - target[0]) <= 2 and abs(actual[1] - target[1]) <= 2:
                    hwid = _get_mouse_hwid(device_id).split("\0", 1)[0]
                    return device_id, f"cursor probe: {hwid or 'unknown device'}"
            except Exception:
                continue
    finally:
        win32api.SetCursorPos(original)

    interception.auto_capture_devices(keyboard=False, mouse=True)
    return interception.get_mouse(), "auto-capture fallback"


def clamp_float(value, default, min_value=None, max_value=None):
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    if min_value is not None:
        result = max(min_value, result)
    if max_value is not None:
        result = min(max_value, result)
    return result


def clamp_int(value, default, min_value=None, max_value=None):
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        result = default
    if min_value is not None:
        result = max(min_value, result)
    if max_value is not None:
        result = min(max_value, result)
    return result


def parse_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def parse_device_id(value, minimum, maximum):
    if value is None or str(value).strip().casefold() == "auto":
        return None
    try:
        device_id = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return device_id if minimum <= device_id <= maximum else None


def select_locked_target(monsters, locked_target, center, monitor_width):
    if not monsters:
        return None, False

    if locked_target is not None:
        nearest = min(
            monsters,
            key=lambda item: (item[0] - locked_target[0]) ** 2 + (item[1] - locked_target[1]) ** 2,
        )
        lock_radius = max(100, int(monitor_width * 0.15))
        distance_sq = (nearest[0] - locked_target[0]) ** 2 + (nearest[1] - locked_target[1]) ** 2
        if distance_sq <= lock_radius ** 2:
            return nearest, False

    center_x, center_y = center
    target = min(
        monsters,
        key=lambda item: ((item[0] - center_x) ** 2 + (item[1] - center_y) ** 2)
        / max(item[2], 0.1),
    )
    return target, True


def get_window_handle(title):
    if not title:
        return 0
    exact_match = win32gui.FindWindow(None, title)
    if exact_match:
        return exact_match

    matches = []
    expected = str(title).strip().casefold()

    def collect_matches(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        actual = win32gui.GetWindowText(hwnd).strip().casefold()
        if actual and expected in actual:
            matches.append(hwnd)

    win32gui.EnumWindows(collect_matches, None)
    return matches[0] if matches else 0


def is_foreground_window(hwnd):
    if not hwnd:
        return False
    foreground = win32gui.GetForegroundWindow()
    if not foreground:
        return False
    if foreground == hwnd:
        return True

    ga_root = getattr(win32gui, "GA_ROOT", 2)
    if win32gui.GetAncestor(foreground, ga_root) == win32gui.GetAncestor(hwnd, ga_root):
        return True

    _, foreground_pid = win32process.GetWindowThreadProcessId(foreground)
    _, game_pid = win32process.GetWindowThreadProcessId(hwnd)
    return bool(game_pid) and foreground_pid == game_pid


def get_window_debug_info(title):
    game_hwnd = get_window_handle(title)
    foreground = win32gui.GetForegroundWindow()
    return {
        "game_hwnd": game_hwnd,
        "game_title": win32gui.GetWindowText(game_hwnd) if game_hwnd else "",
        "foreground_hwnd": foreground,
        "foreground_title": win32gui.GetWindowText(foreground) if foreground else "",
        "active": is_foreground_window(game_hwnd),
    }


def get_safe_window_rect(title, offset_px):
    hwnd = get_window_handle(title)
    if not hwnd:
        return None
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width = (right - left) - (offset_px * 2)
    height = (bottom - top) - (offset_px * 2)
    if width <= 0 or height <= 0:
        return None
    return {
        "top": top + offset_px,
        "left": left + offset_px,
        "width": width,
        "height": height,
        "hwnd": hwnd,
    }


def game_is_active(title):
    return is_foreground_window(get_window_handle(title))


def safe_move_to(title, x, y):
    if not game_is_active(title):
        return False
    interception.move_to(int(x), int(y))
    return True


def safe_click(title, x=None, y=None, button="left", input_lock=None):
    if not game_is_active(title):
        return False
    lock = input_lock or _NullLock()
    with lock:
        interception.click(x=x, y=y, button=button, clicks=1, interval=0.05, delay=0.05)
    return True


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


def safe_press(title, key_str, key_lock):
    if not game_is_active(title):
        return False

    keys = [k.strip() for k in str(key_str).lower().split("+") if k.strip()]
    if not keys:
        return False

    pressed_keys = []
    with key_lock:
        try:
            for key in keys:
                interception.key_down(key)
                pressed_keys.append(key)
            time.sleep(0.05)
        except Exception:
            return False
        finally:
            for key in reversed(pressed_keys):
                try:
                    interception.key_up(key)
                except Exception:
                    pass
    return True


def download_file(url, destination, expected_sha256=None):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/octet-stream"}
    dest_dir = os.path.dirname(os.path.abspath(destination)) or "."
    os.makedirs(dest_dir, exist_ok=True)

    fd, temp_path = tempfile.mkstemp(prefix=".download-", suffix=".tmp", dir=dest_dir)
    os.close(fd)

    sha256 = hashlib.sha256()
    try:
        with requests.get(
            url,
            stream=True,
            headers=headers,
            allow_redirects=True,
            timeout=DOWNLOAD_TIMEOUT,
        ) as resp:
            resp.raise_for_status()
            with open(temp_path, "wb") as handle:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    sha256.update(chunk)

        actual_sha256 = sha256.hexdigest()
        if expected_sha256 and actual_sha256.lower() != expected_sha256.lower():
            raise ValueError(
                f"SHA256 mismatch for {destination}: expected {expected_sha256}, got {actual_sha256}"
            )

        os.replace(temp_path, destination)
        return actual_sha256
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise
