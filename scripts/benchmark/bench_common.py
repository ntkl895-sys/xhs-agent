# -*- coding: utf-8 -*-
"""度量专项（benchmark 套件）· 公共基座。

本模块是所有 benchmark 脚本共享的工具层，提供：

- 统一的指标 schema：``name / value / unit / n / category / method / env /
  timestamp / log_path / confidence``（``note`` 为可选补充，N/A 原因写在这里）；
- 结果落盘（``docs/benchmark/results/``）与原始日志（``docs/benchmark/logs/``）；
- 环境指纹、代理清洗、百分位、子进程捕获、临时数据库、回环 HTTP 等工具。

设计原则（与任务书一致）：

1. 全部指标均来自实跑；跑不出的指标写 ``value=None`` + ``note`` 说明原因，**不伪造**；
2. 结果文件与日志一律 UTF-8（子进程强制 ``PYTHONIOENCODING=utf-8``）；
3. 不引入任何新第三方依赖（只用标准库；HTTP 用 ``urllib`` 且显式绕开系统代理）；
4. 只用 ``docker compose exec`` 在本地容器上操作临时数据库，不触碰既有库。

四类口径（写入每条指标的 ``category`` 字段）：

- ``offline_objective``：离线客观值（确定性、可复跑，与网络/LLM 无关）
- ``llm_small_sample``：真实 LLM 小样本抽样（必须带 n，不得外推）
- ``synthetic_fixture``：合成夹具结果（仅验证管线/正确性）
- ``not_measured``：尚未测 / 跑不出（如实标注 N/A）

用法：benchmark 各脚本与 bench_common.py 同目录，直接 ``import bench_common as bc``。
所有脚本从项目根运行：

    backend\\.venv\\Scripts\\python.exe scripts\\benchmark\\bench_xxx.py
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[2]            # xhs-agent/
SCRIPTS_DIR = ROOT / "scripts"
BACKEND_DIR = ROOT / "backend"
VENV_PY = BACKEND_DIR / ".venv" / "Scripts" / "python.exe"
BENCH_ROOT = ROOT / "docs" / "benchmark"
RESULTS_DIR = BENCH_ROOT / "results"
LOGS_DIR = BENCH_ROOT / "logs"
FIXTURES_DIR = BENCH_ROOT / "fixtures"

# ---------------------------------------------------------------------------
# 口径类别
# ---------------------------------------------------------------------------
CATEGORY_OFFLINE = "offline_objective"
CATEGORY_LLM_SAMPLE = "llm_small_sample"
CATEGORY_SYNTHETIC = "synthetic_fixture"
CATEGORY_NA = "not_measured"

_PROXY_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
    "NO_PROXY", "no_proxy",
)

#: Node 可执行文件候选（本机 managed 路径为兜底；优先 PATH）
_NODE_PATH_HINTS = (
    r"C:\Users\35307\AppData\Local\Programs\nodejs\node.exe",
)
#: playwright-core 所在 node_modules（仅合成截图夹具生成需要）
_NODE_MODULES_HINTS = (
    r"C:\Users\35307\.workbuddy\binaries\node\workspace\node_modules",
)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def ensure_dirs() -> None:
    """确保 docs/benchmark 全部子目录存在。"""
    for directory in (RESULTS_DIR, LOGS_DIR, FIXTURES_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def clean_env(extra: dict | None = None) -> dict:
    """构造子进程环境：去掉代理注入变量、强制 UTF-8、可选追加覆盖项。"""
    env = {key: value for key, value in os.environ.items() if key not in _PROXY_KEYS}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if extra:
        env.update({key: str(value) for key, value in extra.items()})
    return env


def add_backend_to_path() -> None:
    """把 backend/ 加入 sys.path（需要 import app.* 的脚本调用）。"""
    backend = str(BACKEND_DIR)
    if backend not in sys.path:
        sys.path.insert(0, backend)


def now_iso() -> str:
    """带时区偏移的 ISO 8601 时间戳（本地时区）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def smart_decode(raw: bytes) -> str:
    """宽容解码：UTF-8 优先，失败退 GBK，再失败 replace（不抛异常）。"""
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def find_node() -> str | None:
    """定位 node 可执行文件（PATH 优先，本机 managed 路径兜底）。"""
    found = shutil.which("node")
    if found:
        return found
    for hint in _NODE_PATH_HINTS:
        if Path(hint).exists():
            return hint
    return None


