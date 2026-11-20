# 搜索评测合同

`search-matrix.csv` 是默认输入与 CI 检查的共同来源；旧 `.codex-tasks/` 矩阵仅作历史记录。原始结果仍写入本地任务目录，不随仓库发布。

## 本轮变更

- `scoring_version=2026-11-17-evidence-v4`：输出标记当前评分公式版本，不代表旧 raw 已补齐。更换合同后必须重跑完整矩阵，旧结果不得计入新的连续 clean loop。
- 重复请求按正文长度、引用数选择中位成功样本；summary、URLs、正文量与保存的 raw 必须来自同一样本。延迟和稳定性仍使用全部重复观察。
- `diagnostics` 子树在任意深度均不参与 provider、fallback 与发布日期计分；矩阵和权重不变，两侧须用同一 runner 新采样。
- 全部逻辑重复调用另外保存在 `raw/repeats/`：成功及工具错误保存完整 MCP 响应，调用异常保存错误、阶段及异常类型。现有 observations 列记录真实 `run`、`raw_path`、`raw_sha256` 和 `scored_sample`；全部失败也保留记录，不选取成功样本。选中 raw 与 100 列 CSV 保持兼容，旧工件不改写。
- 恢复新版 CSV 时同时核验重复工件路径和哈希；缺失或内容变化会显式报错，不自动重采样。
- 重复响应在远端整体脱敏后才落盘，哈希针对本地持久化后的 UTF-8 内容。证据粒度是一次 `call_tool`，不代表内部 HTTP 重试；进程被强制终止前未完成返回的调用不声称已有完整证据。
- 分批运行与 `--reuse-output-csv` 会按 notes 中的原始路径恢复 raw；`--mysearch-only` 保留对照正文。引用的 raw 丢失会显式报错，请连同 CSV 保留原文件及相对路径，不能只搬运 CSV。
- `claim_groundedness` 只回查本侧结果、抓取页面及检索分支中的正文/snippet，不读生成的 answer、summary、report、标题、URL 或 metadata。有事实 token 却无来源文本时为0；无事实 token 时不扣分。它只检查文本命中，不证明主语归属或事实蕴含。
- `expected_content_patterns` 使用 `|` 分隔**所有必需正文片段**，忽略大小写并折叠空白。每个片段分别判定，通过比例作为 `assertion_pass_rate` 的一个断言组；不能用 URL、标题、生成的 answer/summary、metadata 或搜索 snippet 代替抓取正文。
- 抽取和 crawl 只读取顶层及 `results[]` / `pages[]` 的 `content/raw_content/markdown/text`。地图没有正文，只检验 `links[]` 中发现的非输入页面；不受展示 top-3 截断影响。
- `ranking_quality = 5 × reciprocal-rank@3`：在返回的 `results[]` 中找首个匹配 `expected_url_patterns` 的结果，第 1/2/3 名分别为 5、2.5、1.67，未进入前三为 0。独立 citations 与输入 URL 不参与排序。仅 9 个有明确目标的行启用该维度；未声明判断的行不计入默认总分。该指标不声称衡量完整语义相关性。

目前 48 行均有至少一种可证伪断言；这不代表所有语义错误均可被发现。正文片段命中不是蕴含验证，现有 URL/答案期望也需要随页面和事实变化复核。

2026-10-12依据[Python 3.15.0 官方发布页](https://www.python.org/downloads/release/python-3150/)将版本题期望从3.14更新为3.15（稳定版发布于2026-10-09）。不修改历史CSV/raw、评分公式或延迟预算；更新后须重新完整采样。普通搜索的URL断言仍检查展示的前三条URL，不能据此断言全部结果均未找到目标；没有答案断言的行也不能验证答案的语义正确性。

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

MySearch 侧通过 MCP 使用目标实例的配置；在 `proxy-first` 接线下，搜索、抽取和 bench 都由该 Proxy 的活跃池选 Key，不在 runner 里抽取原始 provider Key。Exa 的 `search` / `contents` 各自遵守操作限制。显式 provider 直连配置会绕过 Proxy，评测前应核对接线；生产与独立 bench 数据库不会自动同步池状态，不能用生产页面的数量代表 bench。Tavily MCP 对照组继续使用自己的上游池，不随 MySearch 本地池切换。

远端 runner 上传的脚本不含凭证，载荷仅经 SSH stdin 传入子进程；私有目录和文件权限为 0700/0600，嵌套 JSON 的凭证回显也会脱敏。完整成功输出才会原子发布；超时、上传失败和子进程崩溃保留各自失败语义。

每轮同时保存 runtime revision、runner/矩阵哈希和实际缓存 TTL。`cold/warm` 表示重复顺序，不证明是否命中缓存；独立 bench 关闭 search 缓存时，extract/social 缓存仍可能生效。

## 多维未见题

固定48行用于回归；泛化结论另用 `scripts/generate_heldout_matrix.py` 在候选代码冻结后生成32行分层样本。主题库与固定集不同，每层随机选主题/措辞，覆盖中英文、5种工具、来源/时间约束、正文开关和多种策略；URL型工具的语言标签只代表层配额，实际参数仍是URL。

保存随机seed、候选commit、生成器/runner/CSV哈希、每行工具参数及断言来源。两侧用同一冻结输入和评分，不用不同随机题对比；所有失败和重复原始响应必须保留。评测后修代码须重新抽样，不能沿用已见题声称泛化。URL/片段命中不证明语义正确性，顺序A/B仍受上游与池状态漂移影响。

```bash
python scripts/generate_heldout_matrix.py --template benchmarks/search-matrix.csv
```
