#!/usr/bin/env python3
"""矩阵断言的**可证伪性**体检（离线，不联网）。

判据来自一条已经被实战证明的纪律：**"测试通过"不是证据，"注入 bug 后失败"
才是**。把它从单测推广到 benchmark 矩阵 —— 一行断言如果在人为破坏答案后
**仍然通过**，那它就是恒真的，等于把一个真空包装成"已覆盖"，比没有断言更糟。

对每一行做三种注入，该行的断言必须各自失败：

  1. 清空 answer      -> 答案类断言必须失败
  2. 清空 results     -> URL 类与字段类断言必须失败
  3. 把 answer 换成源文本里不存在的 token -> groundedness 必须失败

任何一行三种注入都"通过"，说明它的断言恒真 -> 拒绝进矩阵。

用法：
    python3 scripts/audit_matrix_assertions.py [--input-csv PATH] [--json]

退出码：0 全部可证伪；1 存在恒真断言。
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import run_remote_mcp_benchmark as runner  # noqa: E402

DEFAULT_MATRIX = (
    REPO_ROOT
    / ".codex-tasks"
    / "20260530-provider-optimization-loop-v2"
    / "raw"
    / "loop11-benchmark-input-final.csv"
)

#: 注入用的"源里不存在"的假 token。取一个不可能出现在任何真实正文里的形状 ——
#: 若混进真实语料会让 groundedness 假通过。
FABRICATED_TOKEN = "9999.8888.7777"


def _load_matrix(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _blank_row() -> dict[str, object]:
    return {key: "" for key in runner.FIELDNAMES}


def _baseline_row(input_row: dict[str, str]) -> dict[str, object]:
    """一条"看起来正常"的合成结果，作为注入的基底。

    用合成值而不是真实 payload：体检只问"断言对破坏是否敏感"，
    不关心真实内容，合成值更稳定且不依赖网络。
    """
    row = _blank_row()
    row.update(
        {
            "run_status": "captured",
            "mysearch_summary": "Analysis of the topic.",
            "mysearch_top_urls": "https://example.com/one | https://example.com/two",
            "mysearch_citation_count": 2,
            "mysearch_content_char_count": 2000,
            "mysearch_content_item_count": 2,
            "mysearch_published_date_count": 1,
            "mysearch_latency_ms": 1200,
            "tavily_summary": "Analysis of the topic.",
            "tavily_top_urls": "https://example.com/one | https://example.com/two",
            "tavily_citation_count": 2,
            "tavily_content_char_count": 2000,
            "tavily_content_item_count": 2,
            "tavily_published_date_count": 1,
            "tavily_latency_ms": 1200,
        }
    )
    return row


def _score(input_row: dict[str, str], row: dict[str, object]) -> dict[str, object]:
    return runner.score_output_row(input_row, dict(row))


def _passes_any_assertion(
    input_row: dict[str, str], row: dict[str, object]
) -> tuple[bool, dict[str, float]]:
    """这条 row 是否满足该行**声明的**断言。

    没有声明任何断言的行走 `assertion_pass_rate` 的默认 1.0 —— 那种行在体检里
    就是恒真的，应当被拒。返回 `(是否通过, 各断言的通过值)`。
    """
    scored = _score(input_row, row)
    detail = {
        "mysearch_assertion_pass_rate": float(scored.get("mysearch_assertion_pass_rate") or 0.0),
        "tavily_assertion_pass_rate": float(scored.get("tavily_assertion_pass_rate") or 0.0),
        "mysearch_claim_groundedness": float(scored.get("mysearch_claim_groundedness_ratio") or 0.0),
        "tavily_claim_groundedness": float(scored.get("tavily_claim_groundedness_ratio") or 0.0),
    }
    has_declared = bool(
        (input_row.get("expected_answer_patterns") or "").strip()
        or (input_row.get("expected_url_patterns") or "").strip()
    )
    is_social = str(input_row.get("domain", "")).strip().lower() == "纯 social / x"
    if not has_declared and not is_social:
        return True, detail
    return all(value >= 1.0 for value in detail.values()), detail


def audit_row(input_row: dict[str, str]) -> dict[str, object]:
    """对一行做三种注入。

    判定按设计文档的原话：**三项注入"都通过"才说明该行断言恒真**。
    所以"可证伪" = 至少一项注入被检出为失败，而检出的前提是
    **基线本身满足了断言** —— 基线都不满足时，"注入后仍不满足"不是检出，
    只是同一件事发生了两次。

    只测 URL 的行不该因为"清空 answer 未被检出"被判恒真：它本来就不断言答案。
    """
    baseline = _baseline_row(input_row)

    # 让基线的 answer/URL 满足该行的期望，才能真正测"破坏后是否失败"。
    answer_patterns = [
        value for value in runner.parse_pipe_list(input_row.get("expected_answer_patterns", ""))
    ]
    url_patterns = [
        value for value in runner.parse_pipe_list(input_row.get("expected_url_patterns", ""))
    ]
    if answer_patterns:
        baseline["mysearch_summary"] = f"The answer is {answer_patterns[0]}."
        baseline["tavily_summary"] = baseline["mysearch_summary"]
    if url_patterns:
        url = f"https://example.com{url_patterns[0]}" if url_patterns[0].startswith("/") else url_patterns[0]
        baseline["mysearch_top_urls"] = url
        baseline["tavily_top_urls"] = url

    # 基线必须**自洽**：它自己的正文里要含有它自己答案的事实 token。
    # 否则 groundedness 从一开始就不满，后面三项注入全都"未被检出"，
    # 会把这个被测行误判成恒真 —— 那是体检脚本的缺陷，不是矩阵的。
    raw_text = json.dumps(
        {
            "answer": baseline["mysearch_summary"],
            "results": [
                {
                    "url": "https://example.com/one",
                    "title": "One",
                    "snippet": str(baseline["mysearch_summary"]),
                    "author": "a",
                }
            ],
        },
        ensure_ascii=False,
    )
    baseline["mysearch_raw"] = raw_text
    baseline["tavily_raw"] = raw_text

    baseline_ok, _ = _passes_any_assertion(input_row, baseline)

    # 注入 1：清空 answer
    inject1 = copy.deepcopy(baseline)
    inject1["mysearch_summary"] = ""
    inject1["tavily_summary"] = ""
    # 注入 2：清空 results
    inject2 = copy.deepcopy(baseline)
    empty_raw = json.dumps({"answer": "", "results": []}, ensure_ascii=False)
    inject2["mysearch_top_urls"] = ""
    inject2["tavily_top_urls"] = ""
    inject2["mysearch_citation_count"] = 0
    inject2["tavily_citation_count"] = 0
    inject2["mysearch_raw"] = empty_raw
    inject2["tavily_raw"] = empty_raw
    # 注入 3：answer 换成源里没有的 token
    inject3 = copy.deepcopy(baseline)
    fabricated = f"The latest stable version is {FABRICATED_TOKEN}."
    inject3["mysearch_summary"] = fabricated
    inject3["tavily_summary"] = fabricated

    detections = {
        "blank_answer_detected": baseline_ok and not _passes_any_assertion(input_row, inject1)[0],
        "blank_results_detected": baseline_ok and not _passes_any_assertion(input_row, inject2)[0],
        "fabrication_detected": baseline_ok and not _passes_any_assertion(input_row, inject3)[0],
    }
    return {
        "benchmark_id": input_row.get("benchmark_id", ""),
        "domain": input_row.get("domain", ""),
        "baseline_satisfies_assertions": baseline_ok,
        "falsifiable": baseline_ok and any(detections.values()),
        **detections,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-csv", default=str(DEFAULT_MATRIX))
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args()

    path = Path(args.input_csv)
    if not path.exists():
        print(f"matrix not found: {path}", file=sys.stderr)
        return 2

    results = [audit_row(row) for row in _load_matrix(path)]
    offenders = [item for item in results if not item["falsifiable"]]

    if args.json:
        print(json.dumps({"results": results, "offenders": offenders}, ensure_ascii=False, indent=2))
    else:
        header = f"{'benchmark_id':<28} {'blank_ans':<10} {'blank_res':<10} {'fabric':<8} verdict"
        print(header)
        print("-" * len(header))
        for item in results:
            print(
                f"{item['benchmark_id']:<28} "
                f"{str(item['blank_answer_detected']):<10} "
                f"{str(item['blank_results_detected']):<10} "
                f"{str(item['fabrication_detected']):<8} "
                f"{'OK' if item['falsifiable'] else 'TAUTOLOGY'}"
            )
        print()
        print(f"{len(results) - len(offenders)}/{len(results)} rows falsifiable")
        if offenders:
            print("恒真断言（拒绝进矩阵）:")
            for item in offenders:
                print(f"  - {item['benchmark_id']} ({item['domain']})")

    return 1 if offenders else 0


if __name__ == "__main__":
    raise SystemExit(main())
