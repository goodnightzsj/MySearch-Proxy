"""Bounded diagnostic metadata; search text and credentials never belong here."""

from __future__ import annotations

import math
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal, Union, get_args
from uuid import UUID

try:
    from pydantic.v1 import BaseModel, StrictBool, StrictFloat, StrictInt, conint, conlist, constr, root_validator, validator
except ModuleNotFoundError:  # Standalone Proxy also permits Pydantic 1 before its v1 namespace.
    from pydantic import BaseModel, StrictBool, StrictFloat, StrictInt, conint, conlist, constr, root_validator, validator


Provider = Literal["tavily", "firecrawl", "exa", "xai", "web"]
Operation = Literal["search", "contents", "extract", "scrape", "map", "crawl", "crawl_status", "responses", "social_search", "health", "text", "request"]
Outcome = Literal["success", "empty", "degraded", "unavailable", "error", "deadline", "cancelled"]
PROVIDERS = set(get_args(Provider))
OPERATIONS = set(get_args(Operation))
Elapsed = conint(strict=True, ge=0, le=86400000)
PhaseName = constr(strict=True, regex=r"^[a-z][a-z0-9_]{0,47}\Z")
FailureReason = constr(strict=True, regex=r"^(?:[A-Za-z][A-Za-z0-9_]{0,119})?\Z")
UUIDText = constr(strict=True, min_length=32, max_length=36)


def uuid_hex(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value):
        raise ValueError("Invalid diagnostic UUID")
    return UUID(value).hex


class Metadata(BaseModel):
    class Config:
        extra = "forbid"


class Usage(Metadata):
    name: Literal["credits", "input_tokens", "output_tokens", "total_tokens", "cost", "web_search_calls", "x_search_calls", "social_search_calls"]
    unit: Literal["credits", "tokens", "usd", "requests"]
    value: Union[StrictInt, StrictFloat]

    @validator("value")
    def bounded_value(cls, value):
        if not 0 <= value <= 1e12 or not math.isfinite(value):
            raise ValueError("Invalid diagnostic usage")
        return value

    @root_validator(skip_on_failure=True)
    def matching_unit(cls, values):
        name = values["name"]
        expected = "tokens" if name.endswith("_tokens") else "requests" if name.endswith("_calls") else "usd" if name == "cost" else "credits"
        if values["unit"] != expected:
            raise ValueError("Diagnostic usage unit differs")
        return values


class ProviderUsage(Usage):
    provider: Provider


class Phase(Metadata):
    name: PhaseName
    elapsed_ms: Elapsed


class RuntimeCall(Metadata):
    call_id: UUIDText
    layer: Literal["runtime"] = "runtime"
    provider: Provider
    operation: Operation
    phase: PhaseName
    elapsed_ms: Elapsed
    status_code: conint(strict=True, ge=100, le=599) | None
    outcome: Literal["success", "error", "pending"]
    failure_reason: FailureReason = ""
    upstream_request_id: constr(strict=True, regex=r"^[A-Za-z0-9._:-]{1,128}\Z") | None = None
    usage: conlist(Usage, max_items=32)
    _uuid = validator("call_id", allow_reuse=True)(uuid_hex)


class DiagnosticDetails(Metadata):
    request_id: UUIDText | None = None
    phases: conlist(Phase, max_items=64)
    calls: conlist(RuntimeCall, max_items=128)
    call_count: conint(strict=True, ge=0, le=1000000)
    usage: conlist(ProviderUsage, max_items=32)
    calls_truncated: conint(strict=True, ge=0, le=1000000) | None = None
    phases_truncated: conint(strict=True, ge=0, le=1000000) = 0
    elapsed_ms: Elapsed | None = None
    outcome: Outcome | None = None
    warning: Literal["telemetry_failed"] | None = None

    @root_validator(skip_on_failure=True)
    def consistent_calls(cls, values):
        calls, count = values["calls"], values["call_count"]
        if len({call.call_id for call in calls}) != len(calls) or count < len(calls):
            raise ValueError("Invalid diagnostic calls")
        truncated = count - len(calls)
        if values["calls_truncated"] not in (None, truncated):
            raise ValueError("Invalid diagnostic call count")
        values["calls_truncated"] = truncated
        return values


class RequestReport(Metadata):
    request_id: UUIDText
    tool: Literal["search", "extract_url", "research", "map_site", "crawl_site", "mysearch_health"]
    outcome: Outcome
    started_at: constr(strict=True, max_length=40)
    elapsed_ms: Elapsed
    failure_reason: FailureReason = ""
    cache_hit: StrictBool | None = None
    diagnostics: DiagnosticDetails
    _uuid = validator("request_id", allow_reuse=True)(uuid_hex)

    @validator("started_at")
    def utc_timestamp(cls, value):
        started = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if started.utcoffset() != timedelta(0):
            raise ValueError("Diagnostic timestamp must be UTC")
        return started.astimezone(timezone.utc).isoformat()

    @root_validator(skip_on_failure=True)
    def matching_request_id(cls, values):
        nested_id = values["diagnostics"].request_id
        if nested_id is not None and uuid_hex(nested_id) != values["request_id"]:
            raise ValueError("Diagnostic request IDs differ")
        details = values["diagnostics"]
        if details.elapsed_ms not in (None, values["elapsed_ms"]) or details.outcome not in (None, values["outcome"]):
            raise ValueError("Diagnostic terminal values differ")
        return values


def validate_report(raw):
    if not isinstance(raw, dict):
        raise ValueError("Expected diagnostic object")
    return RequestReport.parse_obj(raw).dict(exclude={"diagnostics": {"request_id"}})


@dataclass
class RequestScope:
    request_id: str | None = None
    call_id: str | None = None
    phase: str = "request"
    token_id: int | None = None
    authorized: bool = False
    attempts: list = field(default_factory=list)


current_scope: ContextVar[RequestScope | None] = ContextVar("proxy_diagnostics", default=None)


def scope_from_headers(headers):
    request_id = headers.get("X-MySearch-Request-ID")
    call_id = headers.get("X-MySearch-Call-ID")
    phase = headers.get("X-MySearch-Phase", "request")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", phase):
        raise ValueError("Invalid diagnostic phase")
    return RequestScope(
        request_id=uuid_hex(request_id) if request_id is not None else None,
        call_id=uuid_hex(call_id) if call_id is not None else None,
        phase=phase,
    )


def bind_token(token_row):
    scope = current_scope.get()
    if scope is not None:
        scope.authorized = True
        scope.token_id = token_row["id"] if token_row is not None else None


def operation_for_path(path, method):
    parts = str(path).strip("/").split("/")
    if parts and parts[0] in {"v1", "v2"}:
        parts = parts[1:]
    operation = parts[0] if parts else "request"
    if operation == "crawl" and method.upper() == "GET":
        operation = "crawl_status"
    return operation if operation in OPERATIONS else "request"
