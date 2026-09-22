"""benchmark 失效模式行：**由真实缺陷驱动**的行集规格。

`matrix` 输入 CSV 在 `.codex-tasks/` 下、被 gitignore，所以它不进仓库、
CI 看不到。这份模块是那些行的**权威规格**，放这里的原因正是它需要被
版本控制：CI 的全新 clone 必须能校验"两次真实缺陷是否已被覆盖"。

判据来源（全部是真实使用中出现的缺陷，不是推测）：

- `VERSION_ATTRIBUTION`：`latest stable version of Java` 反复吐出别的产品的
  版本号 —— `26.1.2`（Minecraft Java Edition）、`10.7.3`（JavaFX）、
  `25.12`（Aspose.Cells for Node.js via Java）。
- `SOCIAL_FIELDS`：`results[]` 除 url 外全空，旧矩阵给该形状 35.73/31.67 分。
- `AWARD_ENTITY`：奖项抽取取到提名名单首项或相邻类别。

新增行必须**可证伪**（`scripts/audit_matrix_assertions.py` 会拒绝恒真断言）。
"""

from __future__ import annotations

from typing import NamedTuple


class FailureModeRow(NamedTuple):
    benchmark_id: str
    domain: str
    query: str
    mode_hint: str
    strategy_hint: str
    primary_dimensions: str
    secondary_dimensions: str
    latency_budget_ms: str
    expected_url_patterns: str
    expected_answer_patterns: str
    notes: str


#: `latest stable version of Java` 的四个编造变体。断言写成**归属式**
#: （`java 26|java 25`）而不是裸版本号：实测 `pattern=['25']` 会放行编造的
#: `25.12`，`pattern=['java 25']` 对四个变体全部拒绝。
VERSION_ATTRIBUTION = FailureModeRow(
    benchmark_id="failure-version-attribution-01",
    domain="事实型版本查询",
    query="latest stable version of Java",
    mode_hint="web",
    strategy_hint="balanced",
    primary_dimensions="claim_groundedness|assertion_pass_rate",
    secondary_dimensions="authority_precision|traceability",
    latency_budget_ms="20000",
    expected_url_patterns="",
    expected_answer_patterns="java 26|java 25",
    notes=(
        "失效模式行。断言必须带主语：裸版本号会放行张冠李戴（25.12 是 Aspose 的版本）。"
    ),
)

SOCIAL_FIELDS = FailureModeRow(
    benchmark_id="failure-social-fields-01",
    domain="纯 Social / X",
    query="latest OpenAI X posts GPT-5",
    mode_hint="social",
    strategy_hint="verify",
    primary_dimensions="assertion_pass_rate|semantic_discovery",
    secondary_dimensions="traceability|resilience",
    latency_budget_ms="30000",
    expected_url_patterns="",
    expected_answer_patterns="",
    notes=(
        "失效模式行。由 assertion_pass_rate 的 `_SOCIAL_REQUIRED_FIELDS` 检查判定："
        "title/snippet/author 必须有内容，缺键与空串都算未填。"
    ),
)

AWARD_ENTITY = FailureModeRow(
    benchmark_id="failure-award-entity-01",
    domain="娱乐",
    query="2026 Grammy Record of the Year winner",
    mode_hint="news",
    strategy_hint="verify",
    primary_dimensions="freshness_signal|assertion_pass_rate",
    secondary_dimensions="provider_orchestration|traceability",
    latency_budget_ms="30000",
    expected_url_patterns="grammy",
    expected_answer_patterns="luther",
    notes=(
        "失效模式行。已核实真值（grammy.com 官方分类页）：2026 Record of the Year "
        "是 luther（Kendrick Lamar with SZA）；DtMF 是 **Album** of the Year —— "
        "相邻类别陷阱。断言只写 winner 实体，不写类别词（题干里恒真）。"
    ),
)

#: 全部失效模式行。测试与矩阵生成器都从这里取，避免两处定义漂移。
FAILURE_MODE_ROWS: tuple[FailureModeRow, ...] = (
    VERSION_ATTRIBUTION,
    SOCIAL_FIELDS,
    AWARD_ENTITY,
)


def as_matrix_row(row: FailureModeRow) -> dict[str, str]:
    """转成输入矩阵的一行（补齐固定列）。"""
    return {
        "benchmark_id": row.benchmark_id,
        "domain": row.domain,
        "query": row.query,
        "prompt_variant": "baseline",
        "preferred_tool": "search",
        "mode_hint": row.mode_hint,
        "strategy_hint": row.strategy_hint,
        "include_domains": "",
        "exclude_domains": "",
        "expected_focus": row.primary_dimensions.split("|")[0],
        "strict_required": "false",
        "primary_dimensions": row.primary_dimensions,
        "secondary_dimensions": row.secondary_dimensions,
        "repeat_runs": "3",
        "latency_budget_ms": row.latency_budget_ms,
        "notes": row.notes,
        "sources_hint": "",
        "expected_url_patterns": row.expected_url_patterns,
        "expected_answer_patterns": row.expected_answer_patterns,
    }
