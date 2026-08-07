import math
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class PreflightResult:
    name: str
    passed: bool
    detail: str
    required: bool = True


@dataclass(frozen=True)
class TargetDecision:
    target: tuple | None
    is_new: bool
    score: float | None = None


class SmartTargetManager:
    def __init__(
        self,
        blacklist_seconds=15.0,
        edge_margin_ratio=0.08,
        lock_radius_ratio=0.15,
    ):
        self.blacklist_seconds = max(1.0, float(blacklist_seconds))
        self.edge_margin_ratio = max(0.0, min(float(edge_margin_ratio), 0.4))
        self.lock_radius_ratio = max(0.05, min(float(lock_radius_ratio), 0.5))
        self.locked_target = None
        self.blacklist = []
        self._blacklist_radius = 100

    def _prune_blacklist(self, now):
        self.blacklist = [entry for entry in self.blacklist if entry[2] > now]

    def _is_blacklisted(self, target):
        return any(
            (target[0] - x) ** 2 + (target[1] - y) ** 2 <= self._blacklist_radius ** 2
            for x, y, _ in self.blacklist
        )

    def clear_lock(self):
        self.locked_target = None

    def mark_failed(self, target, now=None):
        if target is None:
            return
        now = time.monotonic() if now is None else now
        self.blacklist.append((target[0], target[1], now + self.blacklist_seconds))
        self.clear_lock()

    def select(self, monsters, center, width, height, now=None):
        now = time.monotonic() if now is None else now
        self._prune_blacklist(now)
        self._blacklist_radius = max(80, int(width * 0.08))
        candidates = [target for target in monsters if not self._is_blacklisted(target)]
        if not candidates:
            self.clear_lock()
            return TargetDecision(None, False)

        lock_radius = max(100, int(width * self.lock_radius_ratio))
        if self.locked_target is not None:
            nearest = min(
                candidates,
                key=lambda item: (item[0] - self.locked_target[0]) ** 2
                + (item[1] - self.locked_target[1]) ** 2,
            )
            distance_sq = (nearest[0] - self.locked_target[0]) ** 2 + (
                nearest[1] - self.locked_target[1]
            ) ** 2
            if distance_sq <= lock_radius ** 2:
                self.locked_target = (nearest[0], nearest[1])
                return TargetDecision(nearest, False, 0.0)

        center_x, center_y = center
        left = center_x - width / 2
        top = center_y - height / 2
        edge_x = width * self.edge_margin_ratio
        edge_y = height * self.edge_margin_ratio

        def target_score(target):
            x, y, confidence = target
            distance = math.hypot(x - center_x, y - center_y) / max(width, height, 1)
            confidence_penalty = (1.0 - max(0.0, min(confidence, 1.0))) * 0.35
            near_edge = (
                x - left < edge_x
                or left + width - x < edge_x
                or y - top < edge_y
                or top + height - y < edge_y
            )
            return distance + confidence_penalty + (0.6 if near_edge else 0.0)

        target = min(candidates, key=target_score)
        score = target_score(target)
        self.locked_target = (target[0], target[1])
        return TargetDecision(target, True, score)


@dataclass(frozen=True)
class RecoveryDecision:
    action: str
    escape_point: tuple | None = None
    recent_failures: int = 0


class StuckRecoveryManager:
    def __init__(self, attempts_before_teleport=2, failure_window_seconds=30.0):
        self.attempts_before_teleport = max(1, int(attempts_before_teleport))
        self.failure_window_seconds = max(5.0, float(failure_window_seconds))
        self.failures = deque()

    def register_failure(self, target, center, monitor, now=None):
        now = time.monotonic() if now is None else now
        while self.failures and now - self.failures[0] > self.failure_window_seconds:
            self.failures.popleft()
        self.failures.append(now)
        recent = len(self.failures)
        if recent >= self.attempts_before_teleport:
            self.failures.clear()
            return RecoveryDecision("teleport", recent_failures=recent)

        center_x, center_y = center
        dx = center_x - target[0]
        dy = center_y - target[1]
        length = math.hypot(dx, dy)
        if length < 1.0:
            dx, dy, length = 1.0, 0.0, 1.0
        step = max(60, int(min(monitor["width"], monitor["height"]) * 0.18))
        x = center_x + int(dx / length * step)
        y = center_y + int(dy / length * step)
        margin = 30
        x = max(monitor["left"] + margin, min(x, monitor["left"] + monitor["width"] - margin))
        y = max(monitor["top"] + margin, min(y, monitor["top"] + monitor["height"] - margin))
        return RecoveryDecision("reposition", (x, y), recent)


