import threading

# Global state
MODEL_NAMES = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite"
]

class SystemState:
    def __init__(self):
        self._lock = threading.Lock()
        self.model_index = 0
        self.interval_seconds = 5
        
    def get_state(self):
        with self._lock:
            return {
                "model_index": self.model_index,
                "current_model": MODEL_NAMES[self.model_index],
                "interval_seconds": self.interval_seconds
            }
            
    def handle_429(self):
        """Returns True if we hit the backoff limit, False if we just switched models."""
        with self._lock:
            if self.model_index < len(MODEL_NAMES) - 1:
                self.model_index += 1
                return False
            else:
                self.interval_seconds = 600
                return True
                
    def reset_success(self):
        with self._lock:
            self.model_index = 0
            self.interval_seconds = 5
            
    def get_current_model(self):
        with self._lock:
            return MODEL_NAMES[self.model_index]
            
    def get_interval(self):
        with self._lock:
            return self.interval_seconds
            
    def set_model_index(self, index: int):
        with self._lock:
            if 0 <= index < len(MODEL_NAMES):
                self.model_index = index
                return True
            return False
            
    def get_all_models(self):
        return MODEL_NAMES

# Singleton instance
state = SystemState()
