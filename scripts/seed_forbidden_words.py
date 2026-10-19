"""第 19 步 · 违禁词词表种子脚本：把内置词表**幂等**写入知识库。

做的事情（只有一件）：

- 读取 ``app/agent/content/forbidden_words.py`` 的内置词表（约 73 词 / 7 类别）；
- 按 ``chunk_type=forbidden_word``、``content=词``、
  ``meta={"category": 类别, "content_hash": normalize_text(词) 的 sha256}``
  写入 ``knowledge_snippets``（``status=active``）；
- 违禁词用于**精确匹配**、`embedding` 允许为空 —— **不加载 / 不下载向量模型**；
- 幂等：与既有 active 违禁词的 ``content_hash`` 相同则跳过（计 reused），
  重复运行不重复插入；同一批内的归一化重复词也只写一次。

前置条件：

- 本地 docker PostgreSQL 已启动（与其余脚本相同；不联网、不消耗任何 token）。

用法（在**项目根目录**下）：

    backend\\.venv\\Scripts\\python.exe scripts\\seed_forbidden_words.py
    backend\\.venv\\Scripts\\python.exe scripts\\seed_forbidden_words.py --dry-run

``--dry-run``：仅打印将写入 / 复用条数，**不写库**（先预检再执行）。

退出码：0 = 成功（含「0 新增」的幂等重复运行）；1 = 失败（数据库不可达等）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# ---- 入口先摘掉沙箱注入的代理变量（本机全部真实请求进程的共同约定）----
for _key in (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
):
    os.environ.pop(_key, None)

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.db.session import SessionLocal  # noqa: E402
from app.services.forbidden_source import seed_builtin_words  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把内置违禁词表幂等写入知识库（knowledge_snippets；embedding 留空）"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="仅打印将写入 / 复用条数，不写库"
    )
    args = parser.parse_args()

    print("=" * 72)
    print("第 19 步 · 违禁词词表种子（幂等；不加载向量模型、不消耗 token）")
    print("=" * 72)

    try:
        with SessionLocal() as db:
            result = seed_builtin_words(db, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001 —— 给清晰指引，不抛裸堆栈
        print(f"[FAIL] 执行失败：{type(exc).__name__}: {exc}")
        print(
            "提示：请确认 docker 容器（xhs-db）已启动、backend/.env 的 DATABASE_URL 可用。"
        )
        return 1

    mode = "DRY-RUN（未写库）" if result["dry_run"] else "已写库"
    print(f"内置词表总数  : {result['total']}")
    print(f"将写入        : {result['would_insert']}")
    print(f"复用（已存在）: {result['reused']}")
    print(f"实际写入      : {result['inserted']}")
    print(f"模式          : {mode}")
    if not args.dry_run and result["inserted"] == 0 and result["would_insert"] == 0:
        print("说明：本次没有新写入（词表此前已同步，按 content_hash 幂等跳过）。")
    print("完成 ✔（可在前端「知识库 → 知识块 → 违禁词」查看）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