def node_env(extra: dict | None = None) -> dict:
    """node 子进程环境：附带 NODE_PATH（playwright-core 所在 node_modules）。"""
    env = clean_env(extra)
    override = os.environ.get("BENCH_NODE_PATH")
    if override:
        env["NODE_PATH"] = override
        return env
    for hint in _NODE_MODULES_HINTS:
        if Path(hint).exists():
            env["NODE_PATH"] = hint
            break
    return env


def _probe_node_version() -> str | None:
    node = find_node()
    if not node:
        return None
    try:
        proc = subprocess.run(
            [node, "--version"], capture_output=True, timeout=20
        )
        return smart_decode((proc.stdout or b"")).strip() or None
    except Exception:  # noqa: BLE001 - 版本探测失败不影响任何指标
        return None


def env_fingerprint() -> dict:
    """环境指纹（写入结果文件与每条指标）。"""
    return {
        "os": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "node": _probe_node_version(),
        "scope": "本地单机（localhost，Docker Desktop 容器化依赖）",
    }


def env_string() -> str:
    """单行环境描述（指标 env 字段默认值）。"""
    fingerprint = env_fingerprint()
    node = fingerprint.get("node") or "n/a"
    return (
        f"{fingerprint['os']} / {fingerprint['machine']} / "
        f"Python {fingerprint['python']} / Node {node} / 本地单机（localhost）"
    )


# ---------------------------------------------------------------------------
# 统计工具
# ---------------------------------------------------------------------------
def percentile(values: list[float], p: float) -> float | None:
    """nearest-rank 百分位（口径：排序后取第 ceil(p/100*n) 个样本）。"""
    if not values:
        return None
    ordered = sorted(values)
    k = max(1, math.ceil(p / 100.0 * len(ordered)))
    return ordered[k - 1]


def stat_block(values: list[float]) -> dict | None:
    """一组样本的 min / p50 / p95 / max / mean（空列表返回 None）。"""
    if not values:
        return None
    return {
        "n": len(values),
        "min": round(min(values), 3),
        "p50": round(percentile(values, 50), 3),
        "p95": round(percentile(values, 95), 3),
        "max": round(max(values), 3),
        "mean": round(sum(values) / len(values), 3),
    }


# ---------------------------------------------------------------------------
# 指标 schema 与落盘
# ---------------------------------------------------------------------------
def metric(
    name: str,
    value,
    unit: str,
    *,
    n: int | None = None,
    category: str = CATEGORY_OFFLINE,
    method: str,
    log_path: str | None = None,
    confidence: str = "high",
    env: str | None = None,
    note: str | None = None,
) -> dict:
    """构造一条标准指标记录（全部字段：name/value/unit/n/category/method/env/
    timestamp/log_path/confidence，+ 可选 note）。"""
    return {
        "name": name,
        "value": value,
        "unit": unit,
        "n": n,
        "category": category,
        "method": method,
        "env": env or env_string(),
        "timestamp": now_iso(),
        "log_path": log_path,
        "confidence": confidence,
        "note": note,
    }


def na_metric(
    name: str,
    *,
    method: str,
    reason: str,
    unit: str = "n/a",
    n: int | None = None,
    log_path: str | None = None,
    env: str | None = None,
) -> dict:
    """构造一条「如实 N/A」指标（未测/跑不出；原因写 note）。"""
    return metric(
        name, None, unit,
        n=n, category=CATEGORY_NA, method=method,
        log_path=log_path, confidence="none", env=env, note=reason,
    )


