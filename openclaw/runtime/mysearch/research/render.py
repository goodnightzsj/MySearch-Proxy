"""Research report rendering: Markdown assembly from structured sections.

Pure data-to-text transform: no network calls, no provider dependencies, no self state.
Extracted from MySearchClient._render_research_report in clients.py:13324-13596.
"""

from __future__ import annotations

from typing import Any


def render_research_report(sections: dict[str, Any]) -> str:
    if not sections:
        return ""

    lines: list[str] = ["## Executive Summary", sections.get("executive_summary", "").strip()]

    key_findings = [str(item).strip() for item in (sections.get("key_findings") or []) if str(item).strip()]
    if key_findings:
        lines.extend(["", "## Key Findings"])
        for item in key_findings:
            lines.append(f"- {item}")

    evidence_highlights = [
        str(item).strip()
        for item in (sections.get("evidence_highlights") or [])
        if str(item).strip()
    ]
    if evidence_highlights:
        lines.extend(["", "## Evidence Highlights"])
        for item in evidence_highlights:
            lines.append(f"- {item}")

    supporting_context = [
        str(item).strip()
        for item in (sections.get("supporting_context") or [])
        if str(item).strip()
    ]
    if supporting_context:
        lines.extend(["", "## Supporting Context"])
        for item in supporting_context[:3]:
            lines.append(f"- {item}")

    consensus_snapshot = [
        str(item).strip()
        for item in (sections.get("consensus_snapshot") or [])
        if str(item).strip()
    ]
    if consensus_snapshot:
        lines.extend(["", "## Consensus Snapshot"])
        for item in consensus_snapshot:
            lines.append(f"- {item}")

    claim_evidence = [
        item
        for item in (sections.get("claim_evidence") or [])
        if isinstance(item, dict) and item.get("claim")
    ]
    if claim_evidence:
        lines.extend(["", "## Claim-Level Evidence"])
        for item in claim_evidence[:4]:
            claim = str(item.get("claim") or "").strip()
            sources = ", ".join(
                str(source).strip()
                for source in (item.get("sources") or [])[:3]
                if str(source).strip()
            )
            providers = ", ".join(
                str(provider).strip()
                for provider in (item.get("providers") or [])[:3]
                if str(provider).strip()
            )
            clusters = ", ".join(
                str(cluster).strip()
                for cluster in (item.get("clusters") or [])[:3]
                if str(cluster).strip()
            )
            support_level = str(item.get("support_level") or "").strip()
            support_basis = str(item.get("support_basis") or "").strip()
            suffix_bits = [
                f"Support: {support_level}" if support_level else "",
                f"Basis: {support_basis}" if support_basis else "",
                f"Sources: {sources}" if sources else "",
                f"Providers: {providers}" if providers else "",
                f"Clusters: {clusters}" if clusters else "",
            ]
            suffix = "; ".join(bit for bit in suffix_bits if bit)
            if suffix:
                lines.append(f"- {claim} ({suffix})")
            else:
                lines.append(f"- {claim}")

    comparison_lens = [
        str(item).strip()
        for item in (sections.get("comparison_lens") or [])
        if str(item).strip()
    ]
    if comparison_lens:
        lines.extend(["", "## Comparison Lens"])
        for item in comparison_lens:
            lines.append(f"- {item}")

    comparison_rows = [
        item
        for item in (sections.get("comparison_rows") or [])
        if isinstance(item, dict) and item.get("candidate")
    ]
    if comparison_rows:
        lines.extend(
            [
                "",
                "## Ranked Shortlist",
                "| Candidate | Cluster | Provider Support | Evidence Note |",
                "|---|---|---|---|",
            ]
        )
        for row in comparison_rows[:4]:
            candidate = str(row.get("candidate") or "").replace("|", "/").strip()
            cluster = str(row.get("cluster") or "").replace("|", "/").strip()
            provider_support = str(row.get("provider_support") or "").replace("|", "/").strip()
            note = str(row.get("note") or "").replace("|", "/").strip()
            lines.append(f"| {candidate} | {cluster} | {provider_support} | {note} |")

    decision_table = [
        item
        for item in (sections.get("decision_table") or [])
        if isinstance(item, dict) and item.get("candidate")
    ]
    if decision_table:
        lines.extend(
            [
                "",
                "## Decision Table",
                "| Candidate | Best Fit | Strengths | Cautions |",
                "|---|---|---|---|",
            ]
        )
        for row in decision_table[:4]:
            candidate = str(row.get("candidate") or "").replace("|", "/").strip()
            fit = str(row.get("fit") or "").replace("|", "/").strip()
            strengths = str(row.get("strengths") or "").replace("|", "/").strip()
            cautions = str(row.get("cautions") or "").replace("|", "/").strip()
            lines.append(f"| {candidate} | {fit} | {strengths} | {cautions} |")

    decision_criteria = [
        str(item).strip()
        for item in (sections.get("decision_criteria") or [])
        if str(item).strip()
    ]
    if decision_criteria:
        lines.extend(["", "## Decision Criteria"])
        for item in decision_criteria[:4]:
            lines.append(f"- {item}")

    comparison_matrix = [
        item
        for item in (sections.get("comparison_matrix") or [])
        if isinstance(item, dict) and item.get("candidate")
    ]
    if comparison_matrix:
        lines.extend(
            [
                "",
                "## Comparison Matrix",
                "| Candidate | Best For | Operational Model | Trade-off |",
                "|---|---|---|---|",
            ]
        )
        for row in comparison_matrix[:4]:
            candidate = str(row.get("candidate") or "").replace("|", "/").strip()
            best_for = str(row.get("best_for") or "").replace("|", "/").strip()
            operational_model = str(row.get("operational_model") or "").replace("|", "/").strip()
            tradeoff = str(row.get("tradeoff") or "").replace("|", "/").strip()
            lines.append(
                f"| {candidate} | {best_for} | {operational_model} | {tradeoff} |"
            )

    operational_tradeoffs = [
        str(item).strip()
        for item in (sections.get("operational_tradeoffs") or [])
        if str(item).strip()
    ]
    if operational_tradeoffs:
        lines.extend(["", "## Operational Trade-offs"])
        for item in operational_tradeoffs[:4]:
            lines.append(f"- {item}")

    decision_checklist = [
        item
        for item in (sections.get("decision_checklist") or [])
        if isinstance(item, dict) and item.get("factor")
    ]
    if decision_checklist:
        lines.extend(
            [
                "",
                "## Decision Checklist",
                "| Factor | Prefer | Why |",
                "|---|---|---|",
            ]
        )
        for row in decision_checklist[:5]:
            factor = str(row.get("factor") or "").replace("|", "/").strip()
            prefer = str(row.get("prefer") or "").replace("|", "/").strip()
            rationale = str(row.get("rationale") or "").replace("|", "/").strip()
            lines.append(f"| {factor} | {prefer} | {rationale} |")

    provider_roles = [
        str(item).strip()
        for item in (sections.get("provider_roles") or [])
        if str(item).strip()
    ]
    if provider_roles:
        lines.extend(["", "## Provider Contributions"])
        for item in provider_roles:
            lines.append(f"- {item}")

    coverage_bits = [
        str(item).strip()
        for item in (sections.get("coverage_bits") or [])
        if str(item).strip()
    ]
    if coverage_bits:
        lines.extend(["", "## Coverage", f"- {' | '.join(coverage_bits)}"])

    source_mix = [
        str(item).strip()
        for item in (sections.get("source_mix") or [])
        if str(item).strip()
    ]
    if source_mix:
        lines.extend(["", "## Source Mix", f"- {' | '.join(source_mix)}"])

    source_clusters = [
        item
        for item in (sections.get("source_clusters") or [])
        if isinstance(item, dict) and item.get("label")
    ]
    if source_clusters:
        lines.extend(["", "## Source Clusters"])
        for cluster in source_clusters[:5]:
            label = str(cluster.get("label") or "").strip()
            count = int(cluster.get("count") or 0)
            tier = str(cluster.get("tier") or "").strip()
            weight = float(cluster.get("weight") or 0)
            domains = ", ".join(
                str(domain).strip()
                for domain in (cluster.get("domains") or [])[:3]
                if str(domain).strip()
            )
            providers = ", ".join(
                str(provider).strip()
                for provider in (cluster.get("providers") or [])[:3]
                if str(provider).strip()
            )
            detail_bits = [
                f"{count} source(s)" if count else "",
                f"tier={tier}" if tier else "",
                f"weight={weight:.1f}" if weight else "",
                f"domains={domains}" if domains else "",
                f"providers={providers}" if providers else "",
            ]
            lines.append(f"- {label}: {'; '.join(bit for bit in detail_bits if bit)}")

    social_signal = str(sections.get("social_signal") or "").strip()
    if social_signal:
        lines.extend(["", "## Social Signal", f"- {social_signal}"])

    recommendation = str(sections.get("recommendation") or "").strip()
    if recommendation:
        lines.extend(["", "## Recommendation", f"- {recommendation}"])

    caveats = [str(item).strip() for item in (sections.get("caveats") or []) if str(item).strip()]
    top_sources = [str(item).strip() for item in (sections.get("top_sources") or []) if str(item).strip()]
    if caveats:
        lines.extend(["", "## Caveats"])
        for item in caveats:
            lines.append(f"- {item}")
    if top_sources:
        lines.extend(["", "## Top Sources"])
        for item in top_sources[:3]:
            lines.append(f"- {item}")

    return "\n".join(lines).strip()
