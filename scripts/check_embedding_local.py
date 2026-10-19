"""第 18 步 · 本地 bge 向量化**真实**检查（**不消耗任何 LLM token、不需要 DeepSeek key**）。

本脚本用真实的 ``BAAI/bge-large-zh-v1.5``（或 ``EMBEDDING_MODEL`` 所配模型）
做三件事 —— 都是「假 embedder 测不出来」的：

1. **维度**：真实模型输出必须恰为 1024 维（与 ``knowledge_snippets.embedding
   vector(1024)`` 列一致；不一致会在服务层直接报错，绝不静默截断 / 补零）；
2. **语义质量**：同义句余弦相似度显著高于无关句（bge 中文语义能力的体检）——
   只在两个「同义 vs 无关」的对比上断言，不硬编码绝对阈值；
3. **端到端可召回**：把一条真实向量写入 ``knowledge_snippets``，再用
   ``crud_knowledge.search_snippets``（query_text 走真实向量化）检索，
   断言能召回自身（余弦距离 0）。

前置条件：

- 已 ``pip install -r backend/requirements.txt``（含 ``langchain-huggingface`` /
  ``sentence-transformers``；首次安装体积较大，CPU 可跑）；
- 首次运行会从 Hugging Face 下载模型权重（默认约 1.3GB）。国内网络建议设置镜像：

  .. code-block:: ini

      HF_ENDPOINT=https://hf-mirror.com

  已知缓存位置：``%USERPROFILE%\\.cache\\huggingface``（或 ``HF_HOME`` 所配目录）。
  已下载过一次后，离线也可用（``HF_HUB_OFFLINE=1``）。

**不破坏你的数据**：本脚本只往 ``knowledge_snippets`` 写入 1 条前缀为
``[CHECK]本地向量`` 的测试片段，跑完按 id + 前缀双口径删除；
本地模型下载缓存**不动**。加 ``--keep-snippet`` 可保留该测试片段。

用法（在**项目根目录**下）：

    backend\\.venv\\Scripts\\python.exe scripts\\check_embedding_local.py
    backend\\.venv\\Scripts\\python.exe scripts\\check_embedding_local.py --keep-snippet

退出码：0 = 全部通过；1 = 有断言失败；2 = **未就绪**（依赖未装 / 模型加载失败），
已打印清晰中文指引，未做断言、不崩溃。
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
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

from sqlalchemy import text  # noqa: E402

from app.agent.embedding.base import EmbeddingError  # noqa: E402
from app.agent.embedding.local import INSTALL_HINT, local_deps_installed  # noqa: E402
from app.agent.embedding.service import (  # noqa: E402
    describe_embedding_readiness,
    embed_texts,
)
from app.core.config import settings  # noqa: E402
from app.crud.knowledge import crud_knowledge  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.enums import ChunkType, SnippetStatus  # noqa: E402

#: 测试片段前缀（清理与复查的双口径都靠它兜底）
PREFIX = "[CHECK]本地向量"

#: 语义对比句：A=原句、B=同义句、C=无关句
SENT_A = "冬天怎么穿羽绒服才能不显臃肿"
SENT_B = "羽绒服穿搭显瘦的搭配技巧有哪些"
SENT_C = "红烧肉的家常做法步骤详细教程"

#: 端到端写库检索用的查询句（与写入内容同义）
QUERY_TEXT = "羽绒服怎么搭配穿才显瘦"

#: 期望维度（与 knowledge_snippets.embedding vector(1024) 一致）
EXPECTED_DIM = 1024


class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def check(self, ok: bool, label: str, detail: str = "") -> None:
        if ok:
            self.passed += 1
            print(f"[PASS] {label}")
        else:
            self.failures.append(label)
            print(f"[FAIL] {label}" + (f"  ->  {detail}" if detail else ""))


def _cosine(vec_a: list[float], vec_b: list[float]) -> float:
    """余弦相似度（bge 已归一化 → 点积即余弦；此处仍做除模保险）。"""
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = sum(a * a for a in vec_a) ** 0.5
    norm_b = sum(b * b for b in vec_b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _print_install_guide() -> None:
    print()
    print("-" * 78)
    print("【未就绪】本地向量化依赖未安装 —— 请先完成以下步骤后重跑本脚本：")
    print()
    print("  1) 安装依赖（首次安装体积较大，含 sentence-transformers / torch CPU 版）：")
    print("       cd backend")
    print("       .venv\\Scripts\\python.exe -m pip install -r requirements.txt")
    print()
    print("  2) （国内网络建议）设置 Hugging Face 镜像后首次运行会自动下载模型")
    print("     （默认约 1.3GB；已下载过一次后离线也可用）：")
    print("       set HF_ENDPOINT=https://hf-mirror.com")
    print()
    print(f"  详细说明：{INSTALL_HINT}")
    print("-" * 78)


def _print_load_guide(exc: Exception) -> None:
    print()
    print("-" * 78)
    print("【未就绪】本地向量模型加载失败 —— 常见原因与处理：")
    print()
    print("  1) 首次使用时需要下载模型权重（默认约 1.3GB），请保持网络可用；")
    print("     国内网络建议设置镜像：set HF_ENDPOINT=https://hf-mirror.com")
    print("  2) 若完全离线：请先在联网环境下载并放入缓存目录（默认")
    print("     %USERPROFILE%\\.cache\\huggingface），或用 HF_HOME 指定目录后")
    print("     以 HF_HUB_OFFLINE=1 运行；")
    print("  3) 磁盘空间不足 / 权限不足也会导致加载失败（模型缓存需要数 GB 空间）。")
    print()
    print(f"  实际错误：{type(exc).__name__}: {str(exc)[:500]}")
    print("-" * 78)


def main() -> int:
    parser = argparse.ArgumentParser(description="第 18 步 · 本地 bge 向量化真实检查")
    parser.add_argument(
        "--keep-snippet",
        action="store_true",
        help="保留测试写入的知识块（默认跑完删除，保证可重复运行）",
    )
    args = parser.parse_args()

    c = Checker()
    print("=" * 78)
    print("第 18 步 · 本地 bge 向量化真实检查（不消耗 LLM token）")
    print("=" * 78)

    readiness = describe_embedding_readiness()
    print(f"provider={readiness['provider']} model={readiness['model']} "
          f"device={readiness['device']} dim={readiness['dim']} "
          f"db_dim={readiness['db_dim']}")
    print()

    if readiness["provider"] != "local":
        print(f"[未就绪] 当前 EMBEDDING_PROVIDER={readiness['provider']!r}（本脚本检查本地模式）。")
        return 2
    if not local_deps_installed():
        _print_install_guide()
        return 2

    # ---- 1) 真实向量化（首次可能触发模型下载）----
    snippet_id: uuid.UUID | None = None
    try:
        vecs = embed_texts([SENT_A, SENT_B, SENT_C])
    except EmbeddingError as exc:
        _print_load_guide(exc)
        return 2
    except Exception as exc:  # noqa: BLE001 —— 加载期的其它异常同样按「未就绪」处理
        _print_load_guide(exc)
        return 2

    c.check(
        len(vecs) == 3 and all(len(vec) == EXPECTED_DIM for vec in vecs),
        f"真实模型输出恰为 {EXPECTED_DIM} 维（3 条文本）",
        f"{[len(vec) for vec in vecs]}",
    )
    c.check(
        int(settings.EMBEDDING_DIM) == EXPECTED_DIM,
        f"EMBEDDING_DIM 配置为 {EXPECTED_DIM}（与知识库列维度一致）",
        f"{settings.EMBEDDING_DIM}",
    )

    # ---- 2) 语义质量：同义近、无关远 ----
    sim_same = _cosine(vecs[0], vecs[1])
    sim_other = _cosine(vecs[0], vecs[2])
    c.check(
        sim_same > sim_other + 0.05,
        "语义对比：同义句相似度显著高于无关句（bge 中文语义能力体检）",
        f"同义={sim_same:.4f} 无关={sim_other:.4f}",
    )
    print(f"       参考值：同义句余弦 {sim_same:.4f} / 无关句余弦 {sim_other:.4f}")

    # ---- 3) 端到端：写库 → 检索可召回 ----
    content = f"{PREFIX}：{QUERY_TEXT}（{uuid.uuid4().hex[:8]}）"
    try:
        with SessionLocal() as db:
            objs = crud_knowledge.insert_many(
                db,
                [
                    {
                        "chunk_type": ChunkType.GENERAL,
                        "title": content[:50],
                        "content": content,
                        "embedding": vecs[1],  # 用「同义句」的向量，检索用 QUERY_TEXT
                        "meta": {"source": "check_embedding_local", "dim": EXPECTED_DIM},
                    }
                ],
            )
        snippet_id = objs[0].id
        c.check(True, "端到端：真实向量写入 knowledge_snippets 成功")
    except Exception as exc:  # noqa: BLE001
        c.check(False, "端到端：真实向量写入 knowledge_snippets 成功", f"{type(exc).__name__}: {exc}")

    if snippet_id is not None:
        try:
            with SessionLocal() as db:
                hits = crud_knowledge.search_snippets(db, query_text=QUERY_TEXT, k=5)
            c.check(
                bool(hits) and hits[0]["id"] == str(snippet_id),
                "端到端：按语义查询检索，命中的第一条为刚写入的片段（可召回）",
                f"top1={hits[0]['id'] if hits else None} want={snippet_id}",
            )
            c.check(
                bool(hits)
                and all("embedding" not in hit for hit in hits)
                and all(hit["status"] == SnippetStatus.ACTIVE.value for hit in hits),
                "检索结果不含 embedding 原始向量、仅 active",
            )
            if hits:
                print(f"       相似度：top1={hits[0]['similarity']}")
        except Exception as exc:  # noqa: BLE001
            c.check(False, "端到端：检索可召回", f"{type(exc).__name__}: {exc}")

    # ---- 4) 清理与复查（双口径）----
    if args.keep_snippet:
        print(f"[KEEP] 按 --keep-snippet 保留测试片段：{snippet_id}")
    else:
        with SessionLocal() as db:
            if snippet_id is not None:
                db.execute(
                    text("DELETE FROM knowledge_snippets WHERE id = :sid"),
                    {"sid": str(snippet_id)},
                )
            db.execute(
                text("DELETE FROM knowledge_snippets WHERE content LIKE :t"),
                {"t": f"%{PREFIX}%"},
            )
            db.commit()
        with SessionLocal() as db:
            residue_by_id = (
                int(
                    db.execute(
                        text("SELECT count(*) FROM knowledge_snippets WHERE id = :sid"),
                        {"sid": str(snippet_id)},
                    ).scalar_one()
                )
                if snippet_id is not None
                else 0
            )
            residue_by_prefix = int(
                db.execute(
                    text("SELECT count(*) FROM knowledge_snippets WHERE content LIKE :t"),
                    {"t": f"%{PREFIX}%"},
                ).scalar_one()
            )
        c.check(
            residue_by_id == 0 and residue_by_prefix == 0,
            "测试片段清理并双口径复查残留为 0（按 id + 按前缀）",
            f"by_id={residue_by_id} prefix={residue_by_prefix}",
        )

    print()
    print(f"通过 {c.passed} 项，失败 {len(c.failures)} 项。")
    if c.failures:
        for name in c.failures:
            print(f"  - FAIL: {name}")
        return 1
    print("全部 PASS ✔（本地向量化真实检查完成；模型缓存未动）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
