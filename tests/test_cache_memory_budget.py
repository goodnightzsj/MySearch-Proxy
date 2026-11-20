from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from mysearch.cache import CacheStore


class CacheMemoryBudgetTests(unittest.TestCase):
    def test_payload_budget_evicts_oldest_without_truncating_retained_content(self):
        cache = CacheStore(ttls={"extract": 300}, max_bytes=1600)
        content = "x" * 800
        cache.set("extract", "first", {"content": content})
        cache.set("extract", "second", {"content": content + "END"})
        self.assertIsNone(cache.get("extract", "first"))
        self.assertEqual(cache.get("extract", "second")["content"], content + "END")
        self.assertLessEqual(cache.health()["extract"]["payload_bytes"], 1600)

    def test_oversized_value_is_not_cached_or_mutated_and_removes_old_value(self):
        cache = CacheStore(ttls={"extract": 300}, max_bytes=1600)
        cache.set("extract", "key", {"content": "old"})
        large = {"content": "完整全文" * 1000 + "END"}
        cache.set("extract", "key", large)
        self.assertTrue(large["content"].endswith("END"))
        self.assertIsNone(cache.get("extract", "key"))
        self.assertEqual(cache.health()["extract"]["payload_bytes"], 0)
        self.assertEqual(cache.health()["extract"]["oversized_skips"], 1)

    def test_replacing_at_entry_capacity_does_not_evict_an_unrelated_key(self):
        cache = CacheStore(ttls={"extract": 300}, max_entries=2)
        cache.set("extract", "first", {"content": "first"})
        cache.set("extract", "second", {"content": "second"})
        cache.set("extract", "second", {"content": "updated"})
        self.assertEqual(cache.get("extract", "first")["content"], "first")
        self.assertEqual(cache.get("extract", "second")["content"], "updated")

    def test_delete_and_expiry_release_accounted_bytes(self):
        cache = CacheStore(ttls={"extract": 10})
        with patch("mysearch.cache.time.monotonic", return_value=100):
            cache.set("extract", "key", {"content": "body"})
            self.assertGreater(cache.health()["extract"]["payload_bytes"], 0)
            cache.delete("extract", "key")
            self.assertEqual(cache.health()["extract"]["payload_bytes"], 0)
            cache.set("extract", "key", {"content": "body"})
        with patch("mysearch.cache.time.monotonic", return_value=111):
            self.assertIsNone(cache.get("extract", "key"))
            self.assertEqual(cache.health()["extract"]["payload_bytes"], 0)

    def test_concurrent_writes_keep_budget_and_caller_isolation(self):
        cache = CacheStore(ttls={"extract": 300}, max_bytes=5000)
        def write(index):
            cache.set("extract", str(index), {"content": "x" * 800, "metadata": {"index": index}})
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(write, range(64)))
        state = cache.health()["extract"]
        self.assertGreater(state["payload_bytes"], 0)
        self.assertLessEqual(state["payload_bytes"], 5000)
        for index in range(64):
            value = cache.get("extract", str(index))
            if value is not None:
                value["metadata"]["index"] = -1
                self.assertEqual(cache.get("extract", str(index))["metadata"]["index"], index)

    def test_disabled_namespace_does_not_account_or_store(self):
        cache = CacheStore(ttls={"search": 0})
        cache.set("search", "key", {"content": "body"})
        self.assertIsNone(cache.get("search", "key"))
        self.assertEqual(cache.health()["search"]["payload_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
