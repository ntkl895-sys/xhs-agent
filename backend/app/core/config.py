"""应用配置。

使用 pydantic-settings 从环境变量 / .env 读取配置，所有字段都带本地开发默认值，
即便没有任何环境变量也能直接启动。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ 目录（app/core/config.py -> 上溯三层）
BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """应用配置项。

    优先级：真实环境变量 > backend/.env > 下面的默认值。
    """

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # 忽略无关变量（如根 .env 的 POSTGRES_USER 等）
    )

    # ------------------------------------------------------------------ 基础
    APP_ENV: str = "local"
    APP_NAME: str = "xhs-agent-backend"
    APP_VERSION: str = "0.1.0"

    # ------------------------------------------------------------- PostgreSQL
    # SQLAlchemy 2 + psycopg3 驱动。
    # 主机固定用 127.0.0.1（而非 localhost）：Win 上 localhost 可能先走 IPv6(::1)
    # 再回退 IPv4，双栈各超时一次；docker-compose 端口也只绑定 127.0.0.1，二者一致。
    DATABASE_URL: str = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/xhs_agent"
    # 连接超时（秒）。必须显式设置：psycopg 默认不设超时，数据库不可达时
    # 连接可能长时间挂起，导致 /health/ready 迟迟不返回。
    DB_CONNECT_TIMEOUT: int = 3

    # ------------------------------------------------------------------ Redis
    REDIS_URL: str = "redis://127.0.0.1:6379/0"
    # 连接 / 读写超时（秒）。同理，避免 Redis 不可达时健康探针挂起。
    REDIS_CONNECT_TIMEOUT: int = 3

    # ------------------------------------------------------------------- CORS
    # 多个来源用英文逗号分隔
    CORS_ORIGINS: str = "http://localhost:3000"

    # ------------------------------------------------------------------- 大模型
    # 一律走 OpenAI 兼容（langchain_openai.ChatOpenAI 指向 DeepSeek）。
    # 模型 ID 保持中文占位，以 DeepSeek 控制台当前列表为准：
    #   https://api-docs.deepseek.com/zh-cn/quick_start/pricing
    # 真实密钥只填在本地 .env，绝不出现在代码 / 日志 / agent_runs / .example 中。
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://api.deepseek.com"
    LLM_MODEL: str = "把这里替换成DeepSeek控制台当前模型名"

    # ------------------------------------------------------------- Agent Harness
    # LangGraph 编排参数（第 Agent Harness 步）。
    # 说明：LangGraph 新版 recursion_limit 默认 1000，本项目仍显式配置以便可预期。
    AGENT_RECURSION_LIMIT: int = 40
    # 单个「调用 LLM 的节点」最大重试**次数**（不含首次尝试）。
    # 仅对 429/5xx/网络异常/JSON 解析失败重试；4xx/鉴权/参数错误立即失败。
    # 换算：LangGraph 的 max_attempts = 本值 + 1（默认 2 次重试 → 最多 3 次总尝试）。
    AGENT_NODE_MAX_RETRIES: int = 2
    # 结构化输出（JSON）最多尝试次数：1 次原始 + 若干次纠正重问
    AGENT_JSON_MAX_ATTEMPTS: int = 3
    # 单节点墙钟超时（秒），用于监控/告警，不中断 HTTP 客户端自身的超时
    AGENT_NODE_TIMEOUT_SECONDS: int = 60
    # 检查点后端：postgres（默认，生产）/ memory（仅离线自测）
    AGENT_CHECKPOINTER: str = "postgres"
    # 总开关：false 时，除显式真实检查脚本外，任何入口都不得发起对外 LLM 网络调用
    AGENT_LLM_ENABLED: bool = False

    # LangSmith 为可选 SaaS 观测平台，默认关闭；开启意味着数据外发与费用由使用者自担。
    AGENT_LANGSMITH_TRACING: bool = False
    AGENT_LANGSMITH_API_KEY: str = ""
    AGENT_LANGSMITH_PROJECT: str = "xhs-agent"

    # --------------------------------------------------------------- 热搜采集
    # 第 9 步（热搜服务化）：经 DailyHotApi 采集公开榜单，落 hotspots 表。
    # 【合规红线】不请求任何小红书域名、不写任何登录态凭据；该来源一律禁用。
    #
    # 说明（第 1 步实测结论，2026-10-09，本地 dailyhot-api 2.0.8）：
    #   - 成功 key：zhihu / douyin / bilibili / toutiao / qq-news；
    #   - bili / newsqq 为**错误** key（正确写法是 bilibili / qq-news）；
    #   - weibo 上游曾持续返 500、baidu 曾返回无 title 的脏数据，故默认禁用；
    #   - 小红书（xhs）无对应路由且公开热搜需登录态 —— 合规上不可采集，默认禁用。
    # 第 23 步（2026-10-15）实测补充：本地镜像 /all 共 56 路由，含 github（GitHub 趋势日榜，
    #   hot 为千分位字符串、含 owner/repo/language/stars 字段）——按要求同级启用。
    HOTSPOT_API_BASE_URL: str = "http://127.0.0.1:6688"
    # 启用的源标识（英文逗号分隔）。**不得包含小红书平台自身来源**。
    HOTSPOT_ENABLED_SOURCES: str = "zhihu,douyin,bilibili,toutiao,qq-news,github"
    # 每源最多保留条数（服务端传 limit，本地再截断兜底）
    HOTSPOT_PER_SOURCE_LIMIT: int = 20
    # 是否强行绕过 DailyHotApi 缓存（默认 false，尊重上游约 60 分钟缓存；
    # true 时请求额外携带 cache=false —— 实测唯一生效的强刷写法）
    HOTSPOT_FORCE_REFRESH: bool = False
    # false：raw_extra 仅保留最小集（id / mobileUrl / timestamp），
    #        cover_url / summary / author 三列留 NULL；
    # true ：额外保留 desc / cover / author 并回填对应列。
    HOTSPOT_KEEP_EXTRA_FIELDS: bool = False
    # 单请求超时（秒）
    HOTSPOT_TIMEOUT_SECONDS: int = 10
    # 单源最多尝试次数（仅对 429 / 5xx / 网络异常指数退避；4xx 立即失败不重试）
    HOTSPOT_MAX_ATTEMPTS: int = 3
    # 定时采集开关 / 间隔（分钟）/ 启动时是否立刻抓一次
    HOTSPOT_SCHEDULER_ENABLED: bool = True
    HOTSPOT_FETCH_INTERVAL_MINUTES: int = 60
    HOTSPOT_FETCH_ON_STARTUP: bool = False

    # ---------------------------------------------------------- 选题 Agent（第 10 步）
    # 在 LangGraph harness 上实现的第一个业务图：单热点 → K 个选题。
    # 本步**不接真实 LLM**（离线自检注入假模型）；真实调用走 harness 的
    # get_chat_model(want_json=True)，受 AGENT_LLM_ENABLED / LLM_API_KEY 闸门约束。
    TOPIC_PROMPT_VERSION: str = "v1"
    # 单个热点默认生成 / 落库的选题条数
    TOPIC_DEFAULT_PER_HOTSPOT: int = 3
    # 未显式指定 hotspot_ids 时，默认取的热点个数
    TOPIC_DEFAULT_HOTSPOT_LIMIT: int = 3
    # 单次请求最多处理的热点个数（硬上限，超限即截断）
    TOPIC_MAX_HOTSPOTS_PER_REQUEST: int = 10
    # 批量处理热点的并发度。默认 1 = **串行**（不引入多线程 / asyncio）
    TOPIC_CONCURRENCY: int = 1
    # 标题最大长度（超出即判不合格）
    TOPIC_TITLE_MAX_LEN: int = 20
    # 话题标签个数区间（纯词，不含 #）
    TOPIC_MIN_TAGS: int = 3
    TOPIC_MAX_TAGS: int = 8
    # 内容大纲条数区间
    TOPIC_MIN_OUTLINE: int = 3
    TOPIC_MAX_OUTLINE: int = 6
    # 节点超时复用 AGENT_NODE_TIMEOUT_SECONDS，不另设
    # ---- 可选：LLM 自主选工具（tool calling，默认关闭）----
    # 关闭时选题图节点/条件边/提示词与接入前完全一致（零行为差异）。
    # 打开后模型可在生成选题前自主调用**只读**工具（热点检索 / 知识检索 /
    # 历史选题去重 / 人设细节）；工具白名单写死在代码里，V1 不开放任何写操作。
    TOPIC_TOOLCALLING_ENABLED: bool = False
    # 工具执行轮数硬上限（超过即收敛到生成节点，不再执行工具）
    TOPIC_TOOLCALLING_MAX_ROUNDS: int = 3

    # ------------------------------------------------------ 内容创作 Agent（第 12 步）
    # 在 LangGraph harness 上实现的第二个业务图：单选题 → 一篇笔记草稿 + 违禁词合规自检。
    # 违禁词「基础风险词表」内置在代码里（app/agent/content/forbidden_words.py），
    # **不写 env、不写日志**；第 19 步接入 knowledge_snippets 后统一由知识库管理。
    CONTENT_PROMPT_VERSION: str = "v1"
    # 标题最大长度（超出即判字段硬伤；与 notes.title 的 String(100) 对齐）
    NOTE_TITLE_MAX_LEN: int = 100
    # 正文长度下限（低于即判字段硬伤）
    NOTE_BODY_MIN_LEN: int = 200
    # 正文目标长度上限（写入提示词的写作目标；**不作为字段闸门**，口径见 docs/agent_content.md）
    NOTE_BODY_MAX_LEN: int = 1000
    # 话题标签个数区间（纯词，不含 #）
    NOTE_MIN_TAGS: int = 3
    NOTE_MAX_TAGS: int = 10

    # ------------------------------- 多版本创作 Agent（第 23 步 · V2 增强（二））
    # 在 LangGraph harness 上实现的第七个业务图：单选题 → N 个角度明显不同、
    # 可横向对比、互斥采用的图文版本（标题 / 正文 / 标签）。
    # N 的允许区间由业务代码常量约束（content_variants/contracts.py，1~5、默认 3）；
    # 版本角度池（实用干货 / 情绪共鸣 / 反差观点 / 亲身经历 / 清单盘点 / 避坑提醒）
    # 内置在代码里（content_variants/prompts.py），**不写 env**。
    CONTENT_VARIANTS_PROMPT_VERSION: str = "v1"

    # ---------------------------------------------------- 数据回填（第 15 步）
    # 截图识别（视觉模型）+ 人工录入，统一沉淀为 note_metrics 快照。
    # 【隐私 / 红线】截图仅由用户本人上传、只在本机流转与识别；不保存任何账号
    # 与登录态凭据；不向任何小红书域名发起请求；识别结果必须经人工确认后才落库
    # （POST /metrics）。
    METRIC_PROMPT_VERSION: str = "v1"
    # 视觉模型名：优先于 LLM_MODEL；为空时回退 LLM_MODEL。
    # ⚠️ 一律以 DeepSeek 控制台当前模型列表为准（当前为 deepseek-flash，可能变化），
    # 代码里不硬编码任何模型名：
    #   https://api-docs.deepseek.com/zh-cn/guides/vision
    LLM_VISION_MODEL: str = ""
    # 是否把上传截图保存到本地磁盘（默认 false：识别阶段图片只在内存 / 请求内流转，
    # 不落盘；只有显式开启且用户确认保存时才会写入 SCREENSHOT_DIR）。
    SAVE_SCREENSHOT: bool = False
    # 截图保存目录（相对 backend/ 目录解析；该目录已加入 .gitignore）
    SCREENSHOT_DIR: str = "data/screenshots"
    # 单图字节上限（默认 4 MiB）。超限不静默截断，直接给明确中文报错。
    # 内联（data URL）大小上限以 DeepSeek 官方视觉文档为准，可用本项调整：
    #   https://api-docs.deepseek.com/zh-cn/guides/vision
    METRIC_MAX_IMAGE_BYTES: int = 4 * 1024 * 1024
    # 允许的图片 MIME 类型（英文逗号分隔；仅这三类）
    METRIC_ALLOWED_IMAGE_TYPES: str = "image/jpeg,image/png,image/webp"

    # ---------------------------------------------------- 单篇诊断（第 16 步）
    # 在 LangGraph harness 上实现的第四个业务图：单篇笔记 → 一次诊断结论。
    # 【红线】诊断 = 只读 + 建议：不修改 notes / topics / note_metrics；不引入任何
    # 外部基准数字（行业均值 / 爆款阈值一律禁止，跨篇基准属第 18 步 RAG）；
    # 数据不足必须如实声明（data_gaps），严重不足时 score 为 null（不硬打分）。
    DIAG_PROMPT_VERSION: str = "v1"
    # 异常波动判定阈值：相邻快照中 views / likes 的降幅达到该比例才判 anomaly。
    # 经验参考值、非平台官方口径，可自行调整。
    DIAG_ANOMALY_DROP_RATIO: float = 0.30
    # 数据缺口收集时的快照条数下限（低于该值会在 data_gaps 中声明）
    DIAG_MIN_SNAPSHOTS: int = 1
    # 四个文本数组（strengths / weaknesses / reasons / suggestions）条数上限
    DIAG_MAX_LIST_ITEMS: int = 8
    # 四个文本数组单条最大字数（超出本地截断）
    DIAG_TEXT_MAX_LEN: int = 300
    # score 合法区间（越界视为硬伤回炉；数据严重不足时必须为 null）
    DIAG_SCORE_MIN: float = 0
    DIAG_SCORE_MAX: float = 100

    # ---------------------------------------------------- 运营看板 + 周报（第 17 步）
    # 看板 = 确定性只读聚合（零 LLM、零网络）；周报 = 第五个业务图
    # （只读 + 建议；结论**不落业务表**，整体写入 agent_runs.outputs）。
    # 【红线】周报严禁编造外部行业数字（行业均值 / 爆款阈值一律禁止，
    # 跨篇基准属第 18 步 RAG）；数据不足必须写 data_gaps；全链路不改任何业务表。
    WEEKLY_PROMPT_VERSION: str = "v1"
    # 周报默认周期（近 N 天，含今天；1 ~ 90 天内由代码校验）
    WEEKLY_DEFAULT_DAYS: int = 7
    # 周报定时（默认关闭；开启后并入 app/main.py 的唯一 lifespan）
    WEEKLY_SCHEDULER_ENABLED: bool = False
    # 周报定时 cron（默认周一 09:00，本地时区；仅在上项为 true 时生效）
    WEEKLY_SCHEDULE_CRON: str = "0 9 * * 1"
    # 趋势图点数上限（区间过长时自动按等间隔收紧桶步长；经验参考值）
    DASHBOARD_TREND_MAX_POINTS: int = 30
    # 排行默认返回条数
    DASHBOARD_RANKING_TOP_N: int = 10
    # 看板默认时间窗口（近 N 个自然日）
    DASHBOARD_DEFAULT_RANGE_DAYS: int = 30

    # ---------------------------------------------------- RAG / embedding（第 18 步）
    # 本地 bge 向量化 + 爆款规律提炼（第六个业务图）+ 选题 / 创作反哺。
    # 【红线】规律只来自**本账号本地数据**（自己的已发布笔记 + note_metrics +
    # diagnostics）：不爬竞品、不引入行业均值 / 爆款门槛等外部数字；跨篇行业
    # 基准不在本步做。
    # 【维度】knowledge_snippets.embedding 建表即为 vector(1024)（第 7 步占位）；
    # bge-large-zh-v1.5 输出 1024 维 ⇒ **零迁移**。换非 1024 维模型需新建迁移
    # 改列维度并重建 HNSW 索引（本步不做；不一致时由代码给清晰中文错误）。
    # 【模型下载】首次真实向量化时才加载 / 下载模型（懒加载，不拖慢启动与健康检查）；
    # 国内可设环境变量 HF_ENDPOINT=https://hf-mirror.com 走镜像。
    EMBEDDING_PROVIDER: str = "local"
    EMBEDDING_MODEL: str = "BAAI/bge-large-zh-v1.5"
    EMBEDDING_DIM: int = 1024
    EMBEDDING_DEVICE: str = "cpu"
    EMBEDDING_BATCH_SIZE: int = 16
    EMBEDDING_TOP_K: int = 5
    # openai 兼容 embedding（仅 EMBEDDING_PROVIDER=openai 时启用；占位，默认空）
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_BASE_URL: str = ""
    EMBEDDING_OPENAI_MODEL: str = ""
    # ---- 检索策略（第 24 步 · 混合检索；默认行为与旧版完全一致）----
    # vector = 纯向量余弦（默认，与历史行为字节级一致）；
    # hybrid = 向量 + pg_trgm 关键字两路，RRF 融合。缺 pg_trgm 扩展时自动降级为 vector。
    RAG_RETRIEVAL_MODE: str = "vector"
    # 两路各自召回的候选池大小（融合前）；必须 ≥ 最终 top_k，否则融合没有意义
    RAG_HYBRID_POOL: int = 20
    # RRF 平滑常数（论文经验值 60；越小头部优势越大）
    RAG_RRF_K: int = 60
    # ---- 爆款规律提炼（第六个业务图）----
    INSIGHTS_PROMPT_VERSION: str = "v1"
    # 样本门槛：published 笔记数低于该值不做提炼（不硬造规律，run 记 partial）
    INSIGHTS_MIN_PUBLISHED: int = 3
    # 单次提炼的规律条数上限（超出本地截断）
    INSIGHTS_PATTERN_MAX: int = 15
    # 单条规律文本最大字数（超出本地截断）
    INSIGHTS_TEXT_MAX_LEN: int = 300
    # 规律提炼定时开关（第 18 步首版以手动触发为主，**未注册定时作业**；
    # 本项为后续（如第 19 步）预留。保持 false 时零影响。）
    INSIGHTS_SCHEDULER_ENABLED: bool = False
    # ---- RAG 反哺开关（默认开启；关闭后退化为「无 RAG」原链路）----
    # 选题：检索 hit_pattern / topic_template / review_insight 拼进系统提示词
    TOPIC_RAG_ENABLED: bool = True
    # 创作：检索 hit_pattern / review_insight 拼进上下文
    CONTENT_RAG_ENABLED: bool = True

    # ---------------------------------------------------- 关键词搜索雷达（第 23 步）
    # GitHub 仓库搜索（官方 api.github.com，只读公开数据）。
    # 【红线】只接「公开、免登录、有官方/稳定接口」的源，本步只注册 github；
    # 其余源只留扩展位（实现 SearchProvider 抽象 + 注册即接入）。
    # 搜索是**确定性工具/服务层**：不挂 LangGraph 图、不调用 LLM、不烧 token。
    # 【令牌】GITHUB_TOKEN 可选：留空匿名（限速约 10 次/分钟），填写后提升
    #   （约 30 次/分钟）——一律以响应头 X-RateLimit-* 为准动态退让。
    #   真实 token 只填本地 .env，绝不入代码 / 日志 / 响应 / 前端产物；
    #   获取路径：https://github.com/settings/tokens
    # 【代理】GITHUB_API_BASE 为外网地址时读 HTTPS_PROXY（国内访问用）；
    #   localhost / 127.0.0.1 绝不走代理。
    GITHUB_TOKEN: str = ""
    GITHUB_API_BASE: str = "https://api.github.com"
    # 单请求超时（秒）与最大尝试次数（仅 5xx / 网络异常重试；
    # 4xx / 429 / 403-限速一律不重试，按 Reset 退避）
    SEARCH_TIMEOUT_SECONDS: int = 10
    SEARCH_MAX_ATTEMPTS: int = 3
    # q 最大长度（strip 后为空 / 超长 → 422）与单次返回条数上限（GitHub per_page ≤ 30）
    SEARCH_QUERY_MAX_LEN: int = 100
    SEARCH_LIMIT_MAX: int = 30
    # 搜索缓存 TTL（秒；默认 30 分钟）——尊重上游限额、避免硬刷
    SEARCH_CACHE_TTL_SECONDS: int = 1800
    # 限速响应缺少 X-RateLimit-Reset 头时的本地退避兜底（秒）
    SEARCH_BACKOFF_FALLBACK_SECONDS: int = 60

    @property
    def cors_origin_list(self) -> list[str]:
        """把 CORS_ORIGINS 字符串拆成列表。"""
        return [item.strip() for item in self.CORS_ORIGINS.split(",") if item.strip()]

    @property
    def native_database_url(self) -> str:
        """去掉 SQLAlchemy 方言段的**原生** PostgreSQL DSN。

        例如 ``postgresql+psycopg://...`` -> ``postgresql://...``。
        psycopg 的 ``ConnectionPool`` 只接受原生 DSN，喂 SQLAlchemy URL 会直接报错。
        """
        return self.DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)

    @property
    def hotspot_source_list(self) -> list[str]:
        """把 ``HOTSPOT_ENABLED_SOURCES`` 拆成去空白的列表（顺序保留）。"""
        return [item.strip() for item in self.HOTSPOT_ENABLED_SOURCES.split(",") if item.strip()]

    @property
    def metric_allowed_image_types(self) -> list[str]:
        """把 ``METRIC_ALLOWED_IMAGE_TYPES`` 拆成小写列表（顺序保留）。"""
        return [
            item.strip().lower()
            for item in self.METRIC_ALLOWED_IMAGE_TYPES.split(",")
            if item.strip()
        ]

    @property
    def screenshot_dir_path(self) -> Path:
        """截图保存目录的绝对路径（相对 ``backend/`` 解析）。"""
        raw = Path(self.SCREENSHOT_DIR)
        return raw if raw.is_absolute() else (BASE_DIR / raw)


@lru_cache
def get_settings() -> Settings:
    """带缓存的配置读取入口。"""
    return Settings()


settings = get_settings()
