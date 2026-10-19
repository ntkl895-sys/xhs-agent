"""第 7 步 · 数据层离线自检（**不需要数据库连接**）。

它只读源码与模型元数据，逐项断言后输出 PASS / FAIL，用于 CI 或本地快速校验：

1. 模型表数量 = 10，且表名集合与约定一致；
2. ``knowledge_snippets.embedding`` 为 ``Vector(1024)``；
3. 迁移目录下**至少存在一个**迁移文件（``0001_baseline.py`` 必须存在）；
4. **全部迁移合并后与 SQLAlchemy metadata 一致**（表名 / 列名 / 索引名 / 向量维度 / 唯一约束名）；
   —— 支持多迁移：``create_table`` 与 ``add_column`` 会**按表合并列集合**，
   以便后续步骤新增迁移（如 ``0002_*``）时本自检仍然成立；
5. 全仓**不存在** ``create_all`` 建表调用（建表只允许走 Alembic）；
6. ``alembic.ini`` 的 ``sqlalchemy.url`` 留空，且 ``env.py`` 从 ``settings.DATABASE_URL`` 注入。

用法（在**项目根目录**下）：

    backend\\.venv\\Scripts\\python.exe scripts\\selftest_db.py

全部通过退出码 0；任一项 FAIL 退出码 1。
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from pgvector.sqlalchemy import Vector as PGVector  # noqa: E402

from app.db.base import Base  # noqa: E402
import app.models  # noqa: E402,F401  —— 注册全部模型到 metadata
from app.models import EMBEDDING_DIM  # noqa: E402

MIGRATION_FILE = BACKEND_DIR / "migrations" / "versions" / "0001_baseline.py"
VERSIONS_DIR = BACKEND_DIR / "migrations" / "versions"
SELFTEST_NAME = Path(__file__).name


def discover_migrations() -> list[Path]:
    """按文件名顺序发现 ``versions/`` 下的全部迁移脚本（跳过 ``__init__.py`` 等）。"""
    if not VERSIONS_DIR.is_dir():
        return []
    return sorted(
        path
        for path in VERSIONS_DIR.glob("*.py")
        if not path.name.startswith("__")
    )

EXPECTED_TABLES = {
    "accounts",
    "personas",
    "hotspots",
    "topics",
    "notes",
    "covers",
    "note_metrics",
    "diagnostics",
    "knowledge_snippets",
    "agent_runs",
}

EXPECTED_UNIQUE_NAMES = {"uq_accounts_xhs_red_id", "uq_hotspot_hash", "uq_metric_snapshot"}

#: 框架托管表（LangGraph 检查点）——**不是业务表**，不得出现在 SQLAlchemy metadata 中。
#: 它们由 langgraph-checkpoint-postgres 的 PostgresSaver.setup() 幂等创建/升级，
#: 且被隔离在独立 schema（默认 langgraph），永远不会写进业务迁移。
FRAMEWORK_TABLES = frozenset(
    {
        "checkpoint_migrations",
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
    }
)


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


# ---------------------------------------------------------------------------
# AST 工具
# ---------------------------------------------------------------------------
def _call_name(func: ast.AST) -> str | None:
    """取调用名：``op.create_table`` -> ``create_table``，``sa.Column`` -> ``Column``。"""
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _const_str(node: ast.AST) -> str | None:
    """从 ``"xxx"`` 或 ``op.f("xxx")`` 中取出字符串常量。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Call) and _call_name(node.func) == "f" and node.args:
        return _const_str(node.args[0])
    return None


