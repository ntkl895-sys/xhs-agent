"""FastAPI 应用入口。

第 6 步：健康检查骨架（``/health``、``/health/ready``）。
第 8 步：新增 ``/api/v1/accounts``、``/api/v1/personas`` 两组 CRUD 路由，
         并统一异常渲染（业务错误 / 字段校验 / 框架 HTTP 异常 / 兜底 500）。
Agent Harness 步：新增 ``lifespan``，负责 LangGraph 检查点连接池与
         （可选的）LangSmith 观测的启停。
第 9 步：新增 ``/api/v1/hotspots`` 路由（列表 / sources / refresh / runs），
         并把 **APScheduler 热搜定时采集** 的启停**并入同一个 lifespan**。
第 10 步：新增 ``/api/v1/topics`` 路由（generate / 列表 / runs / 详情 / 微调），
         在 LangGraph harness 上实现第一个业务图（选题 Agent）。
第 12 步：新增 ``/api/v1/notes`` 路由（generate / 列表 / runs / 详情），
         在 LangGraph harness 上实现第二个业务图（创作 Agent：标题 / 正文 / 标签
         + 违禁词合规自检）。
第 13 步：新增 ``/api/v1/covers`` 路由（列表 / 新建 / 更新 + 互斥选中），
         即「封面工作室」的候选数据存取；封面由前端程序化渲染导出，
         **零 LLM、零迁移、无第二个 lifespan**。
第 15 步：新增 ``/api/v1/metrics`` 路由（时间线 / 快照落库 / 截图识别），
         即「数据回填」；截图识别在 LangGraph harness 上实现第三个业务图
         （视觉模型，``RunType.METRIC_RECOGNIZE``），识别结果**必须人工核对**
         后才落库；零迁移。
第 16 步：新增 ``/api/v1/diagnostics`` 路由（generate / 诊断历史 / 详情），
         即「单篇诊断」；诊断在 LangGraph harness 上实现第四个业务图
         （``RunType.DIAGNOSIS``，只读 + 建议，结论落 ``diagnostics``）；
         零迁移。
第 17 步：新增 ``/api/v1/dashboard``（运营看板：确定性只读聚合，零 LLM）与
         ``/api/v1/reports``（周报：第五个业务图 ``RunType.WEEKLY_REPORT``，
         只读 + 建议，结论写入 ``agent_runs.outputs``）两组路由，并把
         **APScheduler 周报定时**（默认关闭）合并进同一个 lifespan；零迁移。
第 18 步：新增 ``/api/v1/knowledge`` 路由（知识库：列表 / 规律提炼 / 检索测试 /
         归档；规律提炼为第六个业务图 ``RunType.INSIGHT``，向量化走本地 bge
         1024 维 ⇒ 零迁移），并在选题图 / 创作图的 load_context 最小接入
         RAG 反哺（失败只降级不阻断）。

启动：
    cd backend
    uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.health import router as health_router
from app.api.routes.accounts import router as accounts_router
from app.api.routes.content_variants import router as content_variants_router
from app.api.routes.covers import router as covers_router
from app.api.routes.dashboard import router as dashboard_router
from app.api.routes.diagnostics import router as diagnostics_router
from app.api.routes.hotspots import router as hotspots_router
from app.api.routes.knowledge import router as knowledge_router
from app.api.routes.metrics import router as metrics_router
from app.api.routes.notes import router as notes_router
from app.api.routes.personas import router as personas_router
from app.api.routes.reports import router as reports_router
from app.api.routes.search import router as search_router
from app.api.routes.topics import router as topics_router
from app.core.config import settings
from app.core.errors import AppError, code_for_status, format_validation_errors

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("xhs-agent")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期。

    ⚠️ 本项目**只允许有一个 lifespan**。各步骤的启动/关闭钩子都必须**合并进本函数**，
    严禁再定义第二个 ``lifespan`` 互相覆盖。

    当前职责：

    1. 按配置决定是否启用 LangSmith 观测（默认关闭，不上报）；
    2. 初始化 LangGraph 检查点（Postgres：建池 + 建 schema + 幂等 ``setup()``）；
    3. 启动 **APScheduler 热搜定时采集**（第 9 步；启用开关见 ``HOTSPOT_SCHEDULER_ENABLED``）；
    4. 启动 **APScheduler 周报定时**（第 17 步；默认关闭，开关见
       ``WEEKLY_SCHEDULER_ENABLED``）；
    5. 关闭时依次停止两个调度器、释放检查点连接池。

    任一初始化失败**不会**让进程起不来（例如数据库不可达、调度器异常）：
    记 WARNING 后继续启动——健康检查与 CRUD 不受影响。
    """
    # --- 启动 ---
    from app.agent.checkpoint import init_checkpointer
    from app.agent.tracing import langsmith
    from app.services.hotspot.scheduler import start_scheduler
    from app.services.report.scheduler import start_weekly_scheduler

    langsmith.configure_from_settings()
    try:
        init_checkpointer()
        logger.info("LangGraph 检查点初始化完成（后端=%s）", settings.AGENT_CHECKPOINTER)
    except Exception as exc:  # noqa: BLE001 —— 不让中间件故障拖垮整个进程
        logger.warning(
            "LangGraph 检查点初始化失败（应用继续启动；调用图时会重试）：%s", exc
        )

    try:
        # 与检查点共存于同一 lifespan；内部按 HOTSPOT_SCHEDULER_ENABLED 决定是否注册作业
        start_scheduler()
    except Exception as exc:  # noqa: BLE001 —— 调度器故障同样不阻断服务启动
        logger.warning("热搜调度器启动失败（应用继续启动）：%s", exc)

    try:
        # 周报定时（默认关闭）；与热搜调度器同在唯一 lifespan 内启停
        start_weekly_scheduler()
    except Exception as exc:  # noqa: BLE001
        logger.warning("周报调度器启动失败（应用继续启动）：%s", exc)

    try:
        yield
    finally:
        # --- 关闭 ---
        from app.agent.checkpoint import close_checkpointer
        from app.services.hotspot.scheduler import shutdown_scheduler
        from app.services.report.scheduler import shutdown_weekly_scheduler

        try:
            shutdown_scheduler()
        except Exception as exc:  # noqa: BLE001
            logger.warning("热搜调度器关闭异常：%s", exc)
        try:
            shutdown_weekly_scheduler()
        except Exception as exc:  # noqa: BLE001
            logger.warning("周报调度器关闭异常：%s", exc)
        close_checkpointer()
        logger.info("LangGraph 检查点连接池已关闭")


