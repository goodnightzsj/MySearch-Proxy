# 搜索评测合同

`search-matrix.csv` 是默认输入与 CI 检查的共同来源；旧 `.codex-tasks/` 矩阵仅作历史记录。原始结果仍写入本地任务目录，不随仓库发布。

## 本轮变更

- `scoring_version=2026-10-09-evidence-v3`：输出标记当前评分公式版本，不代表旧 raw 已补齐。更换合同后必须重跑完整矩阵，旧结果不得计入新的连续 clean loop。
- 重复请求按正文长度、引用数选择中位成功样本；summary、URLs、正文量与保存的 raw 必须来自同一样本。延迟和稳定性仍使用全部重复观察。
- 分批运行与 `--reuse-output-csv` 会按 notes 中的原始路径恢复 raw；`--mysearch-only` 保留对照正文。引用的 raw 丢失会显式报错，请连同 CSV 保留原文件及相对路径，不能只搬运 CSV。
- `claim_groundedness` 只回查本侧结果、抓取页面及检索分支中的正文/snippet，不读生成的 answer、summary、report、标题、URL 或 metadata。有事实 token 却无来源文本时为0；无事实 token 时不扣分。它只检查文本命中，不证明主语归属或事实蕴含。
- `expected_content_patterns` 使用 `|` 分隔**所有必需正文片段**，忽略大小写并折叠空白。每个片段分别判定，通过比例作为 `assertion_pass_rate` 的一个断言组；不能用 URL、标题、生成的 answer/summary、metadata 或搜索 snippet 代替抓取正文。
- 抽取和 crawl 只读取顶层及 `results[]` / `pages[]` 的 `content/raw_content/markdown/text`。地图没有正文，只检验 `links[]` 中发现的非输入页面；不受展示 top-3 截断影响。
- `ranking_quality = 5 × reciprocal-rank@3`：在返回的 `results[]` 中找首个匹配 `expected_url_patterns` 的结果，第 1/2/3 名分别为 5、2.5、1.67，未进入前三为 0。独立 citations 与输入 URL 不参与排序。仅 9 个有明确目标的行启用该维度；未声明判断的行不计入默认总分。该指标不声称衡量完整语义相关性。

目前 48 行均有至少一种可证伪断言；这不代表所有语义错误均可被发现。正文片段命中不是蕴含验证，现有 URL/答案期望也需要随页面和事实变化复核。

## 新增正文断言的来源

2026-10-09 使用实际提取正文核实：

- [Next.js generateMetadata](https://nextjs.org/docs/app/api-reference/functions/generate-metadata)：`server components`、`automatically memoized`。
- [Apple MacBook Air 中国页](https://www.apple.com.cn/macbook-air/)：`M5`、`18 小时`。
- [FastAPI Background Tasks](https://fastapi.tiangolo.com/tutorial/background-tasks/)：`returning a response`、`add_task`。
- 地图期望发现 FastAPI 的 `/tutorial/` 或 `/advanced/` 文档页，输入根 URL 本身不能通过。

## 验证与运行

```bash
python -m unittest tests.test_benchmark_evidence tests.test_remote_benchmark_config tests.test_ranking_replay
python scripts/audit_matrix_assertions.py
python scripts/run_remote_mcp_benchmark.py --help
```

真实全量对照仍需有效的 SSH host、MySearch/Tavily MCP 端点与 comparator 认证。沿用 `scripts/benchmark.env.example` 的配置约定，不要将实际凭证写入矩阵、命令参数或报告。完整对照须核对运行版本、48 个 ID、两侧 raw 与失败分类；局部 smoke 或合成审计不能算作完整闭环。
