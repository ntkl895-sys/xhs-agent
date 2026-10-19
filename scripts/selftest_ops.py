"""第 20 步 · 健壮性与本地体验 离线自检（一键启停 / 常驻自愈 / 备份恢复 / 收口缺陷）。

覆盖（对任务书第五节逐条对应）：

0. 静态检查：全部 .ps1 可被 PowerShell 解析器解析（零语法错误）；ops 脚本与
   根目录 .bat 不含密钥前缀 / 卷删除字样 / 外部托管软件名 / 平台域名；
   .bat 为纯 ASCII（双击入口不乱码）；共享库函数齐全；start.ps1 关键开关齐全；
1. 环境自检零副作用：伪根目录（缺 .env / 缺 venv / 缺 node_modules / 非工程目录）
   各给明确指引且**不产生任何文件**；
2. 函数级受控分支：前端构建判定（缺 .next / 过期 / 最新 三种）、
   HTTP 探活「不可达 → 优雅返回 false」（不抛异常）；
3. 启停往返（-Dev 受控启动）：start → 健康检查全绿（含 dailyhot 正常路径）→
   重复 start 幂等（不重复拉起、PID 不变）→ stop → 端口回收 + PID 文件清理；
   -SkipDailyhot 容错分支（只 WARNING 不阻断）；status 输出无绝对路径 / 无密钥；
4. 备份 → 恢复往返：真实备份（pg_dump -Fc + pg_restore -l 校验 + 保留策略含清理）；
   恢复到临时验证库并与源库比对关键表行数；-DropVerifyDb 删除验证库；
   文件不存在负例；.env 不入备份；
5. 计划任务注册 / 卸载往返（自启 + 定时备份）：注册后回读可查（含延迟）、
   卸载后回读查无、任务文件夹清理；
6. -Watch 常驻：受控启动 → 杀掉后端进程 → 自动重启（计数 + 日志）→
   stop 联动结束监控（进程退出、状态文件清理）；
7. 收口修复验证：covers 组合 PATCH 单事务生效（字段 + 互斥选中）、
   knowledge 组合 PATCH（status + 字段）单事务生效、重向量化失败降级语义保持、
   apply_admin_update 白名单负断言（status 不走 values 静默通道）、
   选题显式 hotspot_ids 去重（保序、去重先于截断、missing 不重复）；
8. 零迁移断言：versions 恰 2 支、alembic current=head、alembic check 零漂移、
   public 恰 11 张（10 业务 + alembic_version）、langgraph 框架 4 张；
9. 收尾：测试数据双口径清理（按 id + 前缀兜底）、临时库 / 临时目录 / 计划任务 /
   端口全部复核为 0。

运行方式（在**项目根目录**下）：

    backend\\.venv\\Scripts\\python.exe scripts\\selftest_ops.py

依赖本地 docker（db/redis 容器）；会短暂启动/停止前后端（-Dev 模式、不点浏览器）、
注册/卸载同名计划任务后复原。**不联网、不消耗 token**。退出码：0 = 全绿；1 = 有失败。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
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
OPS_DIR = ROOT_DIR / "scripts" / "ops"
VERSIONS_DIR = BACKEND_DIR / "migrations" / "versions"
PY_EXE = BACKEND_DIR / ".venv" / "Scripts" / "python.exe"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import text  # noqa: E402

import app.agent.embedding.service as embedding_service  # noqa: E402
import app.agent.topics.service as topics_service  # noqa: E402
import app.services.knowledge_admin as knowledge_admin  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.agent.embedding.base import EmbeddingNotAvailableError  # noqa: E402
from app.agent.embedding.service import reset_embedder  # noqa: E402
from app.crud.knowledge import crud_knowledge  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models.account import Account  # noqa: E402
from app.models.enums import (  # noqa: E402
    AccountStatus,
    ComplianceStatus,
    HotspotStatus,
    NoteStatus,
    PersonaStatus,
    SnippetStatus,
)
from app.models.hotspot import Hotspot  # noqa: E402
from app.models.note import Note  # noqa: E402
from app.models.persona import Persona  # noqa: E402

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
PREFIX = "[SELFTEST]运维"

#: ops 脚本与双击入口（静态扫描对象）
OPS_SCRIPTS = (
    "ops-common.ps1",
    "start.ps1",
    "stop.ps1",
    "status.ps1",
    "backup.ps1",
    "restore.ps1",
    "install-autostart.ps1",
    "uninstall-autostart.ps1",
    "install-backup-task.ps1",
    "uninstall-backup-task.ps1",
)
BAT_FILES = ("start.bat", "stop.bat")

#: 禁止字样（平台域名 / 登录态 / 密钥前缀 / 外部托管软件）
#: 注：``sk-`` 单独按「真实密钥形态」扫描（sk- + ≥16 位键值串）——
#: Get-OpsSafeText 的脱敏正则**必须**包含 sk- 字样，不能用裸词判泄漏。
FORBIDDEN_TOKENS = (
    "xiaohongshu",
    "xhslink",
    "xhs.com",
    "redbook.com",
    "Cookie",
    "access_token",
    "secret_key",
    "pm2",
    "nssm",
    "nginx",
    "supervisor",
)

#: 真实密钥形态（DeepSeek 等：sk- 后接长键值串）
_SECRET_SHAPED_RE = r"sk-[A-Za-z0-9_\-]{16,}"

#: 通用「绝对路径回显」检查正则（面向脚本输出文本的运行时断言见各组）
_WIN_ABS_RE = r"[A-Za-z]:\\"

#: 共享库必须具备的函数
REQUIRED_COMMON_FUNCTIONS = (
    "Resolve-OpsRoot",
    "Invoke-OpsCapture",
    "Clear-OpsProxyForChild",
    "Wait-OpsContainerHealthy",
    "Wait-OpsHttpHealthy",
    "Stop-OpsProcessTree",
    "Start-OpsService",
    "Register-OpsScheduledTask",
    "Unregister-OpsScheduledTask",
    "Test-OpsFrontendBuildNeeded",
    "Test-OpsPortOwnedByProject",
    "Get-OpsSafeText",
)

#: start.ps1 必须具备的开关
REQUIRED_START_SWITCHES = ("-Dev", "-Watch", "-AutoDocker", "-Migrate", "-NoBrowser", "-SkipDailyhot", "-Root")

#: 业务表名单（public 中除 alembic_version 外的 10 张）
BUSINESS_TABLES = {
    "accounts",
    "agent_runs",
    "covers",
    "diagnostics",
    "hotspots",
    "knowledge_snippets",
    "note_metrics",
    "notes",
    "personas",
    "topics",
}

#: 恢复行数比对用关键表（agent_runs 会持续写入，不参与比对）
KEY_TABLES = (
    "accounts",
    "personas",
    "notes",
    "topics",
    "note_metrics",
    "diagnostics",
    "covers",
    "hotspots",
    "knowledge_snippets",
)

TMP_BASE = Path(tempfile.mkdtemp(prefix="xhs_ops_selftest_"))


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0
        self.skipped: list[str] = []

    def check(self, ok: bool, label: str, detail: str = "") -> None:
        if ok:
            self.passed += 1
            print(f"[PASS] {label}")
        else:
            self.failures.append(label)
            print(f"[FAIL] {label}" + (f"  ->  {detail}" if detail else ""))

    def skip(self, label: str) -> None:
        self.skipped.append(label)
        print(f"[SKIP] {label}")


def run_ps_file(script: Path, args: list[str], tag: str, timeout: int = 420) -> tuple[int, str]:
    """运行 .ps1（stdout+stderr 合并到文件 —— 规避管道句柄继承导致的读取挂起）。"""
    out_path = TMP_BASE / f"{tag}.txt"
    with open(out_path, "w", encoding="utf-8") as fh:
        r = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
            ]
            + args,
            stdout=fh,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            cwd=str(ROOT_DIR),
            stdin=subprocess.DEVNULL,
        )
    return r.returncode, out_path.read_text(encoding="utf-8", errors="replace")


def run_ps_cmd(code: str, tag: str, timeout: int = 180) -> tuple[int, str]:
    out_path = TMP_BASE / f"{tag}.txt"
    with open(out_path, "w", encoding="utf-8") as fh:
        r = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                code,
            ],
            stdout=fh,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            cwd=str(ROOT_DIR),
            stdin=subprocess.DEVNULL,
        )
    return r.returncode, out_path.read_text(encoding="utf-8", errors="replace")


def run_python(args: list[str], tag: str, timeout: int = 240) -> tuple[int, str]:
    out_path = TMP_BASE / f"{tag}.txt"
    with open(out_path, "w", encoding="utf-8") as fh:
        r = subprocess.run(
            [str(PY_EXE)] + args,
            stdout=fh,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            cwd=str(ROOT_DIR),
            stdin=subprocess.DEVNULL,
        )
    return r.returncode, out_path.read_text(encoding="utf-8", errors="replace")


def port_listening(port: int) -> bool:
    rc, out = run_ps_cmd(
        "$c = Get-NetTCPConnection -LocalPort " + str(port) + " -State Listen -ErrorAction SilentlyContinue; "
        "Write-Output ('N=' + @($c).Count)",
        f"port{port}",
        timeout=60,
    )
    return "N=0" not in out


def psql_scalar(database: str, sql: str) -> str | None:
    r = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "postgres",
            "-d",
            database,
            "-tAc",
            sql,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        cwd=str(ROOT_DIR),
    )
    if r.returncode != 0:
        return None
    lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    return lines[0] if lines else None


def db_exists(name: str) -> bool:
    return psql_scalar("postgres", f"SELECT count(*) FROM pg_database WHERE datname = '{name}'") == "1"


def temp_dbs(pattern_prefix: str) -> list[str]:
    r = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-tAc",
            f"SELECT datname FROM pg_database WHERE datname LIKE '{pattern_prefix}%'",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        cwd=str(ROOT_DIR),
    )
    return [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]


def drop_db(name: str) -> bool:
    r = subprocess.run(
        ["docker", "compose", "exec", "-T", "db", "psql", "-U", "postgres", "-c", f'DROP DATABASE "{name}";'],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        cwd=str(ROOT_DIR),
    )
    return r.returncode == 0


def read_run_json(name: str):
    path = ROOT_DIR / "logs" / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return None


def snapshot(path: Path) -> list[str]:
    items: list[str] = []
    for dirpath, dirnames, filenames in os.walk(path):
        for d in dirnames:
            items.append(str(Path(dirpath) / d))
        for f in filenames:
            items.append(str(Path(dirpath) / f))
    return sorted(items)


def health_green() -> bool:
    r = subprocess.run(
        [str(PY_EXE), str(ROOT_DIR / "scripts" / "healthcheck.py")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        cwd=str(ROOT_DIR),
    )
    return r.returncode == 0


# ---------------------------------------------------------------------------
# 假 embedder（确定性；不发请求、不加载模型）
# ---------------------------------------------------------------------------
class FakeEmbedder:
    dim = 1024

    def __init__(self) -> None:
        self.calls: list[int] = []

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        import hashlib
        import random

        self.calls.append(len(texts))
        vectors = []
        for text in texts:
            seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
            rng = random.Random(seed)
            vectors.append([rng.uniform(-1.0, 1.0) for _ in range(self.dim)])
        return vectors


class ThrowingEmbedder:
    dim = 1024

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingNotAvailableError("本地向量化依赖未安装（自检注入）。")


class _EmbedEnv:
    """模拟「本地依赖已装」：闸门放行 + 注入假 embedder（退出时恢复原样）。"""

    def __init__(self, embedder=None) -> None:
        self.embedder = embedder

    def __enter__(self) -> "_EmbedEnv":
        self._orig_blocker = knowledge_admin.describe_embedding_blocker
        self._orig_get = embedding_service.get_embedder
        knowledge_admin.describe_embedding_blocker = lambda: None
        if self.embedder is not None:
            embedding_service.get_embedder = lambda: self.embedder
        reset_embedder()
        return self

    def __exit__(self, *exc) -> None:
        knowledge_admin.describe_embedding_blocker = self._orig_blocker
        embedding_service.get_embedder = self._orig_get
        reset_embedder()


def psql_lines(database: str, sql: str) -> list[str]:
    r = subprocess.run(
        ["docker", "compose", "exec", "-T", "db", "psql", "-U", "postgres", "-d", database, "-tAc", sql],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        cwd=str(ROOT_DIR),
    )
    return [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# 组 1：静态检查（解析 / 合规字样 / 纯 ASCII / 函数与开关齐全）
# ---------------------------------------------------------------------------
def group1_static(c: Checker) -> None:
    print("\n---- 组 1：静态检查 ----")

    for name in OPS_SCRIPTS:
        c.check((OPS_DIR / name).exists(), f"ops 脚本存在：{name}")
    for name in BAT_FILES:
        c.check((ROOT_DIR / name).exists(), f"双击入口存在：{name}")

    for name in BAT_FILES:
        data = (ROOT_DIR / name).read_bytes()
        c.check(all(b < 0x80 for b in data), f"{name} 为纯 ASCII（双击不乱码）")

    # 全量 .ps1 解析（含 scripts 下原有的 install-docker / repair-wsl）
    parse_files = sorted(str(p) for p in (ROOT_DIR / "scripts").rglob("*.ps1"))
    parse_result = TMP_BASE / "parse_result.json"
    lines = ["$ErrorActionPreference = 'Stop'", "$files = @("]
    for i, f in enumerate(parse_files):
        comma = "," if i < len(parse_files) - 1 else ""
        lines.append("    '" + f.replace("'", "''") + "'" + comma)
    lines += [
        ")",
        "$results = @()",
        "foreach ($f in $files) {",
        "    $tokens = $null; $errors = $null",
        "    $null = [System.Management.Automation.Language.Parser]::ParseFile($f, [ref]$tokens, [ref]$errors)",
        "    $results += [pscustomobject]@{ file = $f; errorCount = @($errors).Count }",
        "}",
        "($results | ConvertTo-Json -Depth 4) | Set-Content -Path '"
        + str(parse_result).replace("'", "''")
        + "' -Encoding UTF8",
        "Write-Output 'PARSE_DONE'",
    ]
    parse_ps = TMP_BASE / "parse_check.ps1"
    parse_ps.write_text("\n".join(lines), encoding="utf-8-sig")
    rc, out = run_ps_file(parse_ps, [], "g1_parse", timeout=180)
    bad_files: list[str] = []
    if rc == 0 and parse_result.exists():
        data = json.loads(parse_result.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            data = [data]
        bad_files = [Path(d["file"]).name for d in data if d["errorCount"] > 0]
    c.check(
        rc == 0 and parse_result.exists() and not bad_files and len(parse_files) >= 12,
        f"{len(parse_files)} 个 .ps1 全部可被解析（零语法错误）",
        f"rc={rc} bad={bad_files}",
    )

    # 合规字样扫描（ops 脚本 + bat；下卷删除按「down 紧跟 -v/--volumes」精确匹配）
    scan_targets = [OPS_DIR / name for name in OPS_SCRIPTS] + [ROOT_DIR / name for name in BAT_FILES]
    for path in scan_targets:
        text = path.read_text(encoding="utf-8", errors="replace")
        low = text.lower()
        token_hits = [t for t in FORBIDDEN_TOKENS if t.lower() in low]
        key_shaped = re.search(_SECRET_SHAPED_RE, text) is not None
        downv = re.search(r"down\s+(-v|--volumes)\b", text, re.IGNORECASE) is not None
        env_echo = re.search(r"(Get-Content|cat)\s[^\r\n]*\.env", text) is not None
        c.check(
            not token_hits and not key_shaped and not downv and not env_echo,
            f"{path.name} 合规扫描（无真实密钥串 / 卷删除 / 外部托管软件 / 平台域名 / .env 回显）",
            f"tokens={token_hits} keyShaped={key_shaped} downv={downv} envEcho={env_echo}",
        )

    common = (OPS_DIR / "ops-common.ps1").read_text(encoding="utf-8", errors="replace")
    missing_fn = [fn for fn in REQUIRED_COMMON_FUNCTIONS if f"function {fn}" not in common]
    c.check(not missing_fn, "共享库函数齐全", f"missing={missing_fn}")
    c.check("OpsTaskFolderName" in common, "共享库常量完整（任务文件夹名）")

    start_text = (OPS_DIR / "start.ps1").read_text(encoding="utf-8", errors="replace")
    missing_sw = [sw for sw in REQUIRED_START_SWITCHES if sw not in start_text]
    c.check(not missing_sw, "start.ps1 关键开关齐全", f"missing={missing_sw}")
    c.check("stop.request" in start_text, "start.ps1 含 -Watch 停止标记联动")


# ---------------------------------------------------------------------------
# 组 2：环境自检零副作用（伪根目录）
# ---------------------------------------------------------------------------
def _setup_fake_root(base: Path, level: str) -> Path:
    root = base / level
    if root.exists():
        shutil.rmtree(root)
    (root / "backend").mkdir(parents=True)
    (root / "frontend").mkdir()
    (root / "docker-compose.yml").write_text("services: {}\n", encoding="ascii")
    if level in ("no-venv", "no-node_modules"):
        (root / "backend" / ".env").write_text("PLACEHOLDER=1\n", encoding="ascii")
    if level == "no-node_modules":
        exe = root / "backend" / ".venv" / "Scripts" / "python.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
    return root


def group2_precheck(c: Checker) -> None:
    print("\n---- 组 2：环境自检零副作用（伪根目录） ----")
    base = TMP_BASE / "fake_roots"
    cases = (
        ("no-env", "env.example", 2),
        ("no-venv", "虚拟环境", 2),
        ("no-node_modules", "npm install", 2),
    )
    for level, fragment, expect_rc in cases:
        root = _setup_fake_root(base, level)
        before = snapshot(root)
        rc, out = run_ps_file(OPS_DIR / "start.ps1", ["-Root", str(root), "-NoBrowser"], f"g2_{level}", timeout=120)
        after = snapshot(root)
        c.check(rc == expect_rc, f"伪根目录（{level}）→ 退出码 {expect_rc}", f"rc={rc}")
        c.check(fragment in out, f"伪根目录（{level}）→ 明确指引（含「{fragment}」）", out[-260:])
        c.check(before == after, f"伪根目录（{level}）→ 零副作用（目录无变化）")

    bad_root = base / "not-a-project"
    bad_root.mkdir(parents=True, exist_ok=True)
    rc, out = run_ps_file(OPS_DIR / "start.ps1", ["-Root", str(bad_root), "-NoBrowser"], "g2_not_project", timeout=120)
    c.check(rc == 1 and "项目根" in out, "非工程目录 → 退出码 1 且提示「不像项目根」", f"rc={rc} {out[-160:]}")


# ---------------------------------------------------------------------------
# 组 3：函数级受控分支（构建判定 / 探活不可达）
# ---------------------------------------------------------------------------
def group3_functions(c: Checker) -> None:
    print("\n---- 组 3：函数级受控分支 ----")
    fb = TMP_BASE / "fakebuild"
    common = OPS_DIR / "ops-common.ps1"

    def build_check(name: str, make_next: bool, make_src_newer: bool, tag: str) -> str:
        root = fb / name
        if root.exists():
            shutil.rmtree(root)
        (root / "frontend" / "src").mkdir(parents=True)
        (root / "frontend" / "src" / "page.tsx").write_text("x", encoding="ascii")
        if make_next:
            (root / "frontend" / ".next").mkdir(parents=True)
            (root / "frontend" / ".next" / "BUILD_ID").write_text("x", encoding="ascii")
            bid = root / "frontend" / ".next" / "BUILD_ID"
            page = root / "frontend" / "src" / "page.tsx"
            os.utime(bid, (time.time() + 600, time.time() + 600))
            if make_src_newer:
                os.utime(page, (time.time() + 1200, time.time() + 1200))
            else:
                os.utime(page, (time.time() - 600, time.time() - 600))
        code = (
            f". '{common}'; $r = Test-OpsFrontendBuildNeeded -Root '{root}'; "
            "Write-Output ('R=' + $r.needed + '|' + $r.reason)"
        )
        _, out = run_ps_cmd(code, tag, timeout=90)
        return out.strip()

    r = build_check(name="no_next", make_next=False, make_src_newer=False, tag="g3_build_a")
    c.check("R=True" in r and "未找到" in r, "构建判定：缺 .next → 需要构建", r)
    r = build_check(name="next_newer", make_next=True, make_src_newer=False, tag="g3_build_b")
    c.check("R=False" in r, "构建判定：产物最新 → 跳过构建", r)
    r = build_check(name="src_newer", make_next=True, make_src_newer=True, tag="g3_build_c")
    c.check("R=True" in r and "更新" in r, "构建判定：源码更新 → 需要构建", r)

    code = (
        f". '{common}'; $t0 = Get-Date; "
        "$h = Wait-OpsHttpHealthy -Url 'http://127.0.0.1:59321/' -TimeoutSec 4 -IntervalSec 1 -HttpTimeoutMs 1000; "
        "$dt = ((Get-Date) - $t0).TotalSeconds; "
        "Write-Output ('H=' + $h); Write-Output ('DT=' + [math]::Round($dt, 1))"
    )
    rc, out = run_ps_cmd(code, "g3_http", timeout=90)
    c.check(rc == 0 and "H=False" in out, "HTTP 探活不可达 → 优雅返回 False（不抛异常）", out[:120])
    try:
        dt = float(re.search(r"DT=([0-9.]+)", out).group(1))  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        dt = 999.0
    c.check(dt < 20, "HTTP 探活超时受控（≤20 秒内返回）", f"dt={dt}")


# ---------------------------------------------------------------------------
# 组 4：启停往返（-Dev 受控启动 / 幂等 / 端口与状态文件回收）
# ---------------------------------------------------------------------------
def group4_startstop(c: Checker) -> None:
    print("\n---- 组 4：启停往返（-Dev） ----")
    if port_listening(8000) or port_listening(3000):
        run_ps_file(OPS_DIR / "stop.ps1", [], "g4_pre_stop", timeout=180)
    c.check(not port_listening(8000) and not port_listening(3000), "前置：端口 8000/3000 空闲")

    rc1, out1 = run_ps_file(OPS_DIR / "start.ps1", ["-Dev", "-NoBrowser"], "g4_start1", timeout=420)
    c.check(rc1 == 0, "start（-Dev，含 dailyhot 正常路径）退出码 0", str(rc1))
    c.check("健康检查全部通过" in out1, "健康检查全绿")
    c.check("xhs-dailyhot healthy" in out1, "dailyhot 正常路径：healthy")
    c.check("D:\\" not in out1 and "C:\\" not in out1, "start 输出无绝对路径")
    c.check("sk-" not in out1, "start 输出无密钥前缀")

    be1 = read_run_json("run-backend.json")
    fe1 = read_run_json("run-frontend.json")
    c.check(be1 is not None and fe1 is not None and be1.get("pid") and fe1.get("pid"), "运行状态文件已写入（PID/端口/日志）")
    if be1:
        c.check("log" in be1 and not str(be1.get("log", "")).startswith(("C:", "D:")), "状态文件内路径为项目相对路径")
    c.check(port_listening(8000) and port_listening(3000), "端口 8000/3000 监听中")
    be_logs = sorted((ROOT_DIR / "logs").glob("backend-*.log"))
    fe_logs = sorted((ROOT_DIR / "logs").glob("frontend-*.log"))
    c.check(bool(be_logs) and be_logs[-1].stat().st_size > 0, "后端日志已落盘（非空）")
    c.check(bool(fe_logs) and fe_logs[-1].stat().st_size > 0, "前端日志已落盘（非空）")

    rcs, outs = run_ps_file(OPS_DIR / "status.ps1", [], "g4_status", timeout=180)
    c.check(rcs == 0 and "监听中" in outs, "status.ps1 正常输出运行态")
    c.check("D:\\" not in outs and "C:\\" not in outs, "status 输出无绝对路径")
    c.check("sk-" not in outs, "status 输出无密钥前缀")

    rc2, out2 = run_ps_file(
        OPS_DIR / "start.ps1", ["-Dev", "-NoBrowser", "-SkipDailyhot"], "g4_start2", timeout=240
    )
    c.check(rc2 == 0, "重复 start 退出码 0")
    c.check("已在运行" in out2 and "不重复拉起" in out2, "重复 start 幂等（检测到已在运行）")
    c.check("已按 -SkipDailyhot 跳过" in out2, "-SkipDailyhot 容错分支：仅 WARNING 不阻断")
    be2 = read_run_json("run-backend.json")
    fe2 = read_run_json("run-frontend.json")
    c.check(
        be2 is not None and fe2 is not None and be2["pid"] == be1["pid"] and fe2["pid"] == fe1["pid"],
        "重复 start 未重复拉起（PID 不变）",
    )

    rc3, out3 = run_ps_file(OPS_DIR / "stop.ps1", [], "g4_stop", timeout=240)
    c.check(rc3 == 0, "stop 退出码 0")
    c.check(out3.count("已释放") >= 2, "停止后端口释放（8000 / 3000）")
    c.check(not port_listening(8000) and not port_listening(3000), "端口复核：确认空闲")
    c.check(
        not (ROOT_DIR / "logs" / "run-backend.json").exists()
        and not (ROOT_DIR / "logs" / "run-frontend.json").exists(),
        "PID / 状态文件已清理",
    )


# ---------------------------------------------------------------------------
# 组 5：备份 → 恢复往返
# ---------------------------------------------------------------------------
def group5_backup_restore(c: Checker) -> None:
    print("\n---- 组 5：备份 → 恢复往返 ----")
    rc, out = run_ps_file(OPS_DIR / "backup.ps1", [], "g5_backup", timeout=240)
    c.check(rc == 0 and "备份完成" in out, "真实备份成功（pg_dump -Fc）", f"rc={rc}")
    c.check("完整性校验" in out and "归档条目" in out, "备份完整性校验（pg_restore -l 条目数）")
    c.check(".env" in out and "不纳入备份" in out, "输出声明：.env 不纳入备份")
    dumps = sorted((ROOT_DIR / "backups").glob("xhs_agent-*.dump"))
    c.check(bool(dumps) and dumps[-1].stat().st_size > 0, "备份文件已落盘（非空）")
    latest = dumps[-1]
    hist = ROOT_DIR / "logs" / "backup-history.log"
    c.check(hist.exists() and "| OK |" in hist.read_text(encoding="utf-8", errors="replace"), "备份历史已记录")

    # 保留策略（临时输出目录 + 2 个旧占位文件 → 保留 2 份、清理 1 份）
    tmp_out = TMP_BASE / "backuptest"
    tmp_out.mkdir(exist_ok=True)
    for name in ("xhs_agent-20260101-000000.dump", "xhs_agent-20260102-000000.dump"):
        (tmp_out / name).write_bytes(b"dummy")
    rc, out = run_ps_file(
        OPS_DIR / "backup.ps1", ["-OutputDir", str(tmp_out), "-RetentionCount", "2"], "g5_retention", timeout=240
    )
    files = sorted(p.name for p in tmp_out.glob("xhs_agent-*.dump"))
    c.check(
        rc == 0 and len(files) == 2 and "xhs_agent-20260101-000000.dump" not in files,
        "保留策略：按最近 N 份清理旧备份（保留 2 / 清理 1）",
        str(files),
    )
    c.check("清理：" in out, "清理清单已打印")

    # 默认恢复 → 临时验证库（保留待核对）
    rc, out = run_ps_file(OPS_DIR / "restore.ps1", ["-File", str(latest)], "g5_restore1", timeout=300)
    c.check(rc == 0 and "恢复成功" in out, "恢复（默认 → 临时验证库）成功", f"rc={rc}")
    c.check("关键表行数与源库一致" in out, "恢复往返证据：9 张关键表行数与源库一致")
    c.check("public 表数量：11" in out and "vector 扩展存在" in out, "恢复结构校验（表数量 / 扩展）")
    m = re.search(r"临时验证库已保留：(\S+)", out)
    vdb = m.group(1) if m else ""
    c.check(bool(vdb) and db_exists(vdb), "验证库真实存在（主库未被改动）")
    if vdb and db_exists(vdb):
        drop_db(vdb)
    c.check(bool(vdb) and not db_exists(vdb), "验证库已删除（自检收尾）")

    # -DropVerifyDb：恢复后即删
    rc, out = run_ps_file(OPS_DIR / "restore.ps1", ["-File", str(latest), "-DropVerifyDb"], "g5_restore2", timeout=300)
    m2 = re.search(r"临时验证库已删除：(\S+)", out)
    vdb2 = m2.group(1) if m2 else ""
    c.check(rc == 0 and bool(vdb2) and not db_exists(vdb2), "-DropVerifyDb：恢复 + 校验 + 删除一条龙", f"rc={rc} db={vdb2}")

    # 负例：文件不存在
    rc, out = run_ps_file(OPS_DIR / "restore.ps1", ["-File", "backups\\not-exist.dump"], "g5_negative", timeout=120)
    c.check(rc == 2 and "不存在" in out, "恢复负例：备份文件不存在 → 退出码 2", f"rc={rc}")


# ---------------------------------------------------------------------------
# 组 6：计划任务注册 / 卸载往返（自启 + 定时备份）
# ---------------------------------------------------------------------------
def group6_tasks(c: Checker) -> None:
    print("\n---- 组 6：计划任务注册 / 卸载往返 ----")

    def query_task(task_name: str, extra: str = "") -> str:
        code = (
            "$t = Get-ScheduledTask -TaskName '" + task_name + "' -TaskPath '\\xhs-agent\\' -ErrorAction SilentlyContinue; "
            "Write-Output ('F=' + [bool]$t); "
            "if ($t) { Write-Output ('STATE=' + $t.State) "
            + extra
            + " }"
        )
        _, out = run_ps_cmd(code, "g6_q_" + task_name, timeout=90)
        return out

    rc, out = run_ps_file(OPS_DIR / "install-autostart.ps1", [], "g6_install_auto", timeout=180)
    c.check(rc == 0 and "已注册" in out, "自启任务注册成功", f"rc={rc}")
    c.check("sk-" not in out and "D:\\" not in out, "注册输出无绝对路径 / 密钥")
    out_q = query_task("xhs-agent-autostart", "Write-Output ('DELAY=' + $t.Triggers[0].Delay)")
    c.check("F=True" in out_q, "注册后回读可查（Get-ScheduledTask）", out_q)
    c.check("DELAY=PT45S" in out_q, "登录延迟触发器生效（PT45S）")
    c.check("STATE=Ready" in out_q, "任务状态 Ready")

    rc, out = run_ps_file(OPS_DIR / "uninstall-autostart.ps1", [], "g6_uninstall_auto", timeout=180)
    c.check(rc == 0 and "已删除" in out, "自启任务卸载成功")
    out_q = query_task("xhs-agent-autostart")
    c.check("F=False" in out_q, "卸载后回读查无（自启）")

    rc, out = run_ps_file(OPS_DIR / "install-backup-task.ps1", ["-DailyAt", "03:30"], "g6_install_bk", timeout=180)
    c.check(rc == 0 and "已注册" in out, "定时备份任务注册成功", f"rc={rc}")
    out_q = query_task("xhs-agent-backup")
    c.check("F=True" in out_q, "备份任务回读可查")
    rc, out = run_ps_file(OPS_DIR / "install-backup-task.ps1", ["-DailyAt", "25:99"], "g6_bad_time", timeout=120)
    c.check(rc == 1 and "时间格式非法" in out, "备份任务时间参数校验（负例：25:99 拒绝）", f"rc={rc}")

    rc, out = run_ps_file(OPS_DIR / "uninstall-backup-task.ps1", [], "g6_uninstall_bk", timeout=180)
    c.check(rc == 0, "备份任务卸载成功")
    code = (
        "$any = Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -like 'xhs-agent*' }; "
        "Write-Output ('ANY=' + [bool]$any); "
        "$svc = New-Object -ComObject Schedule.Service; $svc.Connect(); $root = $svc.GetFolder('\\'); "
        "$names = @(); foreach ($f in $root.GetFolders(0)) { $names += $f.Name }; "
        "Write-Output ('FOLDERS=' + ($names -join ','))"
    )
    _, out = run_ps_cmd(code, "g6_final_query", timeout=90)
    c.check("ANY=False" in out, "全部卸载后无 xhs-agent 任务残留（回读）")
    c.check("FOLDERS=" in out and "xhs-agent" not in out.split("FOLDERS=")[1], "任务文件夹已清理（无残留）", out)


# ---------------------------------------------------------------------------
# 组 7：-Watch 常驻 / 崩溃自愈 / stop 联动
# ---------------------------------------------------------------------------
def group7_watch(c: Checker) -> None:
    print("\n---- 组 7：-Watch 常驻与崩溃自愈 ----")
    if port_listening(8000) or port_listening(3000):
        run_ps_file(OPS_DIR / "stop.ps1", [], "g7_pre_stop", timeout=180)

    live_out = TMP_BASE / "g7_watch_live.txt"
    fh = open(live_out, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(OPS_DIR / "start.ps1"),
            "-Watch",
            "-Dev",
            "-NoBrowser",
            "-SkipDailyhot",
        ],
        stdout=fh,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        cwd=str(ROOT_DIR),
    )
    try:
        ok = False
        for _ in range(40):
            time.sleep(3)
            if health_green():
                ok = True
                break
        c.check(ok, "常驻模式（-Watch）启动：健康检查全绿")
        if not ok:
            return

        w1 = None
        for _ in range(20):
            w1 = read_run_json("run-watch.json")
            if w1:
                break
            time.sleep(2)
        c.check(w1 is not None and w1.get("pid"), "run-watch.json 已写入（监控进程状态）")
        be1 = read_run_json("run-backend.json")
        if not be1:
            c.check(False, "run-backend.json 缺失，无法模拟崩溃")
            return

        subprocess.run(
            ["taskkill", "/PID", str(be1["pid"]), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        recovered = False
        be2 = None
        for _ in range(35):
            time.sleep(3)
            be2 = read_run_json("run-backend.json")
            if be2 and be2.get("pid") and be2["pid"] != be1["pid"] and health_green():
                recovered = True
                break
        c.check(recovered, "模拟崩溃 → 退避自动重启并恢复健康", f"be2={be2}")
        c.check(
            bool(be2) and be2.get("restart_count") == 1,
            "重启计数已记录（restart_count=1）",
            str((be2 or {}).get("restart_count")),
        )
        wl_files = sorted((ROOT_DIR / "logs").glob("watch-*.log"))
        wl = wl_files[-1].read_text(encoding="utf-8", errors="replace") if wl_files else ""
        c.check("第 1 次重启" in wl and "已重启" in wl, "watch 日志记录重启原因与结果")

        rc, out = run_ps_file(OPS_DIR / "stop.ps1", [], "g7_stop", timeout=240)
        c.check(rc == 0, "stop 退出码 0（手动停止不再重启）")
        try:
            prc = proc.wait(timeout=30)
            c.check(prc == 0, "监控进程收到停止请求后自行退出（码 0）", str(prc))
        except subprocess.TimeoutExpired:
            proc.kill()
            c.check(False, "监控进程 30 秒内未退出")
        c.check(not (ROOT_DIR / "logs" / "run-watch.json").exists(), "watch 状态文件已清理")
        c.check(not (ROOT_DIR / "logs" / "stop.request").exists(), "停止请求标记已清理")
        c.check(not port_listening(8000) and not port_listening(3000), "端口已释放（8000 / 3000）")
    finally:
        fh.close()
        if proc.poll() is None:
            proc.kill()
        run_ps_file(OPS_DIR / "stop.ps1", [], "g7_cleanup", timeout=180)


# ---------------------------------------------------------------------------
# 组 8：收口修复验证（单事务 / 去重 / 白名单负断言）
# ---------------------------------------------------------------------------
def _mk_note_fixture() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    with SessionLocal() as db:
        account = Account(name=f"{PREFIX}账号", status=AccountStatus.ACTIVE)
        db.add(account)
        db.flush()
        persona = Persona(name=f"{PREFIX}人设", account_id=account.id, status=PersonaStatus.ACTIVE)
        db.add(persona)
        db.flush()
        note = Note(
            account_id=account.id,
            persona_id=persona.id,
            title=f"{PREFIX}笔记",
            status=NoteStatus.DRAFT,
            compliance_status=ComplianceStatus.PENDING,
        )
        db.add(note)
        db.flush()
        ids = (account.id, persona.id, note.id)
        db.commit()
    return ids


def group8_fixes(c: Checker, client: TestClient) -> None:
    print("\n---- 组 8：收口修复验证 ----")

    # ---- covers：组合 PATCH 单事务（字段 + 互斥选中一次提交）----
    account_id, persona_id, note_id = _mk_note_fixture()
    try:
        r1 = client.post(
            "/api/v1/covers",
            json={"note_id": str(note_id), "template_key": "gradient_hero", "color_scheme": "coral"},
        )
        r2 = client.post(
            "/api/v1/covers",
            json={"note_id": str(note_id), "template_key": "gradient_hero", "color_scheme": "coral"},
        )
        c.check(r1.status_code == 200 and r2.status_code == 200, "封面候选创建（前置）", f"{r1.status_code}/{r2.status_code}")
        cid_a = r1.json().get("id")
        cid_b = r2.json().get("id")
        rp = client.patch(
            f"/api/v1/covers/{cid_b}", json={"title_text": f"{PREFIX}组合PATCH", "is_selected": True}
        )
        body = rp.json() if rp.status_code == 200 else {}
        c.check(
            rp.status_code == 200
            and body.get("title_text") == f"{PREFIX}组合PATCH"
            and body.get("is_selected") is True,
            "covers 组合 PATCH：字段 + 互斥选中 单次请求全部生效",
            f"{rp.status_code} {rp.text[:160]}",
        )
        with SessionLocal() as db:
            rows = db.execute(
                text("SELECT id, is_selected, title_text FROM covers WHERE note_id = :n"),
                {"n": str(note_id)},
            ).all()
        selected = [r for r in rows if r[1]]
        c.check(
            len(selected) == 1 and str(selected[0][0]) == cid_b and selected[0][2] == f"{PREFIX}组合PATCH",
            "DB 复核：恰一个选中且为 B（字段与选中同事务落库）",
            str(rows),
        )
        rp2 = client.patch(
            f"/api/v1/covers/{cid_b}", json={"subtitle_text": f"{PREFIX}取消选中", "is_selected": False}
        )
        b2 = rp2.json() if rp2.status_code == 200 else {}
        c.check(
            rp2.status_code == 200 and b2.get("is_selected") is False and b2.get("subtitle_text") == f"{PREFIX}取消选中",
            "covers 组合 PATCH：字段 + 取消选中同样生效",
            f"{rp2.status_code}",
        )
    finally:
        with SessionLocal() as db:
            db.execute(text("DELETE FROM notes WHERE id = :n"), {"n": str(note_id)})
            db.execute(text("DELETE FROM personas WHERE id = :p"), {"p": str(persona_id)})
            db.execute(text("DELETE FROM accounts WHERE id = :a"), {"a": str(account_id)})
            db.commit()

    # ---- knowledge：组合 PATCH 单事务（status + 字段）----
    sid = ""
    with _EmbedEnv(FakeEmbedder()):
        resp = client.post(
            "/api/v1/knowledge/manual",
            json={"chunk_type": "general", "title": f"{PREFIX}组合块", "content": f"{PREFIX}组合正文 v1"},
        )
    if resp.status_code == 201:
        sid = resp.json().get("id", "")
    c.check(resp.status_code == 201 and bool(sid), "知识块创建（前置，假 embedder）", f"{resp.status_code}")
    if sid:
        rp = client.patch(f"/api/v1/knowledge/{sid}", json={"status": "archived", "title": f"{PREFIX}组合改"})
        b3 = rp.json() if rp.status_code == 200 else {}
        c.check(
            rp.status_code == 200 and b3.get("status") == "archived" and b3.get("title") == f"{PREFIX}组合改",
            "knowledge 组合 PATCH：status + title 单次请求全部生效",
            f"{rp.status_code} {rp.text[:160]}",
        )
        with _EmbedEnv(FakeEmbedder()):
            rp2 = client.patch(
                f"/api/v1/knowledge/{sid}",
                json={"status": "active", "content": f"{PREFIX}组合正文 v2"},
            )
        b4 = rp2.json() if rp2.status_code == 200 else {}
        has_vec = psql_scalar(
            "xhs_agent",
            f"SELECT count(*) FROM knowledge_snippets WHERE id = '{sid}' AND embedding IS NOT NULL",
        )
        c.check(
            rp2.status_code == 200 and b4.get("status") == "active" and b4.get("content") == f"{PREFIX}组合正文 v2",
            "knowledge 组合 PATCH：status + content（重向量化成功）全部生效",
            f"{rp2.status_code}",
        )
        c.check(has_vec == "1", "重向量化成功：embedding 已写入")
        with _EmbedEnv(ThrowingEmbedder()):
            rp3 = client.patch(
                f"/api/v1/knowledge/{sid}",
                json={"status": "archived", "content": f"{PREFIX}组合正文 v3"},
            )
        b5 = rp3.json() if rp3.status_code == 200 else {}
        has_vec2 = psql_scalar(
            "xhs_agent",
            f"SELECT count(*) FROM knowledge_snippets WHERE id = '{sid}' AND embedding IS NOT NULL",
        )
        c.check(
            rp3.status_code == 200
            and b5.get("status") == "archived"
            and b5.get("content") == f"{PREFIX}组合正文 v3"
            and bool(b5.get("warnings")),
            "重向量化失败降级：正文与 status 仍同事务保存 + warnings（不丢正文）",
            f"{rp3.status_code} {rp3.text[:200]}",
        )
        c.check(has_vec2 == "0", "重向量化失败：embedding 已置空")
        # 白名单负断言：values 中的 status 被拒绝（不静默改状态）
        with SessionLocal() as db:
            before = crud_knowledge.get_snippet(db, uuid.UUID(sid))
            before_status = before.status.value if before else ""
        with SessionLocal() as db:
            crud_knowledge.apply_admin_update(db, uuid.UUID(sid), values={"status": SnippetStatus.ARCHIVED})
        with SessionLocal() as db:
            after = crud_knowledge.get_snippet(db, uuid.UUID(sid))
            after_status = after.status.value if after else ""
        c.check(
            before_status == after_status,
            "白名单负断言：apply_admin_update 的 values 中 status 被拒绝（状态仅能经显式参数修改）",
            f"{before_status} -> {after_status}",
        )
        with SessionLocal() as db:
            db.execute(text("DELETE FROM knowledge_snippets WHERE id = :i"), {"i": sid})
            db.commit()

    # ---- topics：显式 hotspot_ids 去重（保序 / missing 去重）----
    with SessionLocal() as db:
        h1 = Hotspot(source="selftest", title=f"{PREFIX}热点A", content_hash=uuid.uuid4().hex, status=HotspotStatus.NEW)
        h2 = Hotspot(source="selftest", title=f"{PREFIX}热点B", content_hash=uuid.uuid4().hex, status=HotspotStatus.NEW)
        db.add_all([h1, h2])
        db.flush()
        h1_id, h2_id = h1.id, h2.id
        missing_id = uuid.uuid4()
        payloads, missing = topics_service._resolve_hotspot_payloads(db, [h1_id, h1_id, h2_id, h1_id], 5)
        c.check(
            [p["id"] for p in payloads] == [str(h1_id), str(h2_id)],
            "热点显式传入去重（保序：A,B；重复不重复处理）",
            str([p["id"] for p in payloads]),
        )
        c.check(missing == [], "去重后 missing 为空")
        payloads2, missing2 = topics_service._resolve_hotspot_payloads(db, [h1_id, missing_id, h1_id], 5)
        c.check(
            len(payloads2) == 1 and missing2 == [str(missing_id)],
            "重复的缺失 id 只记一次（missing 去重）",
            f"{payloads2} {missing2}",
        )
        db.delete(h1)
        db.delete(h2)
        db.commit()


# ---------------------------------------------------------------------------
# 组 9：零迁移断言
# ---------------------------------------------------------------------------
def group9_migrations(c: Checker) -> None:
    print("\n---- 组 9：零迁移断言 ----")
    versions = [p for p in VERSIONS_DIR.glob("*.py") if not p.name.startswith("__")]
    c.check(len(versions) == 2, f"migrations/versions 恰 2 支（实际 {len(versions)}）")

    r = subprocess.run(
        [str(PY_EXE), "-m", "alembic", "current"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(BACKEND_DIR),
        timeout=180,
    )
    out = (r.stdout or "") + (r.stderr or "")
    c.check(r.returncode == 0 and "(head)" in out and "0002" in out, "alembic current = 0002 (head)", out[-200:])

    r2 = subprocess.run(
        [str(PY_EXE), "-m", "alembic", "check"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(BACKEND_DIR),
        timeout=180,
    )
    c.check(r2.returncode == 0, "alembic check 零漂移（退出码 0）", ((r2.stdout or "") + (r2.stderr or ""))[-200:])

    pub = psql_scalar("xhs_agent", "SELECT count(*) FROM pg_tables WHERE schemaname='public'")
    c.check(pub == "11", f"public 恰 11 张（10 业务 + alembic_version，实际 {pub}）")
    names = set(psql_lines("xhs_agent", "SELECT tablename FROM pg_tables WHERE schemaname='public'"))
    c.check(names - {"alembic_version"} == BUSINESS_TABLES, "业务表恰为 10 张且名单一致", str(names))
    lg = psql_scalar("xhs_agent", "SELECT count(*) FROM pg_tables WHERE schemaname='langgraph'")
    c.check(lg == "4", f"langgraph 框架表 4 张（实际 {lg}）")


# ---------------------------------------------------------------------------
# 组 10：收尾（双口径清理 + 全局残留复查）
# ---------------------------------------------------------------------------
def group10_cleanup(c: Checker) -> None:
    print("\n---- 组 10：收尾清理与残留复查 ----")
    with SessionLocal() as db:
        db.execute(text("DELETE FROM knowledge_snippets WHERE title LIKE :p OR content LIKE :p"), {"p": f"%{PREFIX}%"})
        db.execute(text("DELETE FROM hotspots WHERE title LIKE :p"), {"p": f"%{PREFIX}%"})
        db.execute(text("DELETE FROM notes WHERE title LIKE :p"), {"p": f"%{PREFIX}%"})
        db.execute(text("DELETE FROM personas WHERE name LIKE :p"), {"p": f"%{PREFIX}%"})
        db.execute(text("DELETE FROM accounts WHERE name LIKE :p"), {"p": f"%{PREFIX}%"})
        db.commit()
        left = {
            "knowledge_snippets": db.execute(
                text("SELECT count(*) FROM knowledge_snippets WHERE title LIKE :p OR content LIKE :p"), {"p": f"%{PREFIX}%"}
            ).scalar_one(),
            "hotspots": db.execute(text("SELECT count(*) FROM hotspots WHERE title LIKE :p"), {"p": f"%{PREFIX}%"}).scalar_one(),
            "notes": db.execute(text("SELECT count(*) FROM notes WHERE title LIKE :p"), {"p": f"%{PREFIX}%"}).scalar_one(),
            "personas": db.execute(text("SELECT count(*) FROM personas WHERE name LIKE :p"), {"p": f"%{PREFIX}%"}).scalar_one(),
            "accounts": db.execute(text("SELECT count(*) FROM accounts WHERE name LIKE :p"), {"p": f"%{PREFIX}%"}).scalar_one(),
        }
    c.check(sum(left.values()) == 0, "测试数据双口径清理后残留为 0", str(left))

    verify_left = temp_dbs("xhs_restore_verify_")
    c.check(len(verify_left) == 0, "无 xhs_restore_verify 残留库", str(verify_left))

    code = (
        "$any = Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -like 'xhs-agent*' }; "
        "Write-Output ('ANY=' + [bool]$any)"
    )
    _, out = run_ps_cmd(code, "g10_tasks", timeout=90)
    c.check("ANY=False" in out, "无计划任务残留（收尾）")

    c.check(not port_listening(8000) and not port_listening(3000), "收尾：端口 8000/3000 空闲")
    c.check(
        not (ROOT_DIR / "logs" / "stop.request").exists()
        and not (ROOT_DIR / "logs" / "run-watch.json").exists(),
        "收尾：无停止标记 / 监控状态残留",
    )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    c = Checker()
    print("=" * 62)
    print(" 第 20 步 · 健壮性与本地体验 离线自检（selftest_ops）")
    print(" 临时目录：" + str(TMP_BASE))
    print("=" * 62)

    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        print(f"FATAL: 本地 PostgreSQL 不可达（请先启动 docker compose 的 xhs-db）：{exc}")
        shutil.rmtree(TMP_BASE, ignore_errors=True)
        return 1

    r = subprocess.run(["docker", "version"], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print("FATAL: docker 不可用（请先启动 Docker Desktop）。")
        shutil.rmtree(TMP_BASE, ignore_errors=True)
        return 1

    client = TestClient(app, raise_server_exceptions=False)
    try:
        group1_static(c)
        group2_precheck(c)
        group3_functions(c)
        group4_startstop(c)
        group5_backup_restore(c)
        group6_tasks(c)
        group7_watch(c)
        group8_fixes(c, client)
        group9_migrations(c)
        group10_cleanup(c)
    finally:
        # 兜底：确保服务已停止 / 计划任务已撤销 / 临时目录删除
        try:
            run_ps_file(OPS_DIR / "stop.ps1", [], "final_stop", timeout=180)
        except Exception:  # noqa: BLE001
            pass
        for name in ("uninstall-autostart.ps1", "uninstall-backup-task.ps1"):
            try:
                run_ps_file(OPS_DIR / name, [], "final_" + name, timeout=120)
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(TMP_BASE, ignore_errors=True)

    print()
    print("=" * 62)
    if c.failures:
        print(f" 通过 {c.passed} 项，失败 {len(c.failures)} 项，跳过 {len(c.skipped)} 项。")
        for name in c.failures:
            print(f"  [FAIL] {name}")
        print(" 存在失败项 —— 请逐项排查。")
        print("=" * 62)
        return 1
    print(f" 通过 {c.passed} 项，失败 0 项，跳过 {len(c.skipped)} 项。")
    for name in c.skipped:
        print(f"  - SKIP: {name}")
    print(" 全部 PASS ✔（离线自检：真实容器 + 真实脚本往返；不联网、不消耗 token）")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
