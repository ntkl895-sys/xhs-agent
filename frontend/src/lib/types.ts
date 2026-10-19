/**
 * 前后端共享的 TypeScript 类型（与后端 Pydantic schema 一一对应）。
 *
 * 只放类型，不放运行时代码，方便前后端对照修改。
 */

export type AccountStatus = "active" | "archived";
export type PersonaStatus = "active" | "archived";

/** 账号（accounts 表对外结构） */
export interface Account {
  id: string;
  name: string;
  xhs_red_id: string | null;
  niche: string | null;
  status: AccountStatus;
  remark: string | null;
  created_at: string;
  updated_at: string;
}

/** 人设（personas 表对外结构 + 归属账号名） */
export interface Persona {
  id: string;
  account_id: string | null;
  /** 由后端 LEFT JOIN accounts 得到；公共人设库为 null */
  account_name: string | null;
  name: string;
  positioning: string | null;
  target_audience: string | null;
  tone: string | null;
  content_pillars: string[] | null;
  taboo: string[] | null;
  signature_style: string | null;
  system_prompt: string | null;
  is_default: boolean;
  status: PersonaStatus;
  created_at: string;
  updated_at: string;
}

/** 统一分页响应 */
export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

/** 统一错误体 */
export interface ApiErrorBody {
  status: "error";
  code: string;
  message: string;
  details?: unknown;
}

/** 新建 / 更新账号的请求体 */
export interface AccountWrite {
  name?: string;
  xhs_red_id?: string | null;
  niche?: string | null;
  remark?: string | null;
  status?: AccountStatus;
}

/** 新建 / 更新人设的请求体 */
export interface PersonaWrite {
  name?: string;
  account_id?: string | null;
  positioning?: string | null;
  target_audience?: string | null;
  tone?: string | null;
  content_pillars?: string[] | null;
  taboo?: string[] | null;
  signature_style?: string | null;
  system_prompt?: string | null;
  is_default?: boolean;
  status?: PersonaStatus;
}

/* ======================================================================
 * 第 11 步：热点雷达（对照 backend/app/schemas/hotspot.py）
 * ==================================================================== */

/** 热搜条目的处理状态（三态可人工互逆切换） */
export type HotspotStatus = "new" | "adopted" | "ignored";

/**
 * 热搜条目（HotspotOut，字段对齐 hotspots 表）。
 *
 * 日期字段为「日期」串（YYYY-MM-DD），时间为 ISO 串。
 */
export interface Hotspot {
  id: string;
  source: string;
  source_item_id: string | null;
  title: string;
  url: string | null;
  mobile_url: string | null;
  hot_score: number | null;
  rank_position: number | null;
  cover_url: string | null;
  summary: string | null;
  author: string | null;
  raw_extra: Record<string, unknown> | null;
  hot_date: string | null;
  fetched_at: string | null;
  content_hash: string;
  status: HotspotStatus;
  created_at: string;
}

/** 各源状态（HotspotSourceOut：配置清单 + 最近一次采集统计） */
export interface HotspotSource {
  source: string;
  name: string;
  enabled: boolean;
  last_fetched_at: string | null;
  last_status: string | null;
  last_new: number | null;
  last_updated: number | null;
  last_failed: number | null;
}

/** 单源统计（SourceStatOut） */
export interface SourceStat {
  source: string;
  source_name: string;
  status: string;
  fetched: number;
  new: number;
  updated: number;
  failed: number;
  blocked: number;
  error: string | null;
}

/** 整次采集总计（CollectTotalsOut；failed / blocked 记的是源个数） */
export interface CollectTotals {
  sources: number;
  fetched: number;
  new: number;
  updated: number;
  failed: number;
  blocked: number;
}

/** 手动刷新结果（HotspotRefreshOut） */
export interface HotspotRefreshResult {
  run_id: string | null;
  trigger: string;
  status: string;
  hot_date: string | null;
  latency_ms: number | null;
  totals: CollectTotals;
  per_source: SourceStat[];
}

