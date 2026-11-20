"""Generate a seeded, stratified holdout; stdout is CSV, never production config."""
from __future__ import annotations

import argparse
import csv
import random
import secrets
import sys
from urllib.parse import urlparse


# This catalog is independent of the fixed regression questions. Sample only after code freeze.
DOCS = (
    ("Python contextlib contextmanager", "https://docs.python.org/3/library/contextlib.html", "contextmanager|yield"),
    ("Python csv DictReader", "https://docs.python.org/3/library/csv.html", "DictReader|newline"),
    ("MDN AbortController abort signal", "https://developer.mozilla.org/en-US/docs/Web/API/AbortController", "AbortController|AbortSignal"),
    ("pytest fixture teardown", "https://docs.pytest.org/en/stable/how-to/fixtures.html", "fixture|teardown"),
)
CATALOG = {
    "web": (
        ("coral bleaching causes", "https://oceanservice.noaa.gov/facts/coral_bleach.html", ""),
        ("why Earth has seasons", "https://spaceplace.nasa.gov/seasons/en/", ""),
        ("how geothermal energy works", "https://www.energy.gov/eere/geothermal/geothermal-basics", ""),
        ("how plate tectonics causes earthquakes", "https://www.usgs.gov/programs/earthquake-hazards", ""),
        ("how photosynthesis works", "https://www.britannica.com/science/photosynthesis", ""),
        ("how tides are generated", "https://oceanservice.noaa.gov/facts/tides.html", ""),
    ),
    "docs": DOCS,
    "github": tuple((f"{repo} repository {symbol}", f"https://github.com/{repo}", "") for repo, symbol in (
        ("redis/redis", "streams"), ("rust-lang/rust", "async"), ("golang/go", "context"), ("pallets/flask", "blueprints"),
    )),
    "pdf": (
        ("Attention Is All You Need paper PDF", "https://arxiv.org/pdf/1706.03762", ""),
        ("Deep Residual Learning for Image Recognition paper PDF", "https://arxiv.org/pdf/1512.03385", ""),
        ("Adam A Method for Stochastic Optimization paper PDF", "https://arxiv.org/pdf/1412.6980", ""),
        ("BERT Pre-training of Deep Bidirectional Transformers paper PDF", "https://arxiv.org/pdf/1810.04805", ""),
    ),
    "news": tuple((f"{name} latest news", url, "") for name, url in (
        ("NASA Artemis", "https://www.nasa.gov/"), ("ESA space exploration", "https://www.esa.int/"),
        ("NOAA climate", "https://www.noaa.gov/"), ("CERN particle physics", "https://home.cern/"),
    )),
    "status": tuple((f"{name} current service status incidents", url, "") for name, url in (
        ("Cloudflare", "https://www.cloudflarestatus.com/"), ("GitHub", "https://www.githubstatus.com/"),
        ("DigitalOcean", "https://status.digitalocean.com/"), ("Atlassian", "https://status.atlassian.com/"),
    )),
    "price": tuple((f"{name} current pricing official", url, "") for name, url in (
        ("Supabase", "https://supabase.com/pricing"), ("Netlify", "https://www.netlify.com/pricing/"),
        ("Vercel", "https://vercel.com/pricing"), ("Cloudflare Workers", "https://developers.cloudflare.com/workers/platform/pricing/"),
    )),
    "research": (
        ("compare PostgreSQL GIN and B-tree indexes tradeoffs", "https://www.postgresql.org/docs/", ""),
        ("compare SQLite WAL and rollback journal concurrency", "https://www.sqlite.org/", ""),
        ("compare Python asyncio and multiprocessing for CPU and IO workloads", "https://docs.python.org/3/", ""),
        ("compare Redis and Memcached eviction and persistence", "https://redis.io/", ""),
    ),
    "social": tuple((f"latest {handle} X posts", "https://x.com/", "") for handle in ("@rustlang", "@golang", "@mozilla", "@NASA")),
}
# name, catalog, domain, mode, strategy, tool, strict-domain, full-content
PROFILES = (
    ("web-fast", "web", "网页", "web", "fast", "search", False, False),
    ("web-auto", "web", "网页", "auto", "", "search", False, False),
    ("web-balanced", "web", "网页", "web", "balanced", "search", False, False),
    ("docs-strict", "docs", "技术文档", "docs", "verify", "search", True, True),
    ("docs-open", "docs", "技术文档", "docs", "balanced", "search", False, True),
    ("github", "github", "GitHub", "github", "balanced", "search", True, True),
    ("release", "github", "更新日志 / release", "github", "verify", "search", True, True),
    ("pdf", "pdf", "PDF", "pdf", "verify", "search", True, True),
    ("news", "news", "新闻", "news", "verify", "search", False, False),
    ("status", "status", "技术动态 / status", "web", "verify", "search", False, False),
    ("price", "price", "物品价格", "web", "verify", "search", True, True),
    ("research", "research", "长尾研究 / 学术比较", "research", "deep", "research", False, True),
    ("social", "social", "纯 Social / X", "social", "verify", "search", False, False),
    ("extract", "docs", "正文抓取", "content", "balanced", "extract_url", False, True),
    ("map", "docs", "站点地图", "map", "balanced", "map_site", False, False),
    ("crawl", "docs", "站点爬取", "crawl", "balanced", "crawl_site", False, True),
)