def parse_migration(path: Path) -> dict:
    """解析单个迁移脚本，抽出建表 / 加列 / 建索引 / 唯一约束 / 向量维度。"""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)

    tables: dict[str, set[str]] = {}
    indexes: set[str] = set()
    unique_names: set[str] = set()
    vector_dims: list[int] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _call_name(node.func)

            if name == "create_table" and node.args:
                tname = _const_str(node.args[0])
                cols: set[str] = set()
                if tname:
                    for arg in node.args[1:]:
                        if isinstance(arg, ast.Call) and _call_name(arg.func) == "Column" and arg.args:
                            cname = _const_str(arg.args[0])
                            if cname:
                                cols.add(cname)
                    tables.setdefault(tname, set()).update(cols)

            elif name == "add_column" and len(node.args) >= 2:
                # op.add_column("topics", sa.Column("content_outline", sa.Text()))
                tname = _const_str(node.args[0])
                col_arg = node.args[1]
                cname = None
                if isinstance(col_arg, ast.Call) and _call_name(col_arg.func) == "Column" and col_arg.args:
                    cname = _const_str(col_arg.args[0])
                if tname and cname:
                    tables.setdefault(tname, set()).add(cname)

            elif name == "create_index" and node.args:
                iname = _const_str(node.args[0])
                if iname:
                    indexes.add(iname)

            elif name == "UniqueConstraint":
                for kw in node.keywords:
                    if kw.arg == "name":
                        uname = _const_str(kw.value)
                        if uname:
                            unique_names.add(uname)

            elif name == "Vector":
                for kw in node.keywords:
                    if kw.arg == "dim" and isinstance(kw.value, ast.Constant):
                        vector_dims.append(int(kw.value.value))

    return {
        "tables": tables,
        "indexes": indexes,
        "unique_names": unique_names,
        "vector_dims": vector_dims,
    }


def merge_migrations(parsed: list[dict]) -> dict:
    """把多个迁移的结果**合并**：同一张表的列集合取并集。"""
    tables: dict[str, set[str]] = {}
    indexes: set[str] = set()
    unique_names: set[str] = set()
    vector_dims: list[int] = []
    for item in parsed:
        for tname, cols in item["tables"].items():
            tables.setdefault(tname, set()).update(cols)
        indexes |= item["indexes"]
        unique_names |= item["unique_names"]
        vector_dims.extend(item["vector_dims"])
    return {
        "tables": tables,
        "indexes": indexes,
        "unique_names": unique_names,
        "vector_dims": vector_dims,
    }