def write_json(path: Path, payload) -> None:
    """UTF-8 原子写 JSON。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def write_suite_result(
    filename: str,
    *,
    suite: str,
    metrics: list[dict],
    detail=None,
    summary=None,
    notes=None,
    env_extra=None,
) -> Path:
    """落盘一个套件结果 JSON（docs/benchmark/results/<filename>）。"""
    ensure_dirs()
    payload = {
        "suite": suite,
        "generated_at": now_iso(),
        "env": {**env_fingerprint(), **(env_extra or {})},
        "summary": summary or {},
        "notes": notes or [],
        "detail": detail or [],
        "metrics": metrics,
    }
    path = RESULTS_DIR / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, payload)
    return path


# ---------------------------------------------------------------------------
# 冻结测试集（带 ground truth 的评测夹具）
# ---------------------------------------------------------------------------
def testsets_dir() -> Path:
    """测试集根目录：docs/benchmark/fixtures/testsets/。"""
    return FIXTURES_DIR / "testsets"


def load_frozen_testsets(name: str, filenames: list[str]) -> tuple[dict, dict]:
    """读取冻结测试集文件并校验 sha256 指纹。

    - 测试集须先由 ``build_testsets.py`` 生成（先冻结、后评测）；
    - 任何对夹具文件的改动都会使 sha256 不匹配 → 直接抛错（评测只读不改）。

    Returns:
        (manifest, {filename: payload})
    """
    base = testsets_dir() / name
    manifest_path = base / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(
            f"测试集 {name} 未生成：请先运行 scripts/benchmark/build_testsets.py"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files: dict = {}
    for filename in filenames:
        path = base / filename
        payload = json.loads(path.read_text(encoding="utf-8"))
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        expected_digest = (manifest.get("sha256") or {}).get(filename)
        if digest != expected_digest:
            raise RuntimeError(
                f"夹具 {name}/{filename} 与冻结指纹不符（sha256 不匹配）——"
                "评测只读不改；如确需修改，请升 version 并以 --force 重新生成、"
                "在报告中保留前后两版结果。"
            )
        files[filename] = payload
    return manifest, files


# ---------------------------------------------------------------------------
# 子进程执行
# ---------------------------------------------------------------------------
def run_capture(
    cmd,
    *,
    cwd=None,
    env: dict | None = None,
    log_path=None,
    timeout: int = 900,
) -> tuple[int, str]:
    """执行外部命令；stdout+stderr 合并写入文件，返回 (rc, text)。

    - 输出**直接重定向到文件**（不经管道）：规避「脚本内启动的长命子进程
      继承捕获管道写端 ⇒ 读取端等不到 EOF」的 Windows 经典挂起
      （本机既有踩坑：一键启停脚本 start.ps1 / stop.ps1 场景）；
    - 未提供 log_path 时使用临时文件（读取后即删）；
    - 超时不抛异常：rc = -9，日志尾部追加超时标记（如实记录）；
    - cwd 缺省为项目根。
    """
    if log_path is not None:
        target = Path(log_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        cleanup = False
    else:
        fd, tmp_name = tempfile.mkstemp(prefix="bench_run_", suffix=".log")
        os.close(fd)
        target = Path(tmp_name)
        cleanup = True

    rc = 0
    try:
        with open(target, "wb") as handle:
            try:
                proc = subprocess.run(
                    [str(item) for item in cmd],
                    cwd=str(cwd) if cwd else str(ROOT),
                    env=env if env is not None else clean_env(),
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    timeout=timeout,
                )
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                rc = -9
        if rc == -9:
            with open(target, "ab") as handle:
                handle.write(
                    f"\n[benchmark] 命令超时（>{timeout}s），已终止捕获。\n".encode("utf-8")
                )
        raw = target.read_bytes()
    finally:
        if cleanup:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
    return rc, smart_decode(raw)


def run_ps1(script, args=(), *, log_path=None, timeout: int = 900, env=None):
    """调用 PowerShell 脚本（-ExecutionPolicy Bypass）。"""
    cmd = [
        "powershell.exe", "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-File", str(script),
        *[str(arg) for arg in args],
    ]
    return run_capture(cmd, cwd=ROOT, env=env or clean_env(), log_path=log_path, timeout=timeout)


# ---------------------------------------------------------------------------
# 数据库（只经 docker compose exec，绝不触碰既有库数据）
# ---------------------------------------------------------------------------
def psql(sql: str, *, database: str = "postgres", timeout: int = 90, env=None):
    """在容器内执行 psql（-tA 免表头）。仅用于 benchmark 自建对象的读/建/删。"""
    return run_capture(
        [
            "docker", "compose", "exec", "-T", "db",
            "psql", "-U", "postgres", "-d", database, "-tAc", sql,
        ],
        cwd=ROOT,
        env=env or clean_env(),
        timeout=timeout,
    )


def db_exists(name: str, *, env=None) -> bool:
    rc, out = psql(f"SELECT 1 FROM pg_database WHERE datname = '{name}'", env=env)
    return rc == 0 and out.strip().startswith("1")


def drop_temp_db(name: str, *, env=None) -> bool:
    """删除临时库（带 FORCE，断开遗留连接）。"""
    rc, _ = psql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)', env=env)
    return rc == 0


def temp_db_name(prefix: str = "xhs_bench") -> str:
    """生成带时间戳的临时库名（小写字母数字下划线）。"""
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{prefix}_{stamp}_{os.getpid()}"


# ---------------------------------------------------------------------------
# 回环 HTTP（不走系统代理）
# ---------------------------------------------------------------------------
def http_get(url: str, *, timeout: float = 15.0) -> tuple[int | None, str, float]:
    """本机回环 HTTP GET；返回 (status, body, elapsed_ms)。异常时 status=None。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        url, headers={"User-Agent": "xhs-agent-benchmark/1.0"}
    )
    started = time.perf_counter()
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except Exception as exc:  # noqa: BLE001 - 连接失败如实反馈
        return None, f"{type(exc).__name__}: {exc}", (time.perf_counter() - started) * 1000
    return status, body, (time.perf_counter() - started) * 1000