/** 热搜采集运行记录（HotspotRunOut，精简） */
export interface HotspotRun {
  id: string;
  run_type: string;
  trigger: string;
  status: string | null;
  inputs: Record<string, unknown> | null;
  outputs: Record<string, unknown> | null;
  latency_ms: number | null;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
}

/** 人工标记热点状态的请求体（后续仅此一个字段） */
export interface HotspotUpdateRequest {
  status: HotspotStatus;
}

/* ======================================================================
 * 第 23 步：关键词搜索（对照 backend/app/schemas/search.py）
 *
 * 注意：类型中**没有**任何 token / 凭据字段——令牌只存在于后端 .env，
 * 不进入前端产物与浏览器。
 * ==================================================================== */

/** 关键词搜索结果单条（SearchItemOut） */
export interface SearchItem {
  /** 仓库全名（owner/repo） */
  title: string;
  /** 仓库主页（GitHub 域） */
  url: string;
  /** 来源标识（当前仅 github） */
  source: string;
  /** 仓库描述 */
  snippet: string | null;
  /** star 数 */
  score: number | null;
  /** 附加信息：language / updated_at / owner / forks_count 等 */
  meta: Record<string, unknown>;
}

/** 关键词搜索响应（SearchResponseOut） */
export interface SearchResponse {
  source: string;
  query: string;
  sort: string;
  count: number;
  /** 是否命中后端缓存 */
  cached: boolean;
  items: SearchItem[];
}

/* ======================================================================
 * 第 11 步：选题库（对照 backend/app/schemas/topic.py）
 * ==================================================================== */

/** 选题生命周期状态机 */
export type TopicStatus = "draft" | "approved" | "writing" | "published" | "discarded";

/** 选题创作难度 */
export type TopicDifficulty = "easy" | "medium" | "hard";

/** 选题列表项（TopicListItem；**不含** content_outline / target_keywords） */
export interface TopicListItem {
  id: string;
  account_id: string | null;
  persona_id: string | null;
  title: string;
  angle: string | null;
  summary: string | null;
  pillar: string | null;
  tags: string[] | null;
  heat_score: number | null;
  difficulty: TopicDifficulty | null;
  score: number | null;
  status: TopicStatus;
  source_hotspot_ids: unknown[] | null;
  generated_at: string | null;
  created_at: string;
  /** 展示用（非数据库列） */
  persona_name: string | null;
  account_name: string | null;
  source_title: string | null;
  source_platform: string | null;
}

/** 溯源单条（source_trace 元素；来自选题生成时的热点快照） */
export interface TopicSourceTrace {
  source?: string | null;
  title?: string | null;
  url?: string | null;
  mobile_url?: string | null;
  hot_date?: string | null;
  hot_score?: number | null;
  rank_position?: number | null;
}

/** 选题详情（TopicOut；**不含** llm_raw —— 它在 agent_runs） */
export interface TopicOut {
  id: string;
  account_id: string | null;
  persona_id: string | null;
  title: string;
  angle: string | null;
  summary: string | null;
  pillar: string | null;
  content_outline: string[] | null;
  tags: string[] | null;
  target_keywords: string[] | null;
  source_hotspot_ids: unknown[] | null;
  source_trace: TopicSourceTrace[] | null;
  heat_score: number | null;
  difficulty: TopicDifficulty | null;
  score: number | null;
  llm_model: string | null;
  meta: Record<string, unknown> | null;
  review_note: string | null;
  status: TopicStatus;
  generated_at: string | null;
  created_at: string;
  updated_at: string;
  /** 展示用（非数据库列） */
  persona_name: string | null;
  account_name: string | null;
}

/** 选题 Agent 运行记录（TopicRunOut；不含 llm_raw） */
export interface TopicRun {
  run_id: string;
  trigger: string | null;
  status: string | null;
  created_at: string;
  finished_at: string | null;
  latency_ms: number | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  total_tokens: number | null;
  json_parse_attempts: number | null;
  error_message: string | null;
  generated: number | null;
  persisted: number | null;
  rejected: number | null;
  skipped: number | null;
  topic_ids: string[] | null;
}