def scan_create_all() -> list[str]:
    """扫描全仓 .py 的 **AST**，找 ``create_all(...)`` **真实调用**。

    用 AST 而非正则：注释与 docstring 里提到 ``create_all`` 不算违反约定。
    排除本自检脚本自身。
    """
    hits: list[str] = []
    skip_dirs = {".venv", "node_modules", ".next", ".git", "__pycache__", ".turbo", "out", "dist", "build"}
    for base, dirs, files in os.walk(ROOT_DIR):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            if not fn.endswith(".py") or fn == SELFTEST_NAME:
                continue
            fp = Path(base) / fn
            try:
                tree = ast.parse(fp.read_text(encoding="utf-8", errors="ignore"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and _call_name(node.func) == "create_all":
                    rel = fp.relative_to(ROOT_DIR)
                    hits.append(f"{rel}:{node.lineno}")
    return hits


def main() -> int:
    c = Checker()
    metadata = Base.metadata
    meta_tables = set(metadata.tables.keys())

    # 1) 模型表数量与表名
    c.check(
        len(meta_tables) == 10 and meta_tables == EXPECTED_TABLES,
        "模型 metadata 恰好 10 张表且表名齐全",
        f"实际={sorted(meta_tables)}",
    )

    # 1b) 框架托管表不得混进业务 metadata（隔离的离线证据）
    leaked = sorted(meta_tables & FRAMEWORK_TABLES)
    c.check(
        not leaked,
        "metadata 中不存在框架检查点表（业务 10 表与框架表隔离）",
        f"混入={leaked}",
    )

    # 2) embedding 为 Vector(1024)
    emb = metadata.tables["knowledge_snippets"].c.embedding
    c.check(
        isinstance(emb.type, PGVector) and getattr(emb.type, "dim", None) == EMBEDDING_DIM,
        f"knowledge_snippets.embedding 为 Vector({EMBEDDING_DIM})",
        f"实际={type(emb.type).__name__}(dim={getattr(emb.type, 'dim', None)})",
    )

    # 3) 迁移文件发现（支持多迁移合并）
    migrations = discover_migrations()
    c.check(
        MIGRATION_FILE.is_file(),
        f"基线迁移文件存在：{MIGRATION_FILE.relative_to(ROOT_DIR)}",
    )
    c.check(
        bool(migrations),
        f"versions/ 下发现迁移文件（{len(migrations)} 个）",
        f"{[p.name for p in migrations]}",
    )
    if not migrations:
        print(f"\n通过 {c.passed} 项，失败 {len(c.failures)} 项。")
        for f in c.failures:
            print(f"  - FAIL: {f}")
        return 1

    print(f"[INFO] 合并 {len(migrations)} 个迁移：{', '.join(p.name for p in migrations)}")
    mig = merge_migrations([parse_migration(path) for path in migrations])

    # 4) 迁移与 metadata 一致性
    c.check(
        set(mig["tables"].keys()) == meta_tables,
        "合并后的迁移建表集合 == metadata 表集合",
        f"迁移={sorted(mig['tables'])}",
    )

    # 4b) 业务迁移中【不得】出现框架检查点表（框架表只由 setup() 管）
    mig_framework = sorted(set(mig["tables"]) & FRAMEWORK_TABLES)
    c.check(
        not mig_framework,
        "业务 Alembic 迁移不包含框架检查点表",
        f"误入={mig_framework}",
    )

    col_mismatch = {
        t: {"mig": sorted(mig["tables"].get(t, set())), "meta": sorted(metadata.tables[t].columns.keys())}
        for t in meta_tables
        if t in mig["tables"] and mig["tables"][t] != set(metadata.tables[t].columns.keys())
    }
    c.check(not col_mismatch, "每张表的列名集合 == metadata 列集合", f"{col_mismatch}")

    meta_indexes = {ix.name for t in metadata.tables.values() for ix in t.indexes}
    c.check(
        mig["indexes"] == meta_indexes,
        f"迁移索引集合 == metadata 索引集合（{len(meta_indexes)} 个）",
        f"缺失={sorted(meta_indexes - mig['indexes'])} 多余={sorted(mig['indexes'] - meta_indexes)}",
    )

    meta_unique = {
        con.name
        for t in metadata.tables.values()
        for con in t.constraints
        if con.__class__.__name__ == "UniqueConstraint" and con.name
    }
    c.check(
        mig["unique_names"] == meta_unique == EXPECTED_UNIQUE_NAMES,
        f"唯一约束名一致（{len(EXPECTED_UNIQUE_NAMES)} 个）",
        f"迁移={sorted(mig['unique_names'])} metadata={sorted(meta_unique)}",
    )

    c.check(
        mig["vector_dims"] and all(d == EMBEDDING_DIM for d in mig["vector_dims"]),
        f"迁移中向量列维度为 {EMBEDDING_DIM}",
        f"实际={mig['vector_dims']}",
    )

    # 5) 全仓不存在 create_all
    hits = scan_create_all()
    c.check(not hits, "全仓不存在 create_all 建表调用（建表只走 Alembic）", f"{hits}")

    # 6) alembic.ini url 留空 + env.py 注入
    ini_path = BACKEND_DIR / "alembic.ini"
    ini_url_empty = False
    if ini_path.is_file():
        for line in ini_path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("sqlalchemy.url"):
                ini_url_empty = line.split("=", 1)[1].strip() == ""
                break
    c.check(ini_url_empty, "alembic.ini 的 sqlalchemy.url 留空")

    env_py = BACKEND_DIR / "migrations" / "env.py"
    env_src = env_py.read_text(encoding="utf-8") if env_py.is_file() else ""
    c.check(
        "settings.DATABASE_URL" in env_src and "set_main_option" in env_src,
        "env.py 从 settings.DATABASE_URL 注入连接串",
    )
    c.check(
        "import app.models" in env_src,
        "env.py 导入 app.models（保证 target_metadata 发现全部表）",
    )

    print()
    print(f"通过 {c.passed} 项，失败 {len(c.failures)} 项。")
    if c.failures:
        for f in c.failures:
            print(f"  - FAIL: {f}")
        return 1
    print("全部 PASS ✔（离线自检，未连接数据库）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
