#!/usr/bin/env python3
"""探测 grok2api 上游，选出 free 号当前可用的最新模型并写入 Proxy 配置。

## 为什么需要"探测"而不是直接读某个列表

上游有三个能列模型的来源，但**没有一个是可信的可用性判据**（2026-09-17 实测）：

| 来源 | 含 `-non-reasoning`（实测 200） | 含 `grok-4.20-0309`（实测 404） |
|---|---|---|
| `GET /v1/models` | 有 | 有（**误报**） |
| `GET /api/admin/v1/models` | 无（**漏报**） | 无（正确） |
| 真实推理调用 | 有 | 无（正确） |

所以本脚本把前两者只当**候选来源**（取并集，互相弥补漏报），
再用真实推理调用逐个验证——只有探测通过的才可能被选用。

## 用法

    # 只看会选什么，不写入（默认）
    python scripts/refresh_grok_models.py --dry-run

    # 写入 Proxy 的 social_model / social_fallback_model
    python scripts/refresh_grok_models.py --apply

Probe 结果以 JSON 输出便于机器消费：

    python scripts/refresh_grok_models.py --json

连接信息默认从环境变量读，与 Proxy 的约定一致：
`SOCIAL_GATEWAY_UPSTREAM_BASE_URL`、`SOCIAL_GATEWAY_ADMIN_USERNAME`、
`SOCIAL_GATEWAY_ADMIN_PASSWORD`、`SOCIAL_GATEWAY_UPSTREAM_API_KEY`。

注意：本脚本**不修改源代码**里的 `_BUILTIN_GROK_MODELS`。那个常量只在
「没有任何 DB 覆盖」的零配置部署上生效，改它需要人工确认，属独立变更。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
# 脚本可能被复制到 repo 之外执行（例如推到目标机上跑），此时 parents[1] 不是仓库根。
# 按"本脚本目录"和"其父目录"两者探测，任一含 mysearch/ 即可。
for _candidate in (Path(__file__).resolve().parent, REPO_ROOT):
    if (_candidate / "mysearch").is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from mysearch.grok_model_refresh import (  # noqa: E402
    build_probe_payload,
    collect_candidates_from_model_list,
    collect_text_candidates,
    evaluate_search_probe,
    merge_candidates,
    pick_primary_and_fallback,
)

V3_ADMIN_PREFIX = "/api/admin/v1"
PROBE_TIMEOUT_SECONDS = 120
MAX_PROBE_COUNT = 8


def _root_base_url(base_url):
    """把上游 base_url 归一为站点根。

    `SOCIAL_GATEWAY_UPSTREAM_BASE_URL` 常带 `/v1` 后缀（OpenAI 兼容端点约定），
    而本脚本要拼接的三类路径**各自已含完整前缀**：

    - 推理：  `/v1/responses`
    - 模型表：`/v1/models`
    - admin： `/api/admin/v1/models`

    若保留 `/v1` 再拼，会得到 `/v1/v1/responses`、`/v1/api/admin/v1/...` 这类
    双前缀地址，一律 404。所以这里统一剥到站点根，只在一处维护归一逻辑。
    """
    normalized = str(base_url or "").strip().rstrip("/")
    for suffix in ("/v1/responses", "/v1/models", "/v1", "/responses", "/api/tavily"):
        if normalized.endswith(suffix):
            return normalized[: -len(suffix)].rstrip("/")
    return normalized


def _request(url, *, method="GET", body=None, headers=None, timeout=30):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
        return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:  # 网络层失败不该让整个刷新崩掉
        return None, str(exc).encode("utf-8")


def admin_login(base_url, username, password):
    status, raw = _request(
        f"{base_url}{V3_ADMIN_PREFIX}/auth/login",
        method="POST",
        body={"username": username, "password": password},
        timeout=30,
    )
    if status != 200:
        raise RuntimeError(f"admin login failed (HTTP {status})")
    payload = json.loads(raw)
    token = (((payload.get("data") or {}).get("tokens") or {}).get("accessToken") or "").strip()
    if not token:
        raise RuntimeError("admin login returned no access token")
    return token


def fetch_json(base_url, path, token=None, timeout=30):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    status, raw = _request(f"{base_url}{path}", headers=headers, timeout=timeout)
    if status != 200:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def probe_model(base_url, api_key, model_id):
    """真实搜索探测。返回 `(search_capable, 证据)`。

    **必须发送真实的 x_search 工具并校验它被调用**——只看 HTTP 200 不够：
    实测 `grok-4.6` / `grok-4.5` 返回 200 却完全不调用工具，而是凭训练数据
    编造 X 帖子（假 status ID、整点时间戳、事实错误）。若只验 200，这类模型
    会被选为搜索主模型，产出看似正常实则捏造的结果。
    """
    status, raw = _request(
        f"{base_url}/v1/responses",
        method="POST",
        body=build_probe_payload(model_id),
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    if status != 200:
        return False, {"http_status": status, "reason": "http_error"}
    try:
        payload = json.loads(raw)
    except Exception:
        return False, {"http_status": status, "reason": "invalid_json"}
    verdict = evaluate_search_probe(payload)
    verdict["http_status"] = status
    return verdict["search_capable"], verdict



def collect_candidates(root_base, admin_token, api_key):
    """从两个端点取候选并集。两个都失败时返回空表（调用方应保留现有配置）。"""
    candidates: list[list[str]] = []
    admin_payload = fetch_json(root_base, f"{V3_ADMIN_PREFIX}/models", admin_token) if admin_token else None
    if admin_payload:
        candidates.append(collect_text_candidates(admin_payload.get("data") or admin_payload))
    list_payload = fetch_json(root_base, "/v1/models", api_key)
    if list_payload:
        candidates.append(collect_candidates_from_model_list(list_payload))
    return merge_candidates(*candidates)


def probe_candidates(base_url, api_key, candidates, *, want=2, limit=MAX_PROBE_COUNT):
    """按偏好顺序探测，凑够 want 个**支持搜索**的模型即停。

    注意：判定标准是"真的调用了搜索工具"，不是 HTTP 200。
    """
    capable: list[str] = []
    probes: list[dict] = []
    for model_id in candidates[:limit]:
        ok, evidence = probe_model(base_url, api_key, model_id)
        probes.append({"model": model_id, "search_capable": ok, **evidence})
        if ok:
            capable.append(model_id)
            if len(capable) >= want:
                break
    return capable, probes


def read_proxy_settings(db_path):
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        return {
            key: (value or "")
            for key, value in conn.execute("SELECT key, value FROM settings")
        }
    finally:
        conn.close()


def write_proxy_models(db_path, primary, fallback):
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('social_model', ?)", (primary,)
        )
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('social_fallback_model', ?)",
            (fallback,),
        )
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=os.environ.get("SOCIAL_GATEWAY_UPSTREAM_BASE_URL", "").rstrip("/"))
    parser.add_argument("--admin-username", default=os.environ.get("SOCIAL_GATEWAY_ADMIN_USERNAME", ""))
    parser.add_argument("--admin-password", default=os.environ.get("SOCIAL_GATEWAY_ADMIN_PASSWORD", ""))
    parser.add_argument("--api-key", default=os.environ.get("SOCIAL_GATEWAY_UPSTREAM_API_KEY", ""))
    parser.add_argument("--db-path", default=os.environ.get("MYSEARCH_PROXY_DB_PATH", ""))
    parser.add_argument("--apply", action="store_true", help="写入 Proxy 配置；省略时只报告")
    parser.add_argument("--dry-run", action="store_true", help="默认行为，显式声明用")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args()

    if not args.base_url:
        print("Missing --base-url or SOCIAL_GATEWAY_UPSTREAM_BASE_URL", file=sys.stderr)
        return 2
    if not args.api_key:
        print("Missing --api-key or SOCIAL_GATEWAY_UPSTREAM_API_KEY", file=sys.stderr)
        return 2

    root_base = _root_base_url(args.base_url)

    admin_token = ""
    if args.admin_username and args.admin_password:
        try:
            admin_token = admin_login(root_base, args.admin_username, args.admin_password)
        except Exception as exc:
            print(f"warn: admin login failed, continuing with /v1/models only: {exc}", file=sys.stderr)

    candidates = collect_candidates(root_base, admin_token, args.api_key)
    if not candidates:
        print("No candidates from either endpoint; keeping current configuration.", file=sys.stderr)
        return 1

    capable, probes = probe_candidates(root_base, args.api_key, candidates)

    # 现有 primary 用于"仍可用则保留"——本脚本的职责是发现失效并恢复，
    # 不是追版本（上游命名跨两套方案，无法从名字可靠判断谁更新）。
    current_primary = ""
    if args.db_path:
        try:
            current_primary = read_proxy_settings(args.db_path).get("social_model", "")
        except Exception as exc:
            print(f"warn: cannot read current social_model: {exc}", file=sys.stderr)

    primary, fallback = pick_primary_and_fallback(capable, current_primary)

    result = {
        "root_base": root_base,
        "candidates": candidates,
        "probes": probes,
        "search_capable": capable,
        "current_primary": current_primary,
        "primary": primary,
        "fallback": fallback,
        "changed": primary != current_primary,
        "applied": False,
    }

    if not primary:
        print(
            "No model passed search probing; keeping current configuration.",
            file=sys.stderr,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1

    if args.apply:
        if not args.db_path:
            print("Missing --db-path or MYSEARCH_PROXY_DB_PATH for --apply", file=sys.stderr)
            return 2
        write_proxy_models(args.db_path, primary, fallback)
        result["applied"] = True

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"candidates: {', '.join(candidates)}")
        for probe in probes:
            mark = "PASS" if probe.get("search_capable") else "FAIL"
            detail = f"tools={probe.get('tool_calls', '-')} ids={probe.get('status_ids', '-')}"
            if probe.get("reason"):
                detail = probe["reason"]
            print(f"  {mark}  {probe['model']:<32} {detail}")
        print(f"current : {current_primary or '(unset)'}")
        print(f"primary : {primary}")
        print(f"fallback: {fallback}")
        print(f"changed : {'yes' if result['changed'] else 'no'}")
        print("applied : " + ("yes" if result["applied"] else "no (dry-run)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
