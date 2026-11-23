"""Per-operation budgets and bounded diagnostics, without request content."""

from __future__ import annotations

import copy
import math
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from threading import Event, Lock
from typing import Any
from uuid import uuid4

from mysearch.errors import MySearchError
from mysearch.provider_contract import ProviderResponse, SOCIAL_PROVIDER_VARIANTS

MAX_CALLS = 128
MAX_PHASES = 64


def extract_usage(payload: object, operation: str = "request") -> list[dict[str, Any]]:
    """Keep reported quantities in their original units; missing is not zero."""
    if not isinstance(payload, dict):
        return []
    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    candidates = [(name, "tokens", usage.get(name)) for name in (
        "input_tokens", "output_tokens", "total_tokens",
    )]
    # Crawl status reports job totals, so repeated polls must not add them again.
    top_level_credits = None if operation == "crawl_status" else payload.get("creditsUsed", payload.get("credits_used"))
    if top_level_credits is None and operation == "scrape":
        document = payload.get("data", payload)
        metadata = document.get("metadata") if isinstance(document, dict) else None
        if isinstance(metadata, dict):
            top_level_credits = metadata.get("creditsUsed", metadata.get("credits_used"))
    credits = usage.get("credits", top_level_credits)
    candidates.append(("credits", "credits", credits))
    cost = payload.get("costDollars")
    if isinstance(cost, dict):
        candidates.append(("cost", "usd", cost.get("total")))
    tools = payload.get("server_side_tool_usage") or payload.get("tool_usage")
    if isinstance(tools, dict):
        candidates.extend((name, "requests", tools.get(name)) for name in (
            "web_search_calls", "x_search_calls", "social_search_calls",
        ))
    details = usage.get("server_side_tool_usage_details")
    if isinstance(details, dict):
        candidates.extend((name, unit, details.get(name)) for name, unit in (
            ("x_posts_fetched", "posts"), ("x_users_fetched", "users"),
        ))
    return [
        {"name": name, "unit": unit, "value": value}
        for name, unit, value in candidates
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        and 0 <= value <= 1e12 and math.isfinite(value)
    ]


def upstream_request_id(payload: object) -> str:
    value = (payload.get("request_id") or payload.get("requestId")) if isinstance(payload, dict) else None
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value) else ""


@dataclass(eq=False)
class RequestState:
    deadline: float
    cancelled: Event = field(default_factory=Event)
    tool: str = ""
    request_id: str = field(default_factory=lambda: uuid4().hex)
    started: float = field(default_factory=time.monotonic)
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    stop_reason: str = ""
    claimed: bool = False
    calls: list[dict[str, Any]] = field(default_factory=list)
    phases: list[dict[str, Any]] = field(default_factory=list)
    call_count: int = 0
    phase_count: int = 0
    totals: dict[tuple[str, str, str], float] = field(default_factory=dict)
    final: dict[str, Any] | None = None
    extractions: dict[tuple, Any] = field(default_factory=dict, repr=False)
    lock: Any = field(default_factory=Lock, repr=False)

    def cancel(self, reason: str = "caller_cancelled") -> None:
        with self.lock:
            if not self.stop_reason:
                self.stop_reason = reason
            self.cancelled.set()

    def progress_snapshot(self) -> list[str]:
        with self.lock:
            return [item["name"] for item in self.phases]

    def start_call(self, call: dict[str, Any]) -> None:
        with self.lock:
            if self.cancelled.is_set():
                raise MySearchError(f"{self.tool or 'research'} request budget exhausted")
            self.call_count += 1
            if len(self.calls) < MAX_CALLS:
                self.calls.append(dict(call))

    def complete_call(self, call: dict[str, Any]) -> None:
        with self.lock:
            if self.final is not None:
                return
            for index, pending in enumerate(self.calls):
                if pending["call_id"] == call["call_id"]:
                    self.calls[index] = copy.deepcopy(call)
                    break
            for quantity in call["usage"]:
                key = (call["provider"], quantity["name"], quantity["unit"])
                self.totals[key] = self.totals.get(key, 0) + quantity["value"]


current_request: ContextVar[RequestState | None] = ContextVar("mysearch_request_budget", default=None)
current_deadline: ContextVar[float | None] = ContextVar("mysearch_nested_deadline", default=None)
current_phase: ContextVar[str] = ContextVar("mysearch_request_phase", default="request")


@contextmanager
def request_context(state: RequestState):
    """MCP may enter this before offload so queue time shares the root budget."""
    token = current_request.set(state)
    try:
        yield state
    finally:
        current_request.reset(token)


def remaining_seconds() -> float | None:
    state = current_request.get()
    deadline = current_deadline.get()
    if state is None and deadline is None:
        return None
    if state is not None:
        deadline = min(state.deadline, deadline or state.deadline)
    remaining = deadline - time.monotonic()
    if (state is not None and state.cancelled.is_set()) or remaining <= 0:
        if state is not None and remaining <= 0 and deadline == state.deadline:
            state.cancel("deadline")
        tool = (state.tool or "research") if state is not None else "provider"
        raise MySearchError(f"{tool} request budget exhausted")
    return remaining


@contextmanager
def phase(name: str):
    state = current_request.get()
    name = name if re.fullmatch(r"[a-z][a-z0-9_]{0,47}", name) else "content"
    token = current_phase.set(name)
    started = time.monotonic()
    try:
        yield
    finally:
        if state is not None:
            with state.lock:
                if state.final is None:
                    state.phase_count += 1
                    if len(state.phases) < MAX_PHASES:
                        state.phases.append({"name": name, "elapsed_ms": elapsed_ms(started)})
        current_phase.reset(token)


