"""Research comparison support: pure query-to-data transforms.

No network calls, no provider dependencies, no self state.
Extracted from MySearchClient methods in clients.py.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def research_cluster_fit_summary(cluster_label: str) -> str:
    return {
        "official": "canonical ground truth",
        "supporting": "supporting analysis",
        "general": "general coverage",
        "community": "community signal",
        "project": "project-native source",
        "curated": "curated comparison",
        "listicle": "broad scan",
        "directory": "directory-style inventory",
    }.get(cluster_label, "general coverage")


def research_comparison_support_summary(
    *,
    authoritative_source_count: int,
    supporting_source_count: int,
) -> str:
    if authoritative_source_count > 0:
        return "Authoritative sources and corroborating analysis were found."
    if supporting_source_count > 0:
        return "Supporting sources and corroborating analysis were found."
    return "The strongest available evidence is comparative rather than authoritative."


def research_ambiguous_product_tokens() -> set[str]:
    return {
        "assistants",
        "audio",
        "background",
        "batch",
        "chat",
        "embeddings",
        "files",
        "images",
        "realtime",
        "responses",
        "webhooks",
    }


def research_result_matches_entity(
    *,
    item: dict[str, Any],
    entity_tokens: tuple[str, ...],
) -> bool:
    tokens = [str(token).strip().lower() for token in entity_tokens if str(token).strip()]
    if not tokens:
        return False
    text = " ".join(
        [
            (item.get("title") or "").lower(),
            (item.get("url") or "").lower(),
            (item.get("snippet") or "").lower(),
        ]
    )
    return any(token in text for token in tokens)


def research_result_matches_comparison_subject(
    *,
    item: dict[str, Any],
    entity_tokens: tuple[str, ...],
) -> bool:
    tokens = [str(token).strip().lower() for token in entity_tokens if str(token).strip()]
    if not tokens:
        return False
    text = " ".join(
        [
            (item.get("title") or "").lower(),
            (item.get("url") or "").lower(),
            (item.get("snippet") or "").lower(),
        ]
    )
    if len(tokens) == 1:
        return tokens[0] in text
    specific_tokens = tokens[1:] or tokens
    specific_match_count = sum(1 for token in specific_tokens if token in text)
    required_specific_matches = 1 if len(specific_tokens) == 1 else min(2, len(specific_tokens))
    if specific_match_count >= required_specific_matches:
        return True
    return all(token in text for token in tokens)


def research_comparison_profile(
    *,
    candidate: str,
    note: str,
    fit: str,
    url: str,
) -> dict[str, str]:
    text = " ".join(bit for bit in (candidate, note, fit, url) if bit).lower()
    if any(token in text for token in ("responses api", "response api", "model response", "tool-using", "streaming")):
        return {
            "best_for": "interactive or tool-using request flows",
            "operational_model": "request/response workflow with iterative calls",
            "tradeoff": "less cost-efficient than batch for very large asynchronous jobs",
        }
    if any(token in text for token in ("batch api", "create batch", "batches", "jsonl", "bulk", "completion_window", "asynchronous")):
        return {
            "best_for": "bulk asynchronous workloads",
            "operational_model": "file-backed batch execution",
            "tradeoff": "higher latency and weaker fit for interactive request/response flows",
        }
    if "background" in text:
        return {
            "best_for": "long-running tasks without holding the client request open",
            "operational_model": "background execution with later retrieval or polling",
            "tradeoff": "complements, but does not replace, bulk batch processing",
        }
    if fit == "project-native source":
        return {
            "best_for": "direct product-side comparison context",
            "operational_model": "product-native comparison page",
            "tradeoff": "may be narrower than broader ecosystem analysis",
        }
    if fit == "canonical ground truth":
        return {
            "best_for": "canonical product guidance",
            "operational_model": "first-party product documentation",
            "tradeoff": "may describe capabilities more than head-to-head trade-offs",
        }
    if fit == "supporting analysis":
        return {
            "best_for": "secondary validation and implementation nuance",
            "operational_model": "supporting vendor or official documentation",
            "tradeoff": "usually complements, rather than replaces, canonical guidance",
        }
    return {
        "best_for": fit or "general comparison coverage",
        "operational_model": "comparison-oriented supporting source",
        "tradeoff": "requires cross-checking against canonical product documentation",
    }


def research_build_operational_tradeoffs(
    *,
    focus_rows: Sequence[Mapping[str, Any]],
) -> list[str]:
    tradeoffs: list[str] = []
    responses_candidate = ""
    batch_candidate = ""
    background_candidate = ""
    for row in focus_rows[:4]:
        candidate = str(row.get("candidate") or "").strip()
        candidate_lower = candidate.lower()
        if not responses_candidate and ("responses" in candidate_lower or "response" in candidate_lower):
            responses_candidate = candidate
        if not batch_candidate and "batch" in candidate_lower:
            batch_candidate = candidate
        if not background_candidate and "background" in candidate_lower:
            background_candidate = candidate
    if responses_candidate and batch_candidate:
        tradeoffs.append(
            f"Interaction model: {responses_candidate} is stronger for interactive or tool-using request flows, while {batch_candidate} is stronger for bulk asynchronous workloads."
        )
        tradeoffs.append(
            f"Latency model: {responses_candidate} keeps a request/response loop, while {batch_candidate} trades latency for discounted high-volume execution."
        )
        tradeoffs.append(
            f"Cost and scale: {batch_candidate} is the better fit when discounted throughput matters more than immediate answers."
        )
    if background_candidate:
        anchor = responses_candidate or "the request/response path"
        tradeoffs.append(
            f"Asynchronous execution: {background_candidate} complements {anchor} when work should continue after handoff without keeping the client request open."
        )
    return tradeoffs[:4]


def research_build_decision_checklist(
    *,
    focus_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    checklist: list[dict[str, str]] = []
    responses_candidate = ""
    batch_candidate = ""
    background_candidate = ""
    for row in focus_rows[:4]:
        candidate = str(row.get("candidate") or "").strip()
        lowered = candidate.lower()
        if not responses_candidate and ("responses" in lowered or "response" in lowered):
            responses_candidate = candidate
        if not batch_candidate and "batch" in lowered:
            batch_candidate = candidate
        if not background_candidate and "background" in lowered:
            background_candidate = candidate
    if responses_candidate and batch_candidate:
        checklist.extend(
            [
                {
                    "factor": "Task duration",
                    "prefer": responses_candidate,
                    "rationale": "better when the answer needs to come back in an interactive request/response loop",
                },
                {
                    "factor": "Workload volume",
                    "prefer": batch_candidate,
                    "rationale": "better when the job is bulk, asynchronous, and throughput-sensitive",
                },
                {
                    "factor": "Latency sensitivity",
                    "prefer": responses_candidate,
                    "rationale": "better when immediate feedback matters more than discounted offline throughput",
                },
                {
                    "factor": "Cost sensitivity",
                    "prefer": batch_candidate,
                    "rationale": "better when discounted high-volume execution matters more than immediate completion",
                },
            ]
        )
    if background_candidate:
        checklist.append(
            {
                "factor": "Asynchronous continuation",
                "prefer": background_candidate,
                "rationale": "better when work should continue after handoff without holding the client request open",
            }
        )
    return checklist[:5]


def research_decision_strengths(
    *,
    cluster_label: str,
    provider_support: str,
    note: str,
    cluster_detail: dict[str, Any],
) -> str:
    strength_bits = [research_cluster_fit_summary(cluster_label)]
    tier = str(cluster_detail.get("tier") or "").strip()
    if tier:
        strength_bits.append(tier)
    if provider_support and provider_support != "unknown":
        strength_bits.append(f"provider support={provider_support}")
    if note:
        strength_bits.append(note[:100])
    return "; ".join(bit for bit in strength_bits if bit)


def research_decision_cautions(
    *,
    cluster_label: str,
    provider_support: str,
) -> str:
    cautions: list[str] = []
    if cluster_label in {"community", "directory", "listicle"}:
        cautions.append("lower authority")
    if " + " not in provider_support and provider_support not in {"", "unknown"}:
        cautions.append("single-provider support")
    return "; ".join(cautions) if cautions else "none"


def research_build_comparison_matrix(
    *,
    focus_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    matrix: list[dict[str, str]] = []
    for row in focus_rows[:3]:
        candidate = str(row.get("candidate") or "").strip()
        if not candidate:
            continue
        profile = research_comparison_profile(
            candidate=candidate,
            note=str(row.get("note") or "").strip(),
            fit=research_cluster_fit_summary(str(row.get("cluster") or "").strip()),
            url=str(row.get("url") or "").strip(),
        )
        matrix.append(
            {
                "candidate": candidate,
                "best_for": profile["best_for"],
                "operational_model": profile["operational_model"],
                "tradeoff": profile["tradeoff"],
            }
        )
    return matrix[:3]