app = FastAPI(
    title="小红书自媒体运营 Agent · 后端",
    version=settings.APP_VERSION,
    description=(
        "账号 / 人设 CRUD、热搜采集服务、选题 Agent、内容创作 Agent、封面候选、"
        "数据回填（快照 + 截图识别）、单篇诊断、运营看板、周报（/api/v1）"
        "+ Agent Harness 基座。健康检查不带 v1 前缀。"
    ),
    lifespan=lifespan,
)

# CORS：仅放行本地前端开发地址（默认 http://localhost:3000）
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 健康检查（不带 v1 前缀，保持第 6 步原样）
app.include_router(health_router)
# 业务路由（统一 /api/v1 前缀）
app.include_router(accounts_router)
app.include_router(personas_router)
app.include_router(hotspots_router)
app.include_router(topics_router)
app.include_router(notes_router)
app.include_router(covers_router)
app.include_router(metrics_router)
app.include_router(diagnostics_router)
app.include_router(dashboard_router)
app.include_router(reports_router)
app.include_router(knowledge_router)
app.include_router(search_router)
app.include_router(content_variants_router)


# ---------------------------------------------------------------------------
# 统一错误渲染：全部返回 {status, code, message, details}
# ---------------------------------------------------------------------------
@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """业务异常（404 / 409 / 422 / 400）统一渲染。"""
    return JSONResponse(status_code=exc.status_code, content=exc.to_body())


@app.exception_handler(RequestValidationError)
async def request_validation_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """请求体 / 查询参数校验失败 -> 422，``details`` 带字段级错误。"""
    return JSONResponse(
        status_code=422,
        content={
            "status": "error",
            "code": "validation",
            "message": "请求参数校验失败",
            "details": format_validation_errors(exc.errors()),
        },
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """框架自身抛出的 HTTPException（如 404 / 405）也统一结构。"""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "status": "error",
            "code": code_for_status(exc.status_code),
            "message": str(exc.detail),
            "details": None,
        },
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """全局兜底异常处理。

    保证任何未处理异常都返回【结构化 JSON】而不是裸 500 堆栈，
    前端可据此展示友好提示。
    """
    logger.exception("未处理异常：%s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "status": "error",
            "code": "internal_error",
            "service": "backend",
            "error": type(exc).__name__,
            "message": "服务器内部错误，请查看后端日志。",
            "path": request.url.path,
        },
    )