/** 选题生成请求体（TopicGenerateRequest） */
export interface TopicGenerateRequest {
  persona_id: string;
  /** 指定热点；为空则按当天优先 + 热度自动取 */
  hotspot_ids?: string[] | null;
  per_hotspot: number;
  limit_hotspots: number;
  force: boolean;
}

/** 选题生成响应（后端 service 返回的汇总） */
export interface TopicGenerateResult {
  status: string;
  requested: number;
  succeeded: number;
  failed: number;
  skipped: number;
  persisted_count: number;
  topics: Array<{ id: string; title: string; [key: string]: unknown }>;
  runs: Array<Record<string, unknown>>;
}

/** 人工微调选题请求体（TopicUpdateRequest） */
export interface TopicUpdateRequest {
  title?: string;
  summary?: string | null;
  angle?: string | null;
  pillar?: string | null;
  content_outline?: string[] | null;
  tags?: string[] | null;
  target_keywords?: string[] | null;
  review_note?: string | null;
  status?: TopicStatus;
}

/* ======================================================================
 * 第 12 步：笔记（对照 backend/app/schemas/note.py；本步只用列表项）
 * ==================================================================== */

/** 笔记生命周期状态 */
export type NoteStatus = "draft" | "ready" | "published" | "archived";

/** 合规自检结论（creative 产物；无 blocked） */
export type ComplianceStatus = "pending" | "passed" | "warning";

/** 笔记列表项（NoteListItem；含人设名 / 账号名，均为展示用非数据库列） */
export interface NoteListItem {
  id: string;
  account_id: string | null;
  persona_id: string | null;
  topic_id: string | null;
  title: string;
  tags: string[] | null;
  compliance_status: ComplianceStatus;
  status: NoteStatus;
  /** 作品链接（第 14 步增补：列表展示已发布外链所需；未发布为 null） */
  publish_url: string | null;
  version: number;
  created_at: string;
  updated_at: string;
  persona_name: string | null;
  account_name: string | null;
  /** 违禁词命中数（由 content_check 派生；列表快速辨识用） */
  forbidden_hits: number | null;
}

/* ======================================================================
 * 第 13 步：封面候选（对照 backend/app/schemas/cover.py）
 * ==================================================================== */

/** 封面候选（CoverOut；逐字段对齐 covers 表，不含任何敏感字段） */
export interface Cover {
  id: string;
  note_id: string;
  /** 模板标识：gradient_hero / list_clean / bg_image / highlight */
  template_key: string;
  /** 配色方案：coral / cream / forest / neon / ocean */
  color_scheme: string;
  title_text: string | null;
  subtitle_text: string | null;
  /** 其余编辑字段（tags / bullets / author / page_no / highlight_word） */
  config_json: Record<string, unknown> | null;
  /** 本工程一律为 null（PNG 由前端实时渲染下载，不落服务器磁盘） */
  image_path: string | null;
  width: number;
  height: number;
  is_selected: boolean;
  version: number;
  created_at: string;
}

/** 某笔记的候选列表（CoverListOut） */
export interface CoverList {
  note_id: string;
  /** 当前选中候选 id；没有选中时为 null */
  selected_id: string | null;
  items: Cover[];
}

/** 新建候选请求体（CoverCreateRequest） */
export interface CoverCreateRequest {
  note_id: string;
  template_key: string;
  color_scheme: string;
  title_text?: string | null;
  subtitle_text?: string | null;
  config_json?: Record<string, unknown> | null;
}

/** 更新候选请求体（CoverUpdateRequest；is_selected=true 时后端同事务互斥选中） */
export interface CoverUpdateRequest {
  template_key?: string;
  color_scheme?: string;
  title_text?: string | null;
  subtitle_text?: string | null;
  config_json?: Record<string, unknown> | null;
  is_selected?: boolean;
}

/* ======================================================================
 * 第 14 步：创作台（对照 backend/app/schemas/note.py 第 14 步增补）
 * ==================================================================== */

