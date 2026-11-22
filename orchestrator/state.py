"""Persistent global orchestrator state with v1 migration and harness fallback chain."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any, Dict, List, Optional

from harness.types import HarnessProfile, normalize_permission_preset

from .storage import get_data_dir

DEFAULT_FALLBACK_CHAIN: List[Dict[str, str]] = [
    {"harness": "agy", "model": "default"},
    {"harness": "agy", "model": "gemini-3.8-flash"},
    {"harness": "agy", "model": "gemini-3.1-pro"},
    {"harness": "agy", "model": "gemini-3.7-flash"},
]

# Backwards-compatible list of model names matching DEFAULT_FALLBACK_CHAIN.
MODEL_NAMES: List[str] = [entry["model"] for entry in DEFAULT_FALLBACK_CHAIN]

BACKOFF_SCHEDULE_SECONDS: List[int] = [120, 300, 600]


def _normalize_fallback_chain(raw_chain: Any) -> List[Dict[str, str]]:
    """Validate and normalize a fallback chain list."""
    if not isinstance(raw_chain, list):
        return [dict(item) for item in DEFAULT_FALLBACK_CHAIN]

    normalized: List[Dict[str, str]] = []
    for entry in raw_chain:
        if isinstance(entry, dict) and entry.get("harness"):
            normalized.append(
                {
                    "harness": str(entry["harness"]).strip().lower(),
                    "model": str(entry.get("model") or "default").strip(),
                }
            )
    return normalized or [dict(item) for item in DEFAULT_FALLBACK_CHAIN]


class SystemState:
    """Thread-safe, cross-process persistent state backed by DATA_DIR/system_state.json."""

    def __init__(self, state_file: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        self._explicit_state_file = state_file
        self._load()

    @property
    def _state_file(self) -> str:
        if self._explicit_state_file:
            return self._explicit_state_file
        return os.path.join(get_data_dir(), "system_state.json")

    @_state_file.setter
    def _state_file(self, path: str) -> None:
        self._explicit_state_file = path

    def _apply_defaults(self) -> None:
        self.fallback_chain: List[Dict[str, str]] = [dict(item) for item in DEFAULT_FALLBACK_CHAIN]
        self.model_index: int = 0
        self.default_harness: str = os.environ.get("DEFAULT_HARNESS", "agy").strip().lower() or "agy"
        self.default_model: str = os.environ.get("DEFAULT_MODEL", self.fallback_chain[0]["model"]).strip() or "default"
        self.default_effort: str = os.environ.get("DEFAULT_EFFORT", "high").strip().lower() or "high"
        self.default_permission_preset: str = normalize_permission_preset(
            os.environ.get("DEFAULT_PERMISSION_PRESET", "auto")
        ).value
        self.interval_seconds: int = 5
        self.backoff_level: int = 0
        self.start_time: str = "Unknown"
        self.watcher_paused: bool = False
        self.paused_projects: List[str] = []

    def _load(self) -> None:
        self._apply_defaults()
        state_file = self._state_file
        if not os.path.exists(state_file):
            return

        try:
            with open(state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return

            self.fallback_chain = _normalize_fallback_chain(data.get("fallback_chain"))
            raw_index = int(data.get("model_index", 0) or 0)
            if not (0 <= raw_index < len(self.fallback_chain)):
                raw_index = 0
            self.model_index = raw_index

            is_legacy_v1 = "default_harness" not in data and "fallback_chain" not in data
            if is_legacy_v1:
                # Lossless migration from v1 Gemini-only state file: adopt agy defaults
                self.default_harness = os.environ.get("DEFAULT_HARNESS", "agy").strip().lower() or "agy"
                self.default_model = self.fallback_chain[self.model_index]["model"]
            else:
                self.default_harness = str(data.get("default_harness") or self.default_harness).strip().lower()
                self.default_model = str(
                    data.get("default_model") or self.fallback_chain[self.model_index]["model"]
                ).strip()

            self.default_effort = str(data.get("default_effort") or self.default_effort).strip().lower()
            self.default_permission_preset = normalize_permission_preset(
                data.get("default_permission_preset") or self.default_permission_preset
            ).value
            self.interval_seconds = max(1, int(data.get("interval_seconds", 5) or 5))
            self.backoff_level = max(0, int(data.get("backoff_level", 0) or 0))
            self.start_time = str(data.get("start_time", "Unknown") or "Unknown")
            self.watcher_paused = bool(data.get("watcher_paused", False))
            raw_paused = data.get("paused_projects", [])
            if isinstance(raw_paused, list):
                self.paused_projects = sorted({str(p).strip().upper() for p in raw_paused if str(p).strip()})
        except Exception as exc:
            print(f"[SystemState] Error loading state file: {exc}")

    def _save(self) -> None:
        state_file = self._state_file
        payload = {
            "default_harness": self.default_harness,
            "default_model": self.default_model,
            "default_effort": self.default_effort,
            "default_permission_preset": self.default_permission_preset,
            "fallback_chain": self.fallback_chain,
            "model_index": self.model_index,
            "interval_seconds": self.interval_seconds,
            "backoff_level": self.backoff_level,
            "start_time": self.start_time,
            "watcher_paused": self.watcher_paused,
            "paused_projects": list(self.paused_projects),
        }
        try:
            parent = os.path.dirname(os.path.abspath(state_file))
            os.makedirs(parent, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(dir=parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2)
                os.replace(temp_path, state_file)
            except Exception:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
                raise
        except Exception as exc:
            print(f"[SystemState] Error saving state file: {exc}")

    def get_state(self) -> Dict[str, Any]:
        """Reload and return current persistent state dictionary."""
        with self._lock:
            self._load()
            return {
                "default_harness": self.default_harness,
                "default_model": self.default_model,
                "current_model": self.default_model,
                "default_effort": self.default_effort,
                "default_permission_preset": self.default_permission_preset,
                "fallback_chain": [dict(item) for item in self.fallback_chain],
                "model_index": self.model_index,
                "interval_seconds": self.interval_seconds,
                "backoff_level": self.backoff_level,
                "start_time": self.start_time,
                "watcher_paused": self.watcher_paused,
                "paused_projects": list(self.paused_projects),
            }

    def handle_429(self) -> bool:
        """
        Advance to the next (harness, model) entry on rate limit.
        Returns True if the end of the chain was reached and backoff was applied.
        """
        with self._lock:
            self._load()
            if self.model_index < len(self.fallback_chain) - 1:
                self.model_index += 1
                entry = self.fallback_chain[self.model_index]
                self.default_harness = entry["harness"]
                self.default_model = entry["model"]
                result = False
            else:
                idx = min(self.backoff_level, len(BACKOFF_SCHEDULE_SECONDS) - 1)
                self.interval_seconds = BACKOFF_SCHEDULE_SECONDS[idx]
                self.backoff_level += 1
                result = True
            self._save()
            print(
                f"[SystemState] PID {os.getpid()} - 429 handled. "
                f"harness={self.default_harness}, model={self.default_model}, "
                f"index={self.model_index}, interval={self.interval_seconds}s"
            )
            return result

    def reset_success(self) -> None:
        """Reset watcher interval and backoff after a successful step without resetting model_index."""
        with self._lock:
            self._load()
            self.interval_seconds = 5
            self.backoff_level = 0
            self._save()

    def reset_model(self) -> None:
        """Reset fallback index and active harness/model back to the primary entry."""
        with self._lock:
            self._load()
            self.model_index = 0
            self.backoff_level = 0
            entry = self.fallback_chain[0]
            self.default_harness = entry["harness"]
            self.default_model = entry["model"]
            self._save()

    def set_start_time(self, start_time_str: str) -> None:
        with self._lock:
            self._load()
            self.start_time = start_time_str
            self._save()

    def get_current_model(self) -> str:
        return str(self.get_state()["default_model"])

    def get_current_harness(self) -> str:
        return str(self.get_state()["default_harness"])

    def get_current_profile(self) -> HarnessProfile:
        """Return global default execution profile as a HarnessProfile."""
        st = self.get_state()
        return HarnessProfile.from_dict(
            {
                "harness": st["default_harness"],
                "model": st["default_model"],
                "effort": st["default_effort"],
                "permission_preset": st["default_permission_preset"],
                "session_mode": "stateless",
            }
        )

    def get_interval(self) -> int:
        return int(self.get_state()["interval_seconds"])

    def set_model_index(self, index: int) -> bool:
        """Select a fallback chain entry by index and sync harness/model."""
        with self._lock:
            self._load()
            if 0 <= index < len(self.fallback_chain):
                self.model_index = index
                entry = self.fallback_chain[index]
                self.default_harness = entry["harness"]
                self.default_model = entry["model"]
                self._save()
                return True
            return False

    def set_harness(self, harness: str, model: Optional[str] = None) -> None:
        """Switch active default harness and optional model."""
        clean_harness = harness.strip().lower()
        if not clean_harness:
            raise ValueError("Harness name cannot be empty")
        with self._lock:
            self._load()
            self.default_harness = clean_harness
            self.default_model = model.strip() if model and model.strip() else "default"
            for idx, entry in enumerate(self.fallback_chain):
                if entry["harness"] == self.default_harness and entry["model"] == self.default_model:
                    self.model_index = idx
                    break
            self._save()

    def set_effort(self, effort: str) -> bool:
        """Update default reasoning effort (low, medium, high, xhigh)."""
        norm = effort.strip().lower()
        if norm not in ("low", "medium", "high", "xhigh"):
            return False
        with self._lock:
            self._load()
            self.default_effort = norm
            self._save()
            return True

    def set_permission_preset(self, preset: str) -> str:
        """Normalize and persist default permission preset."""
        norm = normalize_permission_preset(preset).value
        with self._lock:
            self._load()
            self.default_permission_preset = norm
            self._save()
            return norm

    def get_all_models(self) -> List[str]:
        """Return model labels along the fallback chain for backwards compatibility."""
        chain = self.get_fallback_chain()
        return [entry["model"] for entry in chain]

    def get_fallback_chain(self) -> List[Dict[str, str]]:
        return self.get_state()["fallback_chain"]

    def set_fallback_chain(self, chain: List[Dict[str, str]]) -> None:
        normalized = _normalize_fallback_chain(chain)
        with self._lock:
            self._load()
            self.fallback_chain = normalized
            self.model_index = 0
            self.default_harness = normalized[0]["harness"]
            self.default_model = normalized[0]["model"]
            self._save()

    def is_watcher_paused(self) -> bool:
        """Return True if global watcher scheduling is paused."""
        return bool(self.get_state()["watcher_paused"])

    def set_watcher_paused(self, paused: bool) -> bool:
        """Pause or resume global watcher scheduling."""
        with self._lock:
            self._load()
            self.watcher_paused = bool(paused)
            self._save()
            return self.watcher_paused

    def is_project_paused(self, pid: str) -> bool:
        """Return True if scheduling for pid is paused."""
        return pid.strip().upper() in self.get_state()["paused_projects"]

    def pause_project(self, pid: str) -> List[str]:
        """Pause autonomous scheduling for a single project."""
        clean_pid = pid.strip().upper()
        with self._lock:
            self._load()
            if clean_pid and clean_pid not in self.paused_projects:
                self.paused_projects.append(clean_pid)
                self.paused_projects.sort()
            self._save()
            return list(self.paused_projects)

    def resume_project(self, pid: str) -> List[str]:
        """Resume autonomous scheduling for a single project."""
        clean_pid = pid.strip().upper()
        with self._lock:
            self._load()
            if clean_pid in self.paused_projects:
                self.paused_projects.remove(clean_pid)
            self._save()
            return list(self.paused_projects)

    def get_paused_projects(self) -> List[str]:
        """Return sorted list of currently paused project IDs."""
        return list(self.get_state()["paused_projects"])


state = SystemState()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_state_file = os.path.join(tmpdir, "system_state.json")
        with open(tmp_state_file, "w", encoding="utf-8") as f:
            json.dump({"model_index": 1, "interval_seconds": 5, "start_time": "2026-01-01"}, f)
        st = SystemState(state_file=tmp_state_file)
        assert st.get_current_harness() == "agy"
        assert st.get_current_model() == DEFAULT_FALLBACK_CHAIN[1]["model"]
        assert st.handle_429() is False
        assert st.get_current_model() == DEFAULT_FALLBACK_CHAIN[2]["model"]
    print("orchestrator.state self-test passed!")
