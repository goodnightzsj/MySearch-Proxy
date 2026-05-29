"""
按服务维度管理 API Key 轮询池
"""
import threading
import time

from database import SUPPORTED_SERVICES, get_active_keys, normalize_service, update_key_usage

_FAILURE_RELOAD_COOLDOWN = 5.0


class ServiceKeyPool:
    def __init__(self):
        self._lock = threading.Lock()
        self._keys = {service: [] for service in SUPPORTED_SERVICES}
        self._indexes = {service: 0 for service in SUPPORTED_SERVICES}
        self._initialized = set()
        self._last_failure_reload = {service: 0.0 for service in SUPPORTED_SERVICES}

    def reload(self, service=None):
        services = [normalize_service(service)] if service else list(SUPPORTED_SERVICES)
        with self._lock:
            for item in services:
                self._keys[item] = [dict(row) for row in get_active_keys(item)]
                if self._indexes[item] >= len(self._keys[item]):
                    self._indexes[item] = 0
                self._initialized.add(item)

    def get_next_key(self, service="tavily"):
        """Round-robin 返回某个服务下一个可用 key。"""
        service = normalize_service(service)
        with self._lock:
            if service not in self._initialized:
                self._initialized.add(service)
                needs_load = True
            else:
                needs_load = False
                keys = self._keys[service]
                if not keys:
                    return None
                index = self._indexes[service]
                key = keys[index]
                self._indexes[service] = (index + 1) % len(keys)
                return dict(key)
        # DB I/O outside the lock
        fresh_keys = [dict(row) for row in get_active_keys(service)]
        with self._lock:
            self._keys[service] = fresh_keys
            if not fresh_keys:
                return None
            index = self._indexes.get(service, 0) % len(fresh_keys)
            key = fresh_keys[index]
            self._indexes[service] = (index + 1) % len(fresh_keys)
            return dict(key)

    def report_result(self, service, key_id, success):
        """记录使用结果，失败 3 次自动禁用并从对应池中移除。"""
        service = normalize_service(service)
        update_key_usage(key_id, success)
        if not success:
            now = time.monotonic()
            with self._lock:
                if now - self._last_failure_reload.get(service, 0.0) < _FAILURE_RELOAD_COOLDOWN:
                    return
                self._last_failure_reload[service] = now
            self.reload(service)


pool = ServiceKeyPool()