/** 违禁词单条命中（notes.content_check.hits 的元素） */
export interface ContentCheckHit {
  word: string;
  category: string;
  /** 命中位置：title / body / tags */
  field: string;
  /** 命中片段（截取自原文，展示用） */
  snippet: string;
}

/** 合规自检明细（notes.content_check 的整体结构；只提示、不阻断） */
export interface ContentCheck {
  status: ComplianceStatus;
  hits: ContentCheckHit[];
  checked_fields: string[];
  checked_at: string;
}

/** ``notes.llm_meta`` 对外白名单键（其余键一律不返回） */
export interface NoteLlmMeta {
  prompt_version?: string | number | null;
  json_parse_attempts?: number | null;
  model?: string | null;
}

/**
 * 笔记详情（NoteOut；对照后端 schema 逐字段对齐）。
 *
 * **不含 ``llm_raw``** —— 它只写进 ``agent_runs``，接口一律不返回。
 */
export interface NoteOut {
  id: string;
  account_id: string | null;
  persona_id: string | null;
  topic_id: string | null;
  title: string;
  body: string | null;
  tags: string[] | null;
  content_check: ContentCheck | null;
  compliance_status: ComplianceStatus;
  status: NoteStatus;
  publish_url: string | null;
  xhs_note_id: string | null;
  published_at: string | null;
  version: number;
  llm_meta: NoteLlmMeta | null;
  created_at: string;
  updated_at: string;
  /** 展示用（非数据库列） */
  persona_name: string | null;
  account_name: string | null;
}

/**
 * 编辑 / 状态流转请求体（NoteUpdateRequest）。
 *
 * 约定（与后端一致）：
 * - 不带 ``status`` = 仅编辑 ``title / body / tags``（不触碰状态机）；
 * - 带 ``status`` = 状态流转，**不得**同时携带编辑字段；
 * - ``publish_url`` / ``xhs_note_id`` 仅在 ``status="published"`` 时回填。
 */
export interface NoteUpdateRequest {
  title?: string;
  body?: string | null;
  tags?: string[] | null;
  status?: NoteStatus;
  publish_url?: string | null;
  xhs_note_id?: string | null;
}

/* ======================================================================
 * 第 15 步：数据回填（对照 backend/app/schemas/metric.py）
 * ==================================================================== */

/** 数据快照来源：screenshot（截图识别）/ manual（手工录入） */
export type MetricDataSource = "screenshot" | "manual";

/** 六个指标的值容器（缺失即 null，绝不臆造） */
export interface MetricValues {
  views: number | null;
  likes: number | null;
  collects: number | null;
  comments: number | null;
  shares: number | null;
  new_followers: number | null;
}

/** 单条数据快照（MetricSnapshotOut；screenshot_path 只回传文件名） */
export interface MetricSnapshot {
  id: string;
  note_id: string;
  captured_at: string;
  data_source: MetricDataSource;
  views: number | null;
  likes: number | null;
  collects: number | null;
  comments: number | null;
  shares: number | null;
  new_followers: number | null;
  evidence: Record<string, unknown> | null;
  /** 截图留档文件名（默认 null；仅 SAVE_SCREENSHOT=true 且识别时确认保存才非空） */
  screenshot_path: string | null;
  created_at: string;
}

/** 某笔记的数据时间线（MetricSnapshotListOut；captured_at 倒序） */
export interface MetricSnapshotList {
  note_id: string;
  items: MetricSnapshot[];
}

/**
 * 落库请求体（MetricSnapshotCreate）。
 *
 * **不接受** `screenshot_path`（后端 extra=ignore，服务端只从
 * `evidence.screenshot_file` 提取自己生成的文件名）。
 */
export interface MetricSnapshotCreate {
  note_id: string;
  /** ISO 8601；无时区时后端按 UTC+8 解释 */
  captured_at: string;
  data_source?: MetricDataSource;
  views?: number | null;
  likes?: number | null;
  collects?: number | null;
  comments?: number | null;
  shares?: number | null;
  new_followers?: number | null;
  evidence?: Record<string, unknown> | null;
}

