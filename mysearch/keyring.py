"""MySearch 通用 key ring。"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass as _dataclass
from threading import Lock

from mysearch.config import MySearchConfig, ProviderConfig


def dataclass(*args, **kwargs):
    if sys.version_info < (3, 10):
        kwargs.pop("slots", None)
    return _dataclass(*args, **kwargs)


@dataclass(frozen=True, slots=True)
class KeyRecord:
    provider: str
    key: str
    source: str
    label: str


class MySearchKeyRing:
    def __init__(self, config: MySearchConfig) -> None:
        self.config = config
        self._lock = Lock()
        self._keys: dict[str, list[KeyRecord]] = {
            "tavily": [],
            "firecrawl": [],
            "exa": [],
            "xai": [],
        }
        self._indexes = {
            "tavily": 0,
            "firecrawl": 0,
            "exa": 0,
            "xai": 0,
        }
        self._quarantined: dict[str, dict[str, dict[str, object]]] = {
            "tavily": {},
            "firecrawl": {},
            "exa": {},
            "xai": {},
        }
        self._generation = 0
        self.reload()

    def reload(self, *, clear_quarantine: bool = True) -> None:
        with self._lock:
            self._generation += 1
            self._keys["tavily"] = self._load_provider(self.config.tavily)
            self._keys["firecrawl"] = self._load_provider(self.config.firecrawl)
            self._keys["exa"] = self._load_provider(self.config.exa)
            self._keys["xai"] = self._load_provider(self.config.xai)
            for provider, keys in self._keys.items():
                if clear_quarantine:
                    self._quarantined[provider].clear()
                else:
                    loaded_values = {record.key for record in keys}
                    self._quarantined[provider] = {
                        key: state
                        for key, state in self._quarantined[provider].items()
                        if key in loaded_values
                    }
                if self._indexes[provider] >= len(keys):
                    self._indexes[provider] = 0

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def get_next(self, provider: str) -> KeyRecord | None:
        with self._lock:
            keys = self._available_records_locked(provider)
            if not keys:
                return None
            index = self._indexes[provider] % len(keys)
            self._indexes[provider] = (index + 1) % len(keys)
            return keys[index]

    def has_provider(self, provider: str) -> bool:
        with self._lock:
            return bool(self._available_records_locked(provider))

    def has_configured_provider(self, provider: str) -> bool:
        with self._lock:
            return bool(self._keys.get(provider))

    def is_available(self, provider: str, key: str) -> bool:
        with self._lock:
            return any(
                record.key == key
                for record in self._available_records_locked(provider)
            )

    def quarantine(
        self,
        provider: str,
        key: str,
        reason: str,
        *,
        retry_after_seconds: int | None = None,
        generation: int | None = None,
    ) -> bool:
        """Remove one credential from scheduling temporarily or until reload."""
        with self._lock:
            if generation is not None and generation != self._generation:
                return False
            if not any(record.key == key for record in self._keys.get(provider, [])):
                return False
            next_state = {
                "reason": reason.strip() or "unavailable",
                "until": (
                    time.monotonic() + max(1, retry_after_seconds)
                    if retry_after_seconds is not None
                    else None
                ),
            }
            current_state = self._quarantined[provider].get(key)
            if current_state is not None:
                current_until = current_state.get("until")
                next_until = next_state.get("until")
                if current_until is None:
                    return True
                if next_until is not None and float(next_until) <= float(current_until):
                    return True
            self._quarantined[provider][key] = next_state
            available_count = len(self._available_records_locked(provider))
            if available_count:
                self._indexes[provider] %= available_count
            else:
                self._indexes[provider] = 0
            return True

    def first(self, provider: str) -> KeyRecord | None:
        with self._lock:
            keys = self._available_records_locked(provider)
            if not keys:
                return None
            return keys[0]

    def describe(self) -> dict[str, dict[str, object]]:
        with self._lock:
            result: dict[str, dict[str, object]] = {}
            for provider, keys in self._keys.items():
                available = self._available_records_locked(provider)
                quarantined = self._quarantined[provider]
                unavailable_labels = [
                    record.label for record in keys if record.key in quarantined
                ]
                result[provider] = {
                    "count": len(available),
                    "total_count": len(keys),
                    "quarantined_count": len(quarantined),
                    "sources": sorted({key.source for key in keys}),
                    "labels": [key.label for key in available],
                    "quarantined_labels": unavailable_labels,
                    "quarantine_reasons": sorted(
                        {str(state.get("reason") or "unavailable") for state in quarantined.values()}
                    ),
                }
            return result

    def _available_records_locked(self, provider: str) -> list[KeyRecord]:
        quarantined = self._quarantined.get(provider, {})
        now = time.monotonic()
        expired = [
            key
            for key, state in quarantined.items()
            if state.get("until") is not None and float(state["until"]) <= now
        ]
        for key in expired:
            quarantined.pop(key, None)
        return [
            record
            for record in self._keys.get(provider, [])
            if record.key not in quarantined
        ]

    def _load_provider(self, provider: ProviderConfig) -> list[KeyRecord]:
        loaded: list[KeyRecord] = []

        for index, key in enumerate(provider.api_keys, start=1):
            cleaned = key.strip()
            if not cleaned:
                continue
            loaded.append(
                KeyRecord(
                    provider=provider.name,
                    key=cleaned,
                    source="env",
                    label=f"{provider.name}:env:{index}",
                )
            )

        if provider.keys_file and provider.keys_file.exists():
            loaded.extend(self._load_from_file(provider))

        deduped: list[KeyRecord] = []
        seen: set[str] = set()
        for record in loaded:
            if record.key in seen:
                continue
            seen.add(record.key)
            deduped.append(record)
        return deduped

    def _load_from_file(self, provider: ProviderConfig) -> list[KeyRecord]:
        records: list[KeyRecord] = []
        if provider.keys_file is None:
            return records

        try:
            file_content = provider.keys_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return records

        for line_no, raw_line in enumerate(
            file_content.splitlines(),
            start=1,
        ):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            parts = [item.strip() for item in line.split(",") if item.strip()]
            key = parts[-1] if parts else line
            label = parts[0] if len(parts) >= 2 else f"{provider.keys_file.name}:{line_no}"
            if not key:
                continue

            records.append(
                KeyRecord(
                    provider=provider.name,
                    key=key,
                    source="file",
                    label=label,
                )
            )

        return records
