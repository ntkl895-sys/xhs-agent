"""第 23 步 · GitHub 关键词搜索**真实检查**（真实网络；只读 api.github.com）。

与前缀约定一致的退出码：

    0  真实跑通且字段映射断言全部通过
    1  拿到上游响应但断言失败（映射 / 结构不对）
    2  前置或网络不可用（超时 / 连不通 / 限速中）——**打印说明，不报错刷屏**

网络口径：

- 直连 ``GITHUB_API_BASE``（默认 ``https://api.github.com``）；环境里若设置了
  ``HTTPS_PROXY``，请求会显式走它（国内访问用）；localhost 地址绝不走代理；
- 本脚本只发 1 次搜索请求（匿名限速约 10 次/分钟，脚本远低于该口）；
- **只读**：不写库、不写任何平台；令牌（如配置）只出现在请求头，
  任何输出都不打印令牌值。

用法（在**项目根目录**下）：

    backend\\.venv\\Scripts\\python.exe scripts\\check_github_search.py

    # 可选：换关键词 / 条数
    backend\\.venv\\Scripts\\python.exe scripts\\check_github_search.py --query "react hooks" --limit 3
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import settings  # noqa: E402
from app.services.search.github import GitHubProvider  # noqa: E402
from app.services.search.provider import (  # noqa: E402
    SearchRateLimitedError,
    SearchUpstreamError,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="GitHub 关键词搜索真实检查（只读）")
    parser.add_argument("--query", default="langgraph agent", help="搜索关键词（默认 langgraph agent）")
    parser.add_argument("--limit", type=int, default=5, help="返回条数（默认 5，上限 30）")
    parser.add_argument("--sort", default="stars", choices=["stars", "updated"], help="排序（默认 stars）")
    args = parser.parse_args()

    print("=" * 62)
    print(" 第 23 步 · GitHub 关键词搜索真实检查（只读 api.github.com）")
    print("=" * 62)
    token_state = "已配置（请求带 Authorization，本脚本不显示其值）" if (settings.GITHUB_TOKEN or "").strip() else "未配置（匿名，限速约 10 次/分钟）"
    print(f"GITHUB_TOKEN：{token_state}")
    print(f"API 基址：{settings.GITHUB_API_BASE}")
    print(f"关键词：{args.query!r} · 排序：{args.sort} · 条数：{min(max(1, args.limit), settings.SEARCH_LIMIT_MAX)}")
    print("-" * 62)

    provider = GitHubProvider.from_settings()
    try:
        items = provider.search(args.query, limit=args.limit, sort=args.sort)
    except SearchRateLimitedError as exc:
        print("[SKIP] 上游限速中（未硬刷）：")
        print(f"  {exc}")
        print("  说明：稍后重试即可；如需更高限额，可在 backend/.env 配置 GITHUB_TOKEN（见 docs/search_radar.md）。")
        return 2
    except SearchUpstreamError as exc:
        print("[SKIP] 上游不可用：")
        print(f"  {exc}")
        print("  说明：请检查本机到 api.github.com 的网络连通；")
        print("  国内环境可在 backend/.env 设置 HTTPS_PROXY（或 GITHUB_API_BASE 指向镜像 / 反代）。")
        return 2
    finally:
        provider.close()

    print(f"返回 {len(items)} 条：")
    for idx, item in enumerate(items, start=1):
        language = item.meta.get("language") or "—"
        updated = item.meta.get("updated_at") or "—"
        print(f"  [{idx}] {item.title}")
        print(f"      star={item.score} · 语言={language} · 更新={updated}")
        print(f"      {item.url}")

    # ---- 字段映射与内容断言 ----
    failures: list[str] = []
    if not items:
        failures.append("返回为空（预期至少 1 条）")
    for idx, item in enumerate(items, start=1):
        if not item.url.startswith("https://github.com/"):
            failures.append(f"第 {idx} 条 url 非 github.com 域：{item.url}")
        if not isinstance(item.score, int) or item.score < 0:
            failures.append(f"第 {idx} 条 score 非非负整数：{item.score!r}")
        if item.source != "github":
            failures.append(f"第 {idx} 条 source 异常：{item.source!r}")
        for key in ("language", "updated_at", "owner", "forks_count"):
            if key not in item.meta:
                failures.append(f"第 {idx} 条 meta 缺键：{key}")
        if " " not in item.title and "/" not in item.title:
            failures.append(f"第 {idx} 条 title 非 owner/repo 形态：{item.title!r}")

    print("-" * 62)
    if failures:
        print(f"[FAIL] 断言失败 {len(failures)} 项：")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("[OK] 字段映射与内容断言全部通过（title/url/source/score/meta 一致）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