/**
 * 截图识别候选（MetricRecognizeResult；**绝不写库**）。
 *
 * 人工核对（可编辑）后才通过 `POST /api/v1/metrics` 落库；
 * 不含 llm_raw / base64 / 绝对路径。
 */
export interface MetricRecognizeResult {
  data_source: MetricDataSource;
  metrics: MetricValues;
  evidence: Record<string, unknown> | null;
  captured_at: string;
  /** 未能识别的字段（前端提示条展示） */
  missing_fields: string[];
  /** 识别出但不确信的字段（前端提示条展示） */
  uncertain_fields: string[];
  /** JSON 解析尝试次数（正常为 1） */
  parse_attempts: number;
  warnings: string[];
  note_id: string | null;
  note_title: string | null;
  /** 截图留档文件名（仅当后端 SAVE_SCREENSHOT=true 且本次确认保存） */
  screenshot_file: string | null;
  run_id: string | null;
}

/* ======================================================================
 * 第 16 步：单篇诊断（对照 backend/app/schemas/diagnostic.py）
 * ==================================================================== */

/** 诊断类型：cold_start（冷启动）/ review（复盘）/ anomaly（异常波动） */
export type DiagnosticType = "cold_start" | "review" | "anomaly";

/** 类型判定来源：auto（自动判定）/ manual（用户显式指定） */
export type DiagnosisTypeSource = "auto" | "manual";

/** benchmark 列的固定结构（只装该笔记自身相对指标 + 基准缺失声明） */
export interface DiagnosticBenchmark {
  /** 基准口径：本期恒为 self_only（只基于该笔记自身） */
  basis: string | null;
  /** 最新六指标 */
  latest_metrics: Record<string, number | null>;
  /** 六条互动率（缺失为 null） */
  rates: Record<string, number | null>;
  /** 环比 / 总增量 / 增长率 / 覆盖时长 */
  trends: Record<string, unknown>;
  /** 本地判定阈值（异常降幅比例） */
  thresholds: Record<string, unknown>;
  /** 外部基准：本期恒为 null（绝不杜撰行业均值；跨篇基准见第 18 步） */
  external_benchmark: unknown | null;
  /** 基准缺失声明（中文） */
  benchmark_note: string | null;
}

/** llm_meta 白名单（严格五项；接口一律不返回 llm_raw） */
export interface DiagnosticMeta {
  prompt_version?: string | null;
  json_parse_attempts?: number | null;
  diagnosis_type?: string | null;
  diagnosis_type_source?: DiagnosisTypeSource | null;
  data_gaps?: string[];
}

/**
 * 单条诊断结论（DiagnosticOut；逐列对齐 diagnostics 表）。
 *
 * **不含 ``llm_raw``** —— 它只写进 ``agent_runs``，接口一律不返回。
 */
export interface Diagnostic {
  id: string;
  note_id: string;
  /** 所依据的数据快照 id（无快照时为 null） */
  based_on_metric_id: string | null;
  diagnosis_type: DiagnosticType | null;
  /** 0~100；数据严重不足时为 null（不硬打分） */
  score: number | null;
  strengths: string[];
  weaknesses: string[];
  reasons: string[];
  suggestions: string[];
  benchmark: DiagnosticBenchmark | null;
  /** 实际模型名（不含 key） */
  llm_model: string | null;
  llm_meta: DiagnosticMeta | null;
  created_at: string;
}

/** 某笔记的诊断历史（DiagnosticListOut；created_at 倒序） */
export interface DiagnosticList {
  note_id: string;
  items: Diagnostic[];
}

/** 诊断生成请求体（DiagnosticGenerateRequest；不传 diagnosis_type 即自动判定） */
export interface DiagnosticGenerateRequest {
  note_id: string;
  diagnosis_type?: DiagnosticType | null;
}

/* ======================================================================
 * 第 17 步：运营看板 + 周报
 *（对照 backend/app/schemas/dashboard.py、backend/app/schemas/report.py）
 * ==================================================================== */

/** 指标名（六指标白名单，与后端 METRIC_FIELDS 一致） */
export type MetricName =
  | "views"
  | "likes"
  | "collects"
  | "comments"
  | "shares"
  | "new_followers";

