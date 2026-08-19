import heapq
import math
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class PreflightResult:
    name: str
    passed: bool
    detail: str
    required: bool = True


ACTION_PRIORITIES = {
    "emergency_hp": 0,
    "system": 5,
    "buff": 10,
    "teleport": 20,
    "potion": 25,
    "skill": 30,
    "attack": 40,
    "move": 50,
}


@dataclass
class _ActionRequest:
    kind: str
    priority: int
    sequence: int
    cancelled: bool = False


class ActionScheduler:
    """Serializes input actions and lets higher-priority waiters run first."""

    def __init__(self, priorities=None):
        self.priorities = dict(ACTION_PRIORITIES)
        if priorities:
            self.priorities.update(priorities)
        self._condition = threading.Condition()
        self._waiting = []
        self._active = None
        self._sequence = 0
        self.last_action = None

    def _remove(self, request):
        request.cancelled = True
        self._waiting = [item for item in self._waiting if item[2] is not request]
        heapq.heapify(self._waiting)

    @contextmanager
    def claim(self, kind, timeout=1.0, cancel_event=None):
        priority = self.priorities.get(kind, self.priorities["attack"])
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._condition:
            self._sequence += 1
            request = _ActionRequest(kind, priority, self._sequence)
            heapq.heappush(self._waiting, (priority, request.sequence, request))
            acquired = False
            while not acquired:
                cancelled = request.cancelled or (
                    cancel_event is not None and cancel_event.is_set()
                )
                remaining = None if deadline is None else deadline - time.monotonic()
                if cancelled or (remaining is not None and remaining <= 0):
                    self._remove(request)
                    self._condition.notify_all()
                    break
                if self._active is None and self._waiting[0][2] is request:
                    heapq.heappop(self._waiting)
                    self._active = request
                    self.last_action = kind
                    acquired = True
                    break
                self._condition.wait(
                    0.05 if remaining is None else min(0.05, max(0.0, remaining))
                )
        try:
            yield acquired
        finally:
            if acquired:
                with self._condition:
                    if self._active is request:
                        self._active = None
                    self._condition.notify_all()

    def run(self, kind, callback, timeout=1.0, cancel_event=None):
        with self.claim(kind, timeout=timeout, cancel_event=cancel_event) as acquired:
            return callback() if acquired else False

    def reset(self):
        with self._condition:
            for _, _, request in self._waiting:
                request.cancelled = True
            self._waiting.clear()
            self._condition.notify_all()

    def snapshot(self):
        with self._condition:
            return {
                "active": self._active.kind if self._active else None,
                "waiting": [item[2].kind for item in sorted(self._waiting)],
            }


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
        lock_grace_seconds=0.4,
    ):
        self.blacklist_seconds = max(1.0, float(blacklist_seconds))
        self.edge_margin_ratio = max(0.0, min(float(edge_margin_ratio), 0.4))
        self.lock_radius_ratio = max(0.05, min(float(lock_radius_ratio), 0.5))
        self.lock_grace_seconds = max(0.0, float(lock_grace_seconds))
        self.locked_target = None
        self.lock_missing_since = None
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
        self.lock_missing_since = None

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
        lock_radius = max(100, int(width * self.lock_radius_ratio))
        if self.locked_target is not None:
            if candidates:
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
                    self.lock_missing_since = None
                    return TargetDecision(nearest, False, 0.0)
            if self.lock_missing_since is None:
                self.lock_missing_since = now
            if now - self.lock_missing_since < self.lock_grace_seconds:
                return TargetDecision(None, False)
            self.clear_lock()

        if not candidates:
            return TargetDecision(None, False)

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
        self.lock_missing_since = None
        return TargetDecision(target, True, score)


class KillConfirmationTracker:
    def __init__(self, missing_frames=3, missing_seconds=0.3, min_engagement_seconds=0.1):
        self.missing_frames_required = max(1, int(missing_frames))
        self.missing_seconds_required = max(0.0, float(missing_seconds))
        self.min_engagement_seconds = max(0.0, float(min_engagement_seconds))
        self.confirmed_count = 0
        self.reset()

    def reset(self):
        self.target = None
        self.started_at = None
        self.last_seen_at = None
        self.missing_since = None
        self.missing_frames = 0
        self.engaged = False

    def mark_engaged(self):
        if self.target is not None:
            self.engaged = True

    def observe(self, target, now=None, interrupted=False):
        now = time.monotonic() if now is None else now
        if interrupted:
            self.reset()
            return False

        if target is not None:
            if self.target is None:
                self.target = (target[0], target[1])
                self.started_at = now
                self.engaged = False
            else:
                self.target = (target[0], target[1])
            self.last_seen_at = now
            self.missing_since = None
            self.missing_frames = 0
            return False

        if self.target is None:
            return False
        if self.missing_since is None:
            self.missing_since = now
        self.missing_frames += 1
        last_seen = now if self.last_seen_at is None else self.last_seen_at
        started = now if self.started_at is None else self.started_at
        engagement_seconds = max(0.0, last_seen - started)
        confirmed = (
            self.engaged
            and engagement_seconds >= self.min_engagement_seconds
            and self.missing_frames >= self.missing_frames_required
            and now - self.missing_since >= self.missing_seconds_required
        )
        if confirmed:
            self.confirmed_count += 1
            self.reset()
            return True
        return False


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


def get_due_buffs(buff_settings, last_cast, now=None):
    now = time.monotonic() if now is None else now
    due = []
    for raw_key, raw_setting in buff_settings.items():
        key = str(raw_key).strip().lower()
        if not key:
            continue
        if isinstance(raw_setting, dict):
            cooldown = raw_setting.get("cooldown", 60.0)
            cast_delay = raw_setting.get("cast_delay", 0.5)
        elif isinstance(raw_setting, (tuple, list)):
            cooldown = raw_setting[0]
            cast_delay = raw_setting[1] if len(raw_setting) > 1 else 0.5
        else:
            cooldown = raw_setting
            cast_delay = 0.5
        cooldown = max(1.0, float(cooldown))
        cast_delay = max(0.0, float(cast_delay))
        if key not in last_cast or now - last_cast[key] >= cooldown:
            due.append((key, cooldown, cast_delay))
    return due


def select_due_buff(
    buff_settings, last_cast, now=None, last_global_cast=None, global_interval=0.5
):
    now = time.monotonic() if now is None else now
    if (
        last_global_cast is not None
        and now - last_global_cast < max(0.0, float(global_interval))
    ):
        return None
    due = get_due_buffs(buff_settings, last_cast, now)
    return due[0] if due else None
