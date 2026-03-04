import threading
import os
import json
from .storage import get_data_dir

# Global list of models
MODEL_NAMES = [
    "gemini-3-flash-preview",
    "default",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite"
]

class SystemState:
    def __init__(self):
        self._lock = threading.Lock()
        self._state_file = os.path.join(get_data_dir(), "system_state.json")
        self._load()
        print(f"[SystemState] Initialized in process {os.getpid()}. State: {self.get_state()}")
        
    def _load(self):
        if os.path.exists(self._state_file):
            try:
                with open(self._state_file, "r") as f:
                    data = json.load(f)
                    self.model_index = data.get("model_index", 0)
                    self.interval_seconds = data.get("interval_seconds", 5)
                    # Ensure index is within bounds
                    if self.model_index >= len(MODEL_NAMES):
                        self.model_index = 0
                    self.start_time = data.get("start_time", "Unknown")
                return
            except Exception as e:
                print(f"[SystemState] Error loading state file: {e}")
        
        self.model_index = 0
        self.interval_seconds = 5
        self.start_time = "Unknown"

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self._state_file), exist_ok=True)
            with open(self._state_file, "w") as f:
                json.dump({
                    "model_index": self.model_index,
                    "interval_seconds": self.interval_seconds,
                    "start_time": getattr(self, "start_time", "Unknown")
                }, f)
        except Exception as e:
            print(f"[SystemState] Error saving state file: {e}")

    def get_state(self):
        # We re-load on every read to ensure we see changes from other processes (Web UI)
        with self._lock:
            self._load()
            return {
                "model_index": self.model_index,
                "current_model": MODEL_NAMES[self.model_index],
                "interval_seconds": self.interval_seconds,
                "start_time": getattr(self, "start_time", "Unknown")
            }
            
    def handle_429(self):
        with self._lock:
            self._load()
            if self.model_index < len(MODEL_NAMES) - 1:
                self.model_index += 1
                result = False
            else:
                self.interval_seconds = 600
                result = True
            self._save()
            print(f"[SystemState] PID {os.getpid()} - 429 handled. New State: index={self.model_index}, interval={self.interval_seconds}")
            return result
                
    def reset_success(self):
        with self._lock:
            self._load()
            self.interval_seconds = 5
            self._save()
            # Note: We do NOT reset model_index here per user request to only reset interval.
            
    def reset_model(self):
        with self._lock:
            self.model_index = 0
            self._save()

    def set_start_time(self, start_time_str: str):
        with self._lock:
            self._load()
            self.start_time = start_time_str
            self._save()

    def get_current_model(self):
        return self.get_state()["current_model"]
            
    def get_interval(self):
        return self.get_state()["interval_seconds"]
            
    def set_model_index(self, index: int):
        with self._lock:
            if 0 <= index < len(MODEL_NAMES):
                self.model_index = index
                self._save()
                return True
            return False
            
    def get_all_models(self):
        return MODEL_NAMES

# Singleton instance
state = SystemState()