/** 趋势分桶模式：day（自然日）/ auto（等间隔；请求 day 超点数上限时自动收紧） */
export type TrendBucket = "day" | "auto";

/** 全账号最新漏斗（published 笔记合计口径；缺失按 0，覆盖率见 metric_coverage） */
export interface DashboardFunnel {
  /** 六指标合计（缺失按 0） */
  latest_totals: Record<string, number>;
  /** 六率（views 合计<=0 时全 null；4 位小数） */
  rates: Record<string, number | null>;
  /** 至少一个指标有值的 published 笔记数 */
  covered_notes: number;
  /** 参与统计的 published 笔记总数 */
  published_notes: number;
  /** 每个指标有非空值的笔记数（覆盖率可识别） */
  metric_coverage: Record<string, number>;
}

/** GET /api/v1/dashboard/summary 响应 */
export interface DashboardSummary {
  /** 计数：accounts / notes_total / notes_published / notes_ready /
   *  notes_draft / topics_writing / notes_published_in_range */
  counts: Record<string, number>;
  funnel: DashboardFunnel;
  /** 周期窗口（近 N 个自然日，含今天） */
  range_days: number;
  /** 聚合生成时间（本地时区） */
  generated_at: string;
}

/** 趋势上的一个桶（value=null 表示该桶无数据，前端断线展示、不插值） */
export interface TrendPoint {
  /** 桶起始日（YYYY-MM-DD，本地时区） */
  bucket: string;
  value: number | null;
}

/** 趋势覆盖率信息（有多少笔记 / 多少桶真的有数据） */
export interface TrendCoverage {
  notes: number;
  notes_with_data: number;
  points_with_data: number;
  points_total: number;
  step_days: number;
  range_days: number;
}

/** GET /api/v1/dashboard/trend 响应 */
export interface DashboardTrend {
  metric: MetricName;
  /** **生效**分桶模式（请求 day 但超点数上限时会收紧为 auto） */
  bucket: TrendBucket;
  points: TrendPoint[];
  coverage: TrendCoverage;
}

/** 排行中的一篇笔记（value=null = 该指标暂无数据，排最后） */
export interface RankingItem {
  note_id: string;
  title: string | null;
  value: number | null;
  published_at: string | null;
}

/** GET /api/v1/dashboard/ranking 响应 */
export interface DashboardRanking {
  metric: MetricName;
  items: RankingItem[];
}

/** 周报报告本体（归一后的固定结构；data_gaps 已与本地缺口合并去重） */
export interface WeeklyReportSections {
  period_start: string | null;
  period_end: string | null;
  summary: string;
  highlights: string[];
  issues: string[];
  /** 关键结论的数据依据（引用给定事实数字） */
  data_evidence: string[];
  /** 下周可执行动作（按重要性排序） */
  next_week_suggestions: string[];
  /** 数据缺口声明（本地 + 模型合并） */
  data_gaps: string[];
  /** 0~1；无法评估时为 null */
  confidence: number | null;
}

/** POST /api/v1/reports/weekly/generate 响应 */
export interface WeeklyReport {
  run_id: string;
  period_start: string | null;
  period_end: string | null;
  report: WeeklyReportSections | null;
  /** JSON 解析尝试（理想为 1） */
  parse_attempts: number | null;
  data_gaps: string[];
  warnings: string[];
}

/** GET /api/v1/reports/weekly/{run_id} 详情 */
export interface WeeklyReportDetail {
  run_id: string;
  status: string | null;
  created_at: string | null;
  llm_model: string | null;
  parse_attempts: number | null;
  total_tokens: number | null;
  period_start: string | null;
  period_end: string | null;
  report: WeeklyReportSections | null;
}

/** GET /api/v1/reports/weekly 列表项（精简） */
export interface WeeklyReportListItem {
  run_id: string;
  status: string | null;
  created_at: string | null;
  period_start: string | null;
  period_end: string | null;
  summary: string;
  confidence: number | null;
  data_gaps_count: number;
  next_week_suggestions_count: number;
  parse_attempts: number | null;
  total_tokens: number | null;
}