class EngagementTimer:
    def __init__(self, missing_reset_seconds=0.5):
        self.missing_reset_seconds = max(0.0, float(missing_reset_seconds))
        self.started_at = None
        self.missing_since = None

    def observe(self, has_target, now=None):
        now = time.monotonic() if now is None else now
        if has_target:
            if self.started_at is None:
                self.started_at = now
            elif (
                self.missing_since is not None
                and now - self.missing_since >= self.missing_reset_seconds
            ):
                self.started_at = now
            self.missing_since = None
            return max(0.0, now - self.started_at)

        if self.missing_since is None:
            self.missing_since = now
        return 0.0

    def reset(self):
        self.started_at = None
        self.missing_since = None

    def shift(self, seconds):
        if self.started_at is not None:
            self.started_at += seconds
        if self.missing_since is not None:
            self.missing_since += seconds


def load_with_single_recovery(loader, recovery):
    try:
        return loader(), False
    except Exception as first_error:
        recovery(first_error)
        return loader(), True


def evaluate_worker_health(now, heartbeats, error_counts, timeout_seconds, max_errors):
    for worker_name, heartbeat in heartbeats.items():
        if now - heartbeat > timeout_seconds:
            return f"{worker_name} worker has not responded for {now - heartbeat:.1f}s"
    for worker_name, error_count in error_counts.items():
        if error_count >= max_errors:
            return f"{worker_name} reached {error_count} errors in 30s"
    return None


def select_potion_action(potions, hp_percent, sp_percent, last_used, now=None):
    now = time.monotonic() if now is None else now
    eligible = []
    for index, potion in enumerate(potions):
        if not potion.get("en"):
            continue
        key = str(potion.get("key", "")).strip().lower()
        if not key:
            continue
        potion_type = potion.get("type", "HP")
        threshold = int(potion.get("pct", 50))
        value = hp_percent if potion_type == "HP" else sp_percent
        if value is None or not 2 <= value < threshold:
            continue
        delay = max(0.02, int(potion.get("dly", 50)) / 1000.0)
        tracker_key = f"p_{index}_{key}"
        if now - last_used.get(tracker_key, 0.0) < delay:
            continue
        priority = (0 if potion_type == "HP" else 1, threshold, index)
        eligible.append((priority, index, potion, tracker_key))
    if not eligible:
        return None
    _, index, potion, tracker_key = min(eligible, key=lambda item: item[0])
    return index, potion, tracker_key


def select_due_skill(skills, sp_percent, combat_active, last_cast, now=None):
    now = time.monotonic() if now is None else now
    for index, skill in enumerate(skills):
        if not skill.get("en"):
            continue
        key = str(skill.get("key", "")).strip().lower()
        if not key:
            continue
        if skill.get("target_only") and not combat_active:
            continue
        min_sp = max(0, min(int(skill.get("min_sp", 0)), 100))
        if sp_percent is None or sp_percent < min_sp:
            continue
        cooldown = max(0.1, float(skill.get("cooldown", 1.0)))
        tracker_key = f"s_{index}_{key}"
        if now - last_cast.get(tracker_key, 0.0) >= cooldown:
            return index, skill, tracker_key
    return None


def select_due_buff(
    buff_settings, last_cast, now=None, last_global_cast=None, global_interval=0.5
):
    now = time.monotonic() if now is None else now
    if (
        last_global_cast is not None
        and now - last_global_cast < max(0.0, float(global_interval))
    ):
        return None
    for raw_key, raw_cooldown in buff_settings.items():
        key = str(raw_key).strip().lower()
        if not key:
            continue
        if isinstance(raw_cooldown, dict):
            cooldown = raw_cooldown.get("cooldown", 60.0)
            cast_delay = raw_cooldown.get("cast_delay", 0.5)
        elif isinstance(raw_cooldown, (tuple, list)):
            cooldown = raw_cooldown[0]
            cast_delay = raw_cooldown[1] if len(raw_cooldown) > 1 else 0.5
        else:
            cooldown = raw_cooldown
            cast_delay = 0.5
        cooldown = max(1.0, float(cooldown))
        cast_delay = max(0.0, float(cast_delay))
        if key not in last_cast or now - last_cast[key] >= cooldown:
            return key, cooldown, cast_delay
    return None
