"""通用时间能力（PRD 3.1 / 6.5，FR-T-01~04）。

设计要点：
  - 展示/日志/文件名用墙上时间（wall clock），计时/超时/ETA 用单调时钟，二者分离，
    修改系统时间不会影响任务计时与超时判定。
  - TimeGuard 采用「可信时钟锚点 + 单调性校验 + 累计运行时长 + HMAC 完整性」分层缓解，
    并如实说明纯离线环境无法 100% 防篡改（PRD 6.5.4 / R-07）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

TS_FORMAT = "%Y-%m-%d %H:%M:%S"
STAMP_FORMAT = "%Y%m%d-%H%M%S"

CLOCK = None  # 单例在下方初始化


class ClockService:
    """全端统一的时钟抽象；业务层禁止直接调用系统时间 API。"""

    def wall_now(self) -> datetime:
        return datetime.now().astimezone()

    def utc_now(self) -> datetime:
        return datetime.now(timezone.utc)

    def mono(self) -> float:
        return time.monotonic()

    def fmt(self, dt: datetime | None = None) -> str:
        return (dt or self.wall_now()).strftime(TS_FORMAT)

    def stamp(self) -> str:
        return self.wall_now().strftime(STAMP_FORMAT)

    def iso(self, dt: datetime | None = None) -> str:
        return (dt or self.wall_now()).isoformat(timespec="seconds")

    def unique_path(self, directory: Path, base: str, ext: str) -> Path:
        """FR-T-01：文件名自动追加时间戳，同名冲突递增序号，绝不覆盖。"""
        directory.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in base).strip("_") or "pack"
        stamp = self.stamp()
        with _NAME_LOCK:
            candidate = directory / f"{safe}_{stamp}.{ext}"
            n = 1
            while candidate.exists():
                candidate = directory / f"{safe}_{stamp}_{n}.{ext}"
                n += 1
            # 占位，防并发重名
            candidate.touch()
        return candidate


_NAME_LOCK = threading.Lock()
CLOCK = ClockService()


class TimeGuard:
    """FR-T-04：时间异常/篡改检测与授权风险分级。"""

    _DEVICE_KEY = b"mc-jb2vv-local-device-key-v1"  # 生产环境应改为平台密钥链

    def __init__(self, state_path: Path):
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._session_mono = CLOCK.mono()
        self._last_accum_mono = self._session_mono
        self._lock = threading.Lock()
        self._state = self._load()

    def _sign(self, payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        return hmac.new(self._DEVICE_KEY, raw, hashlib.sha256).hexdigest()

    def _load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {
                "installed_at": CLOCK.iso(),
                "last_wall": None,
                "uptime_total_s": 0.0,
                "risk_events": [],
            }
        try:
            obj = json.loads(self.state_path.read_text(encoding="utf-8"))
            sig = obj.pop("signature", "")
            if not hmac.compare_digest(sig, self._sign(obj)):
                obj["risk_events"] = list(obj.get("risk_events", [])) + [
                    f"{CLOCK.fmt()} ANCHOR_TAMPERED 可信锚点签名校验失败"
                ]
            return obj
        except (ValueError, OSError):
            return {
                "installed_at": CLOCK.iso(),
                "last_wall": None,
                "uptime_total_s": 0.0,
                "risk_events": [f"{CLOCK.fmt()} ANCHOR_RESET 锚点不可读，已重建"],
            }

    def _save(self) -> None:
        obj = dict(self._state)
        obj["signature"] = self._sign(obj)
        self.state_path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

    def check(self) -> dict[str, Any]:
        """返回时钟健康状态；不阻断用户数据，仅做风险分级（PRD 6.5.4）。"""
        with self._lock:
            now = CLOCK.wall_now()
            events: list[str] = []
            level = "OK"
            last_raw = self._state.get("last_wall")
            if last_raw:
                try:
                    last = datetime.fromisoformat(last_raw)
                    delta = (now - last).total_seconds()
                    if delta < -60:
                        level = "RISK"
                        events.append(f"{CLOCK.fmt(now)} CLOCK_ROLLBACK 检测到系统时间回拨 {abs(delta):.0f}s")
                    elif delta > 365 * 86400:
                        level = "WARNING"
                        events.append(f"{CLOCK.fmt(now)} CLOCK_JUMP 时间异常前跳 {delta / 86400:.0f} 天")
                except ValueError:
                    events.append(f"{CLOCK.fmt(now)} ANCHOR_PARSE 上次时间不可解析")

            # 累计运行时长：以单调时钟增量累加，回拨系统时间无法延长授权
            mono_now = CLOCK.mono()
            self._state["uptime_total_s"] = float(self._state.get("uptime_total_s", 0.0)) + max(
                0.0, mono_now - self._last_accum_mono
            )
            self._last_accum_mono = mono_now
            self._state["last_wall"] = now.isoformat(timespec="seconds")
            self._state["risk_events"] = (list(self._state.get("risk_events", [])) + events)[-50:]
            self._save()

            return {
                "status": level,
                "now": CLOCK.fmt(now),
                "utc": CLOCK.utc_now().strftime(TS_FORMAT) + " UTC",
                "timezone": str(now.tzinfo),
                "installed_at": self._state.get("installed_at", ""),
                "uptime_total_s": round(self._state["uptime_total_s"], 1),
                "events": events,
                "recent_events": self._state["risk_events"][-5:],
                "note": "纯离线环境无法 100% 防御时间篡改；本方案为多层风险缓解（PRD 6.5.4 / R-07）。",
            }


class TimeoutGuard:
    """FR-T-03：分级超时判定，全部基于单调时钟。"""

    STAGES = {
        "parse": "文件/ZIP 解析",
        "image": "单张贴图处理",
        "anim": "动画/JSON 生成",
        "export": "导出打包",
        "task": "整任务",
    }

    def __init__(self, limits: dict[str, float]):
        self.limits = dict(limits)

    def limit(self, stage: str) -> float:
        return float(self.limits.get(stage, 60.0))

    def expired(self, stage: str, started_mono: float) -> bool:
        return (CLOCK.mono() - started_mono) > self.limit(stage)

    def remaining(self, stage: str, started_mono: float) -> float:
        return max(0.0, self.limit(stage) - (CLOCK.mono() - started_mono))

    def describe(self) -> dict[str, Any]:
        return {k: {"label": v, "seconds": self.limit(k)} for k, v in self.STAGES.items()}


class ProgressTimer:
    """FR-T-02：已消耗时长 + 滚动加权 ETA。"""

    def __init__(self) -> None:
        self.start_mono = CLOCK.mono()
        self.start_wall = CLOCK.wall_now()
        self._window: list[float] = []
        self._lock = threading.Lock()

    def elapsed(self) -> float:
        return CLOCK.mono() - self.start_mono

    def elapsed_text(self) -> str:
        return _hms(self.elapsed())

    def record(self, duration_s: float) -> None:
        with self._lock:
            self._window.append(max(0.0001, duration_s))
            if len(self._window) > 20:
                self._window.pop(0)

    def eta(self, done: int, total: int) -> float:
        if done >= total or not self._window:
            return 0.0
        with self._lock:
            window = list(self._window)
        # 近期样本权重更高
        weights = [i + 1 for i in range(len(window))]
        avg = sum(w * v for w, v in zip(weights, window)) / sum(weights)
        return avg * (total - done)

    def started_at(self) -> str:
        return CLOCK.fmt(self.start_wall)


def _hms(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def hms(seconds: float) -> str:
    return _hms(seconds)


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, TS_FORMAT)
    except ValueError:
        return None