/** GET /api/v1/reports/weekly 响应（created_at 倒序） */
export interface WeeklyReportList {
  items: WeeklyReportListItem[];
}

/** 周报生成请求体（三参数全可选；days 与显式起止互斥） */
export interface WeeklyReportGenerateRequest {
  period_start?: string | null;
  period_end?: string | null;
  days?: number | null;
}

/* ======================================================================
 * 第 18 步：RAG 知识库
 *（对照 backend/app/schemas/knowledge.py；均不含 embedding 向量、不含 llm_raw）
 * ==================================================================== */

/** 知识块类型（与后端 ChunkType 一致） */
export type ChunkTypeName =
  | "hit_pattern"
  | "forbidden_word"
  | "persona_knowledge"
  | "topic_template"
  | "review_insight"
  | "general";

/** 知识块状态（active / archived；无物理删除） */
export type SnippetStatusName = "active" | "archived";

/** 单条知识块（不含 embedding 向量） */
export interface KnowledgeSnippet {
  id: string;
  chunk_type: ChunkTypeName;
  ref_type: string | null;
  title: string | null;
  content: string;
  /** 来源 / 证据 / 样本量 / 置信度 / content_hash 等 */
  meta: Record<string, unknown> | null;
  status: SnippetStatusName;
  created_at: string;
}

/** GET /api/v1/knowledge 列表（created_at 倒序） */
export interface SnippetList {
  items: KnowledgeSnippet[];
  /** 过滤后的总数（不受分页影响） */
  total: number;
  limit: number;
  offset: number;
}

/** POST /api/v1/knowledge/extract 响应（规律提炼结果摘要） */
export interface ExtractResult {
  run_id: string;
  /** success / partial（样本不足或有条目被剔除） */
  status: string;
  /** 样本不足：未调用模型、零写入（data_gaps 有指引） */
  insufficient_sample: boolean;
  sample_size: number | null;
  min_required: number | null;
  generated: number;
  persisted: number;
  reused: number;
  skipped: number;
  snippet_ids: string[];
  rejected: number;
  data_gaps: string[];
  warnings: string[];
  parse_attempts: number | null;
  prompt_version: string | null;
}

/** 检索命中（不含 embedding 向量） */
export interface KnowledgeSearchHit {
  id: string;
  chunk_type: ChunkTypeName;
  title: string | null;
  content: string;
  meta: Record<string, unknown> | null;
  status: SnippetStatusName;
  created_at: string;
  /** 余弦相似度（-1~1；越大越相似） */
  similarity: number;
}

/** POST /api/v1/knowledge/search 响应（top-k，按相似度降序） */
export interface KnowledgeSearchResult {
  query: string;
  items: KnowledgeSearchHit[];
}

/** 检索测试请求体 */
export interface KnowledgeSearchRequest {
  query: string;
  chunk_types?: ChunkTypeName[] | null;
  k?: number;
}

/** PATCH /api/v1/knowledge/{id} 请求体（状态切换 / 编辑标题正文） */
export interface SnippetUpdateRequest {
  /** 仅允许 active / archived 间切换（缺省 = 不改状态） */
  status?: SnippetStatusName;
  /** 新标题（缺省 = 不改；空串 = 清空） */
  title?: string | null;
  /** 新正文（变化时后端重算 content_hash 并重新向量化） */
  content?: string | null;
}

/* ======================================================================
 * 第 19 步：知识库人工维护（手动块 / 违禁词入库 / 人设知识 / 文档导入）
 *（对照 backend/app/schemas/knowledge.py；仍不含 embedding 向量、不含 llm_raw）
 * ==================================================================== */

/** POST /api/v1/knowledge/manual 请求体（手动创建知识块） */
export interface SnippetManualCreateRequest {
  chunk_type: ChunkTypeName;
  title?: string | null;
  content: string;
  /** 违禁词类别（仅 chunk_type=forbidden_word；缺省 manual） */
  category?: string | null;
  /** 可选弱关联类型（persona 需带 ref_id） */
  ref_type?: "persona" | "manual_doc" | null;
  ref_id?: string | null;
}

