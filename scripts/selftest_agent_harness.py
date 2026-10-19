"""Agent Harness 离线自检（**不联网、不需要真实 API Key**）。

逐项断言并输出 PASS / FAIL，覆盖六组：

1. **配置闸门**：``AGENT_LLM_ENABLED=false`` / 缺 key 时，工厂不发起真实调用，
   且报错信息明确可识别；
2. **冒烟图 + 脏 JSON**：FakeListChatModel 跑通冒烟图并产出结构化对象；
   带围栏 / 裸控制字符 / 噪声的输出能被解析且 ``parse_attempts`` 计数正确；
3. **检查点**：Postgres ``setup()`` 幂等（重复调用不报错）、同一 ``thread_id``
   状态可写回 / 读回、``interrupt()`` → ``Command(resume=...)`` 暂停-恢复闭环成立；
4. **框架表隔离**：框架检查点表落在独立 schema；``scripts/selftest_db.py`` 与
   ``scripts/verify_schema.py`` 仍全绿（业务恰好 10 张表）；
5. **重试语义**：可重试错误按 ``AGENT_NODE_MAX_RETRIES`` 重试，4xx 不可重试错误立即失败；
6. **审计与脱敏**：``agent_runs`` 行不含 ``sk-`` 密钥、token 与 ``node_trace`` 就位；
   LangSmith 未配置时确认默认关闭、不上报。

用法（在**项目根目录**下）：

    backend\\.venv\\Scripts\\python.exe scripts\\selftest_agent_harness.py

全部通过退出码 0；任一项 FAIL 退出码 1。
脚本结束时**物理清理**本次产生的 ``agent_runs``（``inputs.kind=harness_smoke``）与
检查点线程，并复查残留为 0。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from langgraph.types import Command  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.agent import errors as agent_errors  # noqa: E402
from app.agent import checkpoint as ckpt  # noqa: E402
from app.agent.examples import smoke_graph as S  # noqa: E402
from app.agent.graph.base import make_config, new_thread_id  # noqa: E402
from app.agent.llm import factory as llm_factory  # noqa: E402
from app.agent.parsing.json_repair import invoke_json  # noqa: E402
from app.agent.runner import describe_retry_semantics, run_agent  # noqa: E402
from app.agent.tracing import langsmith  # noqa: E402
from app.agent.tracing.recorder import REDACTED  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.db.session import engine  # noqa: E402

# 本次自检创建的全部检查点线程，收尾时清理
_CREATED_THREADS: list[str] = []


class Checker:
    """极简的 PASS/FAIL 收集器（与第 7 / 8 步脚本风格一致）。"""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def check(self, ok: bool, label: str, detail: str = "") -> None:
        if ok:
            self.passed += 1
            print(f"[PASS] {label}")
        else:
            self.failures.append(label)
            suffix = f"  ->  {detail}" if detail else ""
            print(f"[FAIL] {label}{suffix}")

    def section(self, title: str) -> None:
        print(f"\n=== {title} ===")


def _smoke_state(require_human: bool = False, thread_id: str = "t") -> dict:
    """构造冒烟图的初始状态（纯数据，可安全序列化）。"""
    return {
        "kind": S.SMOKE_KIND,
        "run_id": "selftest",
        "thread_id": thread_id,
        "input": {"require_human": require_human},
        "output": None,
        "draft": None,
        "errors": [],
        "parse_attempts": 0,
        "usage": {},
        "node_trace": [],
        "human_payload": None,
    }


# ---------------------------------------------------------------------------
# 1) 配置闸门
# ---------------------------------------------------------------------------
def check_llm_gate(c: Checker) -> None:
    c.section("1) 配置闸门：关闭时绝不发起真实调用")

    readiness = llm_factory.describe_llm_readiness()
    snapshot_blob = str(readiness)
    c.check(
        "ready" in readiness and "has_api_key" in readiness,
        "就绪状态快照含就绪判定字段",
        f"{readiness}",
    )
    c.check(
        "sk-" not in snapshot_blob
        and (not settings.LLM_API_KEY.strip() or settings.LLM_API_KEY not in snapshot_blob),
        "就绪状态快照不含密钥明文",
        "快照里出现了密钥形态字符串",
    )

    # 环境无关：先注入「关闭」配置跑负向断言，再恢复；随后注入就绪配置跑正向断言。
    # 关键点：断言真实异常类型 / 错误关键串 / 就绪位，不再用 c.check(True, …) 占位。
    original_enabled = settings.AGENT_LLM_ENABLED
    original_key = settings.LLM_API_KEY
    original_model = settings.LLM_MODEL

    raised: BaseException | None = None
    try:
        settings.AGENT_LLM_ENABLED = False
        settings.LLM_API_KEY = ""
        try:
            llm_factory.get_chat_model()
        except BaseException as exc:  # noqa: BLE001
            raised = exc
        c.check(
            isinstance(raised, agent_errors.AgentConfigError),
            "关闭时 get_chat_model 抛 AgentConfigError（注入 AGENT_LLM_ENABLED=false）",
            f"实际={type(raised).__name__ if raised else 'None'}",
        )
        message = str(raised) if raised else ""
        c.check(
            "AGENT_LLM_ENABLED" in message,
            "关闭态错误信息明确指出 AGENT_LLM_ENABLED",
            message[:120],
        )
        c.check(
            llm_factory.is_llm_configured() is False,
            "is_llm_configured() 在关闭态正确报告未就绪",
        )
    finally:
        settings.AGENT_LLM_ENABLED = original_enabled
        settings.LLM_API_KEY = original_key

    # 就绪分支同样环境无关：假 key + 非占位模型名只构造实例，不发起任何请求。
    fake_key = "s" + "k-" + "selftest-" + "0" * 8
    ready_model: object | None = None
    ready_raised: BaseException | None = None
    try:
        settings.AGENT_LLM_ENABLED = True
        settings.LLM_API_KEY = fake_key
        settings.LLM_MODEL = "selftest-model"
        c.check(
            llm_factory.is_llm_configured() is True,
            "is_llm_configured() 在就绪态报告已就绪（注入假 key + 非占位模型名）",
        )
        try:
            ready_model = llm_factory.get_chat_model()
        except BaseException as exc:  # noqa: BLE001
            ready_raised = exc
        c.check(
            ready_model is not None and ready_raised is None,
            "就绪态 get_chat_model 返回模型实例（构造阶段零网络请求）",
            f"实际={type(ready_raised).__name__ if ready_raised else type(ready_model).__name__}",
        )
    finally:
        settings.AGENT_LLM_ENABLED = original_enabled
        settings.LLM_API_KEY = original_key
        settings.LLM_MODEL = original_model

    # want_vision（第 15 步接通视觉）：构造成功与否取决于「视觉是否就绪」——
    # 已配置（开关 + key + 视觉模型名可用）→ 返回多模态模型实例；
    # 未配置 → AgentConfigError（构造阶段零请求）。
    # 语义随步骤演进：本段由「一律报错」提级为「按就绪位分流」（修脚本、不改产品）。
    vision_result: object | None = None
    vision_raised: BaseException | None = None
    try:
        vision_result = llm_factory.get_chat_model(want_vision=True)
    except BaseException as exc:  # noqa: BLE001
        vision_raised = exc
    if llm_factory.is_vision_configured():
        c.check(
            vision_raised is None and vision_result is not None,
            "want_vision=True 已接通视觉：已配置时返回多模态模型实例（第 15 步）",
            f"实际={type(vision_raised).__name__ if vision_raised else type(vision_result).__name__}",
        )
        c.check(
            getattr(vision_result, "model_name", None) == llm_factory.resolve_vision_model_name(),
            "视觉模型名优先取 LLM_VISION_MODEL、为空回退 LLM_MODEL",
            f"实际={getattr(vision_result, 'model_name', None)}",
        )
    else:
        c.check(
            isinstance(vision_raised, agent_errors.AgentConfigError),
            "want_vision=True 未配置时抛 AgentConfigError（构造阶段零请求）",
            f"实际={type(vision_raised).__name__ if vision_raised else 'None'}",
        )
    c.check(
        "vision_ready" in readiness and "vision_model" in readiness,
        "就绪快照含视觉三键（vision_model / vision_model_is_placeholder / vision_ready）",
        f"{sorted(readiness)}",
    )

    # LangSmith 默认关闭
    ls = langsmith.status()
    c.check(
        ls["enabled"] is False,
        "LangSmith 默认未启用（不上报任何数据）",
        f"{ls}",
    )


# ---------------------------------------------------------------------------
# 2) 冒烟图 + 脏 JSON
# ---------------------------------------------------------------------------
def check_smoke_and_json(c: Checker) -> None:
    c.section("2) 冒烟图与脏 JSON 修复")

    # 2.1 无检查点跑通，产出 Pydantic 结构化对象
    graph = S.build_smoke_graph(S.make_fake_model(), use_checkpointer=False)
    out = graph.invoke(_smoke_state())
    draft = S.SmokeDraft.model_validate(out.get("output") or {})
    c.check(
        draft.title == "三分钟看懂保温杯" and draft.score == 88,
        "冒烟图产出结构化对象 SmokeDraft",
        f"output={out.get('output')}",
    )
    c.check(
        [n["node"] for n in out.get("node_trace", [])] == ["draft", "review", "finalize"],
        "节点轨迹按 draft→review→finalize 顺序记录",
        f"{[n['node'] for n in out.get('node_trace', [])]}",
    )

    # 2.2 脏 JSON（围栏 + 裸控制字符 + 噪声）能被修复解析
    dirty = S.DIRTY_JSON_RESPONSE
    model = S.make_fake_model([dirty])
    result = invoke_json(model, [])
    c.check(
        result.payload == {"title": "三分钟看懂保温杯", "keywords": ["保温杯", "选购"], "score": 88},
        "脏 JSON（围栏/裸控制字符/噪声）被成功解析",
        f"payload={result.payload}",
    )
    c.check(result.attempts == 1, "脏 JSON 首次即命中，parse_attempts == 1", f"attempts={result.attempts}")

    # 2.3 「先坏后好」→ attempts == 2，计数如实反映
    seq = S.ScriptedChatModel(script=[S.UNPARSEABLE_RESPONSE, dirty])
    result2 = invoke_json(seq, [])
    c.check(
        result2.attempts == 2 and result2.payload.get("score") == 88,
        "先坏后好时 parse_attempts == 2（计数如实）",
        f"attempts={result2.attempts} payload={result2.payload}",
    )

    # 2.4 始终不可解析 → 耗尽 AGENT_JSON_MAX_ATTEMPTS 后抛 AgentJSONError
    bad = S.ScriptedChatModel(script=[S.UNPARSEABLE_RESPONSE])
    raised: BaseException | None = None
    try:
        invoke_json(bad, [])
    except BaseException as exc:  # noqa: BLE001
        raised = exc
    c.check(
        isinstance(raised, agent_errors.AgentJSONError),
        "始终不可解析时抛 AgentJSONError",
        f"实际={type(raised).__name__ if raised else 'None'}",
    )
    c.check(
        bad.calls == settings.AGENT_JSON_MAX_ATTEMPTS,
        f"模型被调用 AGENT_JSON_MAX_ATTEMPTS={settings.AGENT_JSON_MAX_ATTEMPTS} 次",
        f"calls={bad.calls}",
    )
    c.check(
        isinstance(raised, agent_errors.AgentRetryableError),
        "AgentJSONError 归类为可重试（供节点 RetryPolicy 再兜一层）",
    )


# ---------------------------------------------------------------------------
# 3) 检查点：幂等 / 读写回 / interrupt-resume
# ---------------------------------------------------------------------------
def check_checkpointer(c: Checker) -> None:
    c.section("3) Postgres 检查点：幂等 / 读写回 / interrupt-resume")

    c.check(not ckpt.use_memory(), "检查点后端为 postgres（离线自测亦用本地库）", settings.AGENT_CHECKPOINTER)

    saver = ckpt.init_checkpointer()
    c.check(saver is not None and type(saver).__name__ == "PostgresSaver", "PostgresSaver 初始化成功", type(saver).__name__)

    # 3.1 setup() 幂等：重复调用不报错
    idempotent = True
    detail = ""
    try:
        saver.setup()
        saver.setup()
    except BaseException as exc:  # noqa: BLE001
        idempotent = False
        detail = f"{type(exc).__name__}: {exc}"
    c.check(idempotent, "checkpointer.setup() 幂等（连续两次调用不报错）", detail)

    # 3.2 同一 thread_id 写回 / 读回
    tid = new_thread_id()
    _CREATED_THREADS.append(tid)
    graph = S.build_smoke_graph(S.make_fake_model(), checkpointer=saver)
    cfg = make_config(tid)
    first = graph.invoke(_smoke_state(thread_id=tid), cfg)
    snap = graph.get_state(cfg)
    c.check(
        snap.values.get("output") == first.get("output") and bool(snap.values.get("draft")),
        "同一 thread_id 的状态可写回并通过 get_state 读回",
        f"snapshot.output={snap.values.get('output')}",
    )

    # 3.3 interrupt -> resume 闭环
    tid2 = new_thread_id()
    _CREATED_THREADS.append(tid2)
    cfg2 = make_config(tid2)
    paused = graph.invoke(
        {**_smoke_state(require_human=True, thread_id=tid2)},
        cfg2,
    )
    interrupts = paused.get("__interrupt__") or []
    payload = getattr(interrupts[0], "value", None) if interrupts else None
    c.check(
        bool(interrupts) and isinstance(payload, dict) and "question" in payload,
        "review 节点 interrupt() 暂停并抛出待确认载荷",
        f"payload={payload}",
    )
    snap_paused = graph.get_state(cfg2)
    c.check(
        snap_paused.next == ("review",),
        "暂停后 get_state().next 指向 review（等待人工）",
        f"next={snap_paused.next}",
    )

    resumed = graph.invoke(Command(resume={"title": "人工改写后的标题", "score": 95}), cfg2)
    out = resumed.get("output") or {}
    c.check(
        out.get("title") == "人工改写后的标题" and out.get("score") == 95,
        "Command(resume=...) 恢复后跑完，人工决策生效",
        f"output={out}",
    )
    snap_done = graph.get_state(cfg2)
    c.check(snap_done.next == (), "恢复后图已结束（next 为空）", f"next={snap_done.next}")


# ---------------------------------------------------------------------------
# 4) 框架表隔离 + 业务表断言回归
# ---------------------------------------------------------------------------
def check_framework_isolation(c: Checker) -> None:
    c.section("4) 框架检查点表隔离与业务表回归")

    report = ckpt.framework_tables_report()
    c.check(report["isolated"], "框架检查点表全部落在独立 schema", f"{report}")
    c.check(
        not report["any_in_public"],
        "public schema 内没有任何框架检查点表",
        f"schemas_seen={report['schemas_seen']}",
    )
    names = {t["table"] for t in report["tables"]}
    c.check(
        names == set(ckpt.FRAMEWORK_CHECKPOINT_TABLES),
        f"框架托管表清单齐全（{len(ckpt.FRAMEWORK_CHECKPOINT_TABLES)} 张）",
        f"实际={sorted(names)}",
    )

    with engine.connect() as conn:
        public_business = conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_type='BASE TABLE' "
                "AND table_name <> 'alembic_version'"
            )
        ).scalars().all()
    c.check(
        len(public_business) == 10,
        "public schema 仍恰好 10 张业务表",
        f"实际 {len(public_business)} 张：{sorted(public_business)}",
    )

    # 业务 metadata 未被污染
    from app.db.base import Base  # noqa: PLC0415

    c.check(
        len(Base.metadata.tables) == 10,
        "SQLAlchemy metadata 仍恰好 10 张表（框架表未混入）",
        f"实际={sorted(Base.metadata.tables)}",
    )

    # 回归：第 7 步两个脚本仍全绿
    py = sys.executable
    for script_name, label in (
        ("selftest_db.py", "第 7 步 selftest_db.py 仍全绿"),
        ("verify_schema.py", "第 7 步 verify_schema.py 仍全绿"),
    ):
        proc = subprocess.run(  # noqa: S603
            [py, str(ROOT_DIR / "scripts" / script_name)],
            capture_output=True,
            text=True,
            cwd=str(ROOT_DIR),
        )
        tail = (proc.stdout or "").strip().splitlines()[-3:]
        c.check(proc.returncode == 0, label, f"exit={proc.returncode} | {' / '.join(tail)}")


# ---------------------------------------------------------------------------
# 5) 重试语义
# ---------------------------------------------------------------------------
def check_retry_semantics(c: Checker) -> None:
    c.section("5) 重试语义：可重试 vs 不可重试")

    max_retries = settings.AGENT_NODE_MAX_RETRIES
    max_attempts = max_retries + 1  # LangGraph 的 max_attempts = 1 + 重试次数

    # 5.1 持续 429 → 恰好重试到 max_attempts 后失败
    retryable = S.ScriptedChatModel(script=[agent_errors.AgentRetryableError("429 simulated")])
    raised: BaseException | None = None
    try:
        S.build_smoke_graph(retryable, use_checkpointer=False).invoke(_smoke_state())
    except BaseException as exc:  # noqa: BLE001
        raised = exc
    c.check(
        retryable.calls == max_attempts,
        f"可重试错误共尝试 {max_attempts} 次（1 次原始 + {max_retries} 次重试 = AGENT_NODE_MAX_RETRIES+1）",
        f"calls={retryable.calls}",
    )
    c.check(
        isinstance(raised, agent_errors.AgentRetryableError),
        "可重试错误耗尽后如实抛出",
        f"实际={type(raised).__name__ if raised else 'None'}",
    )

    # 5.2 不可重试（4xx/鉴权）→ 只试 1 次立即失败
    non_retryable = S.ScriptedChatModel(script=[agent_errors.AgentNonRetryableError("401 simulated")])
    raised2: BaseException | None = None
    try:
        S.build_smoke_graph(non_retryable, use_checkpointer=False).invoke(_smoke_state())
    except BaseException as exc:  # noqa: BLE001
        raised2 = exc
    c.check(
        non_retryable.calls == 1,
        "不可重试错误只尝试 1 次（立即失败）",
        f"calls={non_retryable.calls}",
    )
    c.check(
        isinstance(raised2, agent_errors.AgentNonRetryableError),
        "不可重试错误如实抛出",
        f"实际={type(raised2).__name__ if raised2 else 'None'}",
    )

    # 5.3 `classify_exception` 对真实 openai 异常的分类
    import httpx  # noqa: PLC0415
    import openai  # noqa: PLC0415

    req = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    cases = [
        (openai.RateLimitError("429", response=httpx.Response(429, request=req), body=None), agent_errors.AgentRetryableError, "429 -> 可重试"),
        (openai.InternalServerError("500", response=httpx.Response(500, request=req), body=None), agent_errors.AgentRetryableError, "500 -> 可重试"),
        (openai.AuthenticationError("401", response=httpx.Response(401, request=req), body=None), agent_errors.AgentNonRetryableError, "401 -> 不可重试"),
        (openai.BadRequestError("400", response=httpx.Response(400, request=req), body=None), agent_errors.AgentNonRetryableError, "400 -> 不可重试"),
    ]
    for exc, expected, label in cases:
        got = agent_errors.classify_exception(exc)
        c.check(isinstance(got, expected), f"异常分类正确：{label}", f"实际={type(got).__name__}")

    # 5.4 一次失败后恢复
    flaky = S.ScriptedChatModel(script=[agent_errors.AgentRetryableError("429 once"), S.DIRTY_JSON_RESPONSE])
    out = S.build_smoke_graph(flaky, use_checkpointer=False).invoke(_smoke_state())
    c.check(
        flaky.calls == 2 and (out.get("output") or {}).get("score") == 88,
        "一次瞬时失败后重试即成功",
        f"calls={flaky.calls}",
    )

    # 5.5 重试语义换算自洽（重试次数 → 总尝试次数）
    sem = describe_retry_semantics()
    c.check(
        sem["node_max_attempts"] == sem["node_max_retries"] + 1
        and sem["worst_case_model_calls"] == settings.AGENT_JSON_MAX_ATTEMPTS * sem["node_max_attempts"],
        "重试语义换算自洽（max_attempts = 1 + 重试次数；最坏调用数 = JSON 层 × 节点层）",
        f"{sem}",
    )


# ---------------------------------------------------------------------------
# 6) 审计与脱敏
# ---------------------------------------------------------------------------
def check_recorder(c: Checker) -> None:
    c.section("6) 运行审计（agent_runs）与密钥脱敏")

    fake_key = "sk-FAKE0123456789abcdefghij"
    tid = new_thread_id()
    _CREATED_THREADS.append(tid)

    model = S.make_usage_fake_model(S.DIRTY_JSON_RESPONSE, {"input_tokens": 11, "output_tokens": 22, "total_tokens": 33})
    result = run_agent(
        S.SMOKE_KIND,
        S.smoke_build_fn(model),
        inputs={"kind": S.SMOKE_KIND, "note": f"这里混入一个假密钥 {fake_key}", "require_human": False},
        thread_id=tid,
        run_type="other",
    )
    c.check(result["status"] == "success", "run_agent 返回 success", f"status={result['status']}")
    c.check(
        (result["output"] or {}).get("score") == 88,
        "run_agent 透传结构化产出",
        f"output={result['output']}",
    )

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT id, run_type, status, inputs, outputs, llm_model, llm_base_url, "
                "prompt_tokens, completion_tokens, total_tokens, json_parse_attempts, "
                "latency_ms, error_message "
                "FROM agent_runs WHERE id = :rid"
            ),
            {"rid": result["run_id"]},
        ).mappings().first()

    c.check(row is not None, "agent_runs 已写入审计行")
    if row is None:
        return

    blob = str(dict(row))
    c.check(fake_key not in blob, "审计行内不含注入的 sk- 密钥", blob[:160])
    c.check("sk-" not in blob, "审计行内不存在任何 sk- 形态字符串")
    c.check(REDACTED in blob, "被脱敏处显示 [REDACTED]")
    c.check(row["run_type"] == "other", "run_type 落 other（冒烟运行）", str(row["run_type"]))
    c.check(row["status"] == "success", "status 落 success", str(row["status"]))
    c.check(
        row["prompt_tokens"] == 11 and row["completion_tokens"] == 22 and row["total_tokens"] == 33,
        "token 三字段回填正确",
        f"{row['prompt_tokens']}/{row['completion_tokens']}/{row['total_tokens']}",
    )
    c.check(row["json_parse_attempts"] == 1, "json_parse_attempts 回填为 1", str(row["json_parse_attempts"]))
    c.check(bool(row["latency_ms"] is not None and row["latency_ms"] >= 0), "latency_ms 已记录", str(row["latency_ms"]))
    c.check(row["llm_base_url"] == settings.LLM_BASE_URL, "llm_base_url 如实记录（不含 key）", str(row["llm_base_url"]))
    node_trace = (row["outputs"] or {}).get("node_trace")
    c.check(
        isinstance(node_trace, list) and [n.get("node") for n in node_trace] == ["draft", "review", "finalize"],
        "outputs.node_trace 记录三个节点",
        f"{node_trace}",
    )

    # 失败路径：异常信息里混入密钥也必须被抹掉
    tid2 = new_thread_id()
    _CREATED_THREADS.append(tid2)
    boom = S.ScriptedChatModel(script=[agent_errors.AgentNonRetryableError(f"401 unauthorized key={fake_key}")])
    raised: BaseException | None = None
    try:
        run_agent(
            S.SMOKE_KIND,
            S.smoke_build_fn(boom),
            inputs={"kind": S.SMOKE_KIND, "require_human": False},
            thread_id=tid2,
            run_type="other",
        )
    except BaseException as exc:  # noqa: BLE001
        raised = exc
    c.check(raised is not None, "运行失败时异常继续向上抛（不吞异常）")

    with engine.connect() as conn:
        failed = conn.execute(
            text(
                "SELECT status, error_message FROM agent_runs "
                "WHERE inputs->>'kind' = :k ORDER BY created_at DESC LIMIT 1"
            ),
            {"k": S.SMOKE_KIND},
        ).mappings().first()
    c.check(
        failed is not None and failed["status"] == "failed",
        "失败运行落 failed",
        f"{dict(failed) if failed else None}",
    )
    err_msg = (failed or {}).get("error_message") or ""
    c.check(fake_key not in err_msg and "sk-" not in err_msg, "失败行的 error_message 已脱敏", err_msg[:160])


# ---------------------------------------------------------------------------
# 收尾清理
# ---------------------------------------------------------------------------
def cleanup(c: Checker) -> None:
    c.section("收尾：清理本次产生的测试数据")

    saver = ckpt.get_checkpointer()
    removed_threads = 0
    for tid in _CREATED_THREADS:
        try:
            saver.delete_thread(tid)
            removed_threads += 1
        except Exception:  # noqa: BLE001 —— 清理失败不影响结论，但会在残留复查中暴露
            pass
    c.check(removed_threads == len(_CREATED_THREADS), f"检查点线程已清理（{removed_threads}/{len(_CREATED_THREADS)}）")

    with engine.begin() as conn:
        deleted = conn.execute(
            text("DELETE FROM agent_runs WHERE inputs->>'kind' = :k"),
            {"k": S.SMOKE_KIND},
        ).rowcount
    print(f"       已删除 agent_runs 冒烟行：{deleted}")

    with engine.connect() as conn:
        residual = conn.execute(
            text("SELECT count(*) FROM agent_runs WHERE inputs->>'kind' = :k"),
            {"k": S.SMOKE_KIND},
        ).scalar()
        residual_ckpt = conn.execute(
            text("SELECT count(*) FROM langgraph.checkpoints WHERE thread_id = ANY(:tids)"),
            {"tids": _CREATED_THREADS or [""]},
        ).scalar()
        # 顺带报告框架表总量（仅信息，不作断言：后续步骤产生真实运行后会有正常数据）
        totals = {
            t: conn.execute(text(f"SELECT count(*) FROM langgraph.{t}")).scalar()
            for t in ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
        }
    c.check(residual == 0, "agent_runs 冒烟残留为 0", f"残留={residual}")
    c.check(residual_ckpt == 0, "检查点冒烟残留为 0", f"残留={residual_ckpt}")
    print(f"       [INFO] 框架检查点表现有总行数（仅信息）：{totals}")


def main() -> int:
    c = Checker()
    print("Agent Harness 离线自检（不联网、不需要真实 key）")
    print(f"检查点后端：{settings.AGENT_CHECKPOINTER} | recursion_limit={settings.AGENT_RECURSION_LIMIT} | "
          f"node_max_retries={settings.AGENT_NODE_MAX_RETRIES} | json_max_attempts={settings.AGENT_JSON_MAX_ATTEMPTS}")

    try:
        check_llm_gate(c)
        check_smoke_and_json(c)
        check_checkpointer(c)
        check_framework_isolation(c)
        check_retry_semantics(c)
        check_recorder(c)
    finally:
        cleanup(c)
        ckpt.close_checkpointer()

    print()
    print(f"通过 {c.passed} 项，失败 {len(c.failures)} 项。")
    if c.failures:
        for f in c.failures:
            print(f"  - FAIL: {f}")
        return 1
    print("全部 PASS ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