def wait_http(url: str, *, expect: int = 200, timeout: float = 90.0, interval: float = 1.0):
    """轮询等待 HTTP 就绪；返回 (ok, last_status)。"""
    deadline = time.time() + timeout
    last_status: int | None = None
    while time.time() < deadline:
        status, _body, _ms = http_get(url, timeout=5.0)
        last_status = status
        if status == expect:
            return True, status
        time.sleep(interval)
    return False, last_status


def healthcheck(log_path=None, *, timeout: int = 120):
    """复用项目既有 scripts/healthcheck.py（localhost 四探针）。"""
    return run_capture(
        [str(VENV_PY), str(SCRIPTS_DIR / "healthcheck.py")],
        cwd=ROOT, env=clean_env(), log_path=log_path, timeout=timeout,
    )


# ---------------------------------------------------------------------------
# 统一主函数出口
# ---------------------------------------------------------------------------
EXIT_OK = 0        # 正常完成（含如实 N/A）
EXIT_FAIL = 1      # 出现失败项
EXIT_GATED = 2     # 阶段二门槛：未配置 key / 未就绪，未发任何请求
EXIT_ENV = 3       # 运行环境不满足（服务未启动等），未执行测量


def print_banner(title: str) -> None:
    """统一标题打印。"""
    print("=" * 66)
    print(f"  {title}")
    print("=" * 66)
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# 视觉识别的数值归一化与字段级计分（合成/真实两档共用）
# ---------------------------------------------------------------------------
def canonical_metric_value(raw) -> int | None:
    """把识别/标注的数字文本归一为整数。

    规则（与 REPORT / 计分脚本同步声明）：去掉千分位逗号与空白；支持
    尾部 ``万`` / ``w`` / ``W`` 后缀（×10000）；解析失败返回 ``None``
    （``None`` 与任何值都不判等，如实计为不一致）。
    """
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "").replace("，", "").replace(" ", "")
    if not text:
        return None
    multiplier = 1
    if text.endswith("万"):
        multiplier, text = 10000, text[:-1]
    elif text[-1:] in ("w", "W"):
        multiplier, text = 10000, text[:-1]
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return int(round(value * multiplier))


def score_metric_field(expected, recognized) -> dict:
    """单字段计分：返回归一化前后两侧与是否命中（双 None 判不命中，避免空对空）。"""
    exp_canonical = canonical_metric_value(expected)
    got_canonical = canonical_metric_value(recognized)
    matched = (
        exp_canonical is not None
        and got_canonical is not None
        and exp_canonical == got_canonical
    )
    return {
        "expected_raw": expected,
        "recognized_raw": recognized,
        "expected_canonical": exp_canonical,
        "recognized_canonical": got_canonical,
        "matched": matched,
    }