/** 写操作（手动创建 / 编辑）响应：知识块 + 非阻断提示（仍不含向量） */
export interface SnippetWriteResult extends KnowledgeSnippet {
  /** 如「重向量化失败：已保存正文并置空向量」 */
  warnings: string[];
}

/** POST /api/v1/knowledge/seed-forbidden-words 响应（幂等同步内置词表） */
export interface SeedForbiddenWordsResult {
  total: number;
  inserted: number;
  reused: number;
  would_insert: number;
  dry_run: boolean;
}

/** POST /api/v1/knowledge/import-text 请求体（文档导入·文本粘贴） */
export interface ImportTextRequest {
  content: string;
  chunk_type?: "general" | "persona_knowledge";
  /** chunk_type=persona_knowledge 时必填（人设 id） */
  ref_id?: string | null;
  chunk_size?: number | null;
  chunk_overlap?: number | null;
}

/** 文档导入结果（文本 / 文件共用；如实计数） */
export interface ImportResult {
  total_chunks: number;
  inserted: number;
  skipped: number;
  failed: number;
  snippet_ids: string[];
  warnings: string[];
  chunk_size: number;
  chunk_overlap: number;
  /** 实际使用的切分实现（官方 / 内置等效） */
  splitter: string;
}

/* ======================================================================
 * 第 23 步 · V2 增强（二）：同选题多版本创作
 *（对照 backend/app/schemas/content_variants.py；不含 llm_raw、不含向量字段）
 * ==================================================================== */

/** 版本级判定：合格 / 违禁命中被拦截 / 缺标题正文不合格 */
export type ContentVariantStatus = "qualified" | "blocked" | "rejected";

/** 违禁命中明细项（仅 blocked 版本携带） */
export interface VariantHit {
  word?: string;
  category?: string;
  field?: string;
  snippet?: string;
}

/** POST /api/v1/content-variants/generate 请求体 */
export interface VariantsGenerateRequest {
  topic_id: string;
  /** 版本数（1~5，默认 3） */
  n?: number;
  /** 显式角度列表（留空 = 自动从角度池选取；提供时数量需与 n 一致且不重复） */
  angles?: string[] | null;
}

/** 生成响应中的单个版本（含被拦截 / 不合格的版本，供即时对比） */
export interface VariantGenerateItem {
  angle: string;
  title: string;
  body: string;
  tags: string[];
  status: ContentVariantStatus;
  reason?: string | null;
  /** 仅 qualified 版本有值（已落库的笔记 id） */
  note_id?: string | null;
  hits?: VariantHit[] | null;
}

/** POST /generate 的返回：汇总计数 + 逐版明细（不含 llm_raw） */
export interface VariantsGenerateResult {
  status: string;
  run_id?: string | null;
  topic_id: string;
  variant_group_id?: string | null;
  requested: number;
  generated?: number | null;
  persisted?: number | null;
  blocked?: number | null;
  rejected?: number | null;
  items: VariantGenerateItem[];
  note_ids: string[];
  error_message?: string | null;
}

/** 批次内的一条版本笔记（对比 / 采用响应用） */
export interface VariantNote {
  note_id: string;
  angle: string;
  index?: number | null;
  title: string;
  body: string;
  tags: string[];
  compliance_status: ComplianceStatus;
  adopted: boolean;
  created_at: string;
}

/** 一个批次（variant_group_id 下的一组版本） */
export interface VariantBatch {
  group_id: string;
  created_at: string;
  variants: VariantNote[];
}

/** GET /api/v1/content-variants?topic_id= 的返回 */
export interface VariantsBatchesResult {
  topic_id: string;
  batches: VariantBatch[];
}

/** PATCH /{note_id}/adopt 的返回：采用后的整批最新状态 */
export interface VariantAdoptResult {
  group_id: string;
  adopted_note_id: string;
  variants: VariantNote[];
}