def elapsed_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))


@contextmanager
def direct_call(*, provider: str = "web", operation: str = "text"):
    """Trace one direct HTTP send; its caller owns response interpretation."""
    remaining_seconds()
    state = current_request.get()
    call = {
        "layer": "runtime", "call_id": uuid4().hex, "provider": provider,
        "operation": operation, "phase": current_phase.get(), "elapsed_ms": 0,
        "status_code": None, "outcome": "pending", "failure_reason": "", "usage": [],
    }
    started = time.monotonic()
    if state is not None:
        state.start_call(call)
    try:
        yield call
    except BaseException as exc:
        call["outcome"], call["failure_reason"] = "error", type(exc).__name__
        raise
    finally:
        call["elapsed_ms"] = elapsed_ms(started)
        if state is not None:
            state.complete_call(call)


def operation_timeout(config: Any, tool: str, arguments: dict[str, Any]) -> float:
    timeout = float(config.timeout_seconds)
    if tool == "research":
        return float(getattr(config, "research_timeout_seconds", 0) or timeout)
    if tool == "crawl_site":
        # Existing crawl budget includes its pinned-key Retry-After allowance.
        return float(getattr(config, "crawl_timeout_seconds", 0) or timeout) + 120
    if tool == "search" and (
        arguments.get("mode") == "social" or arguments.get("provider") == "xai"
        or "x" in (arguments.get("sources") or [])
        or arguments.get("allowed_x_handles") or arguments.get("excluded_x_handles")
    ):
        return max(timeout, float(getattr(config, "xai_social_timeout_seconds", 0) or timeout))
    return timeout


def result_outcome(tool: str, result: Any) -> str:
    if not isinstance(result, dict):
        return "success"
    if ProviderResponse.kind_of(result) == "unavailable":
        return "unavailable"
    if result.get("provider") in SOCIAL_PROVIDER_VARIANTS - {"custom_social"} or result.get("fallback") or result.get("social_error") or result.get("secondary_error"):
        return "degraded"
    if any((result.get("evidence") or {}).get(name) for name in ("result_event_page_budget_exhausted", "summary_error")):
        return "degraded"
    if result.get("warning") or any(page.get("error") or page.get("warning") for page in result.get("pages", [])):
        return "degraded"
    if tool == "crawl_site" and result.get("metadata", {}).get("entry_page", {}).get("status") == "failed":
        return "degraded"
    field_name = {"search": "results", "extract_url": "content", "map_site": "links", "crawl_site": "pages"}.get(tool)
    if field_name in result and not result[field_name]:
        return "empty"
    return "success"


def finish_payload(state: RequestState, result: Any = None, error: BaseException | None = None) -> dict[str, Any] | None:
    """Claim the only terminal event; a late worker cannot replace cancellation."""
    with state.lock:
        if state.final is not None:
            return None
        if not state.stop_reason and time.monotonic() >= state.deadline:
            state.stop_reason = "deadline"
        if state.stop_reason == "deadline":
            outcome, reason = "deadline", "deadline"
        elif state.stop_reason in {"caller_cancelled", "shutdown"}:
            outcome, reason = "cancelled", "cancelled"
        elif error is not None:
            outcome, reason = "error", type(error).__name__
        else:
            outcome, reason = result_outcome(state.tool, result), ""
        cache = result.get("cache", {}) if isinstance(result, dict) else {}
        cache_hits = [item["hit"] for item in cache.values() if isinstance(item, dict) and isinstance(item.get("hit"), bool)]
        duration = elapsed_ms(state.started)
        diagnostics = {
            "request_id": state.request_id,
            "phases": list(state.phases),
            "phases_truncated": state.phase_count - len(state.phases),
            "calls": copy.deepcopy(state.calls),
            "call_count": state.call_count,
            "calls_truncated": state.call_count - len(state.calls),
            "usage": [
                {"provider": provider, "name": name, "unit": unit, "value": value}
                for (provider, name, unit), value in sorted(state.totals.items())
            ],
            "elapsed_ms": duration,
            "outcome": outcome,
        }
        state.final = {
            "request_id": state.request_id, "tool": state.tool, "outcome": outcome,
            "started_at": state.started_at, "elapsed_ms": duration,
            "failure_reason": reason, "cache_hit": any(cache_hits) if cache_hits else None,
            "diagnostics": diagnostics,
        }
        state.stop_reason = state.stop_reason or "completed"
        state.extractions.clear()
        state.cancelled.set()
        return state.final


def diagnosed(tool: str):
    def decorate(method):
        @wraps(method)
        def run(self, *args, **kwargs):
            state = current_request.get() or self.new_request(tool, **kwargs)
            with state.lock:
                owner = not state.claimed
                state.claimed = True
            deadline = min(state.deadline, current_deadline.get() or state.deadline,
                           time.monotonic() + operation_timeout(self.config, tool, kwargs))
            token = current_deadline.set(deadline)
            try:
                with request_context(state), phase(tool):
                    remaining_seconds()
                    result = method(self, *args, **kwargs)
            except BaseException as exc:
                if owner:
                    self.finish_request(state, error=exc)
                raise
            finally:
                current_deadline.reset(token)
            if owner:
                self.finish_request(state, result=result)
                if isinstance(result, dict):
                    result = {**result, "diagnostics": copy.deepcopy(state.final["diagnostics"])}
            return result
        return run
    return decorate