def generate_rows(seed: int, fieldnames: list[str]) -> list[dict[str, str]]:
    rng = random.Random(seed)
    rows = []
    for name, catalog, domain, mode, strategy, tool, strict, content in PROFILES:
        for index, (topic, url, patterns) in enumerate(rng.sample(CATALOG[catalog], 2)):
            language = "zh" if index == 0 else "en"
            if name == "release":
                topic = topic.split(" repository ")[0] + " latest release notes"
            query = (rng.choice(("请查找：{}，给出来源。", "请说明 {}，并引用证据。")) if language == "zh"
                     else rng.choice(("Find {} with sources.", "Explain {} and cite the evidence."))).format(topic)
            if tool in {"extract_url", "map_site", "crawl_site"}:
                query = url
            primary = ["semantic_discovery"]
            if strict:
                primary = ["authority_precision"]
            if content:
                primary.append("content_fidelity")
            if name in {"news", "status", "social", "release"}:
                primary += ["freshness_signal", "provider_orchestration"]
            if name == "research":
                primary = ["provider_orchestration", "multi_source_fusion", "content_fidelity"]
            if name in {"map", "crawl"}:
                primary = ["site_coverage", "provider_orchestration"] + (["content_fidelity"] if content else [])
            if name in {"extract", "crawl"}:
                primary.append("assertion_pass_rate")
            row = dict.fromkeys(fieldnames, "")
            row.update(benchmark_id=f"heldout-{name}-{index + 1:02}", domain=domain, query=query,
                       prompt_variant="strict" if strict else "baseline", preferred_tool=tool,
                       mode_hint=mode, strategy_hint=strategy, include_domains=urlparse(url).hostname if strict else "",
                       exclude_domains="reddit.com|medium.com|youtube.com" if name == "docs-strict" else "",
                       expected_focus=primary[0], strict_required=str(strict).lower(),
                       primary_dimensions="|".join(primary), secondary_dimensions="traceability|resilience|efficiency",
                       repeat_runs="2" if tool == "research" else "1" if tool in {"map_site", "crawl_site"} else "3",
                       latency_budget_ms="60000" if tool in {"research", "crawl_site"} else "30000" if name == "social" else "20000",
                       notes=f"holdout; stratum={name}; language={language}; seed={seed}; URL/body assertions are not entailment",
                       sources_hint="web|x" if name == "status" and index else "",
                       expected_url_patterns=url.rsplit("/", 1)[-1] if name == "pdf" else url.removeprefix("https://").rstrip("/") if name != "map" else urlparse(url).hostname + "/",
                       expected_content_patterns=patterns if name in {"extract", "crawl"} else "",
                       include_content=str(content).lower())
            rows.append(row)
    rng.shuffle(rows)
    assert len({row["benchmark_id"] for row in rows}) == len(rows) == 32
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--template", required=True, help="Frozen fixed regression matrix: header only is used")
    args = parser.parse_args()
    seed = args.seed if args.seed is not None else secrets.randbits(64)
    with open(args.template, newline="", encoding="utf-8") as source:
        fields = csv.DictReader(source).fieldnames
    if not fields or "benchmark_id" not in fields:
        parser.error("Template must have a benchmark_id column")
    fields = list(dict.fromkeys([*fields, "include_content"]))
    rows = generate_rows(seed, fields)
    writer = csv.DictWriter(sys.stdout, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    print(f"seed={seed}; rows={len(rows)}", file=sys.stderr)


if __name__ == "__main__":
    main()
