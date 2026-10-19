# xhs-agent · 本地运维手册（一键启停 / 常驻自愈 / 备份恢复）

> 此前交付。本文档面向**日常使用**：一键启动与停止、常驻监控、开机自启、
> 备份与恢复、日志位置、常见故障排查、可撤销项与口径标注。
>
> 设计红线（全步一致）：
> - **纯本地、只绑 127.0.0.1**，不做公网部署（留到此前）；
> - 进程托管只用 **Windows 内置能力**（PowerShell + 任务计划程序），
> 不引入 PM2 / NSSM / Nginx / 外部 supervisor；
> - 所有自动化（开机自启、定时备份）**默认不启用**、提供**可撤销**的卸载脚本，
> **绝不静默改系统**；卸载 = 删除任务，即复原；
> - 不自动安装依赖、不自动填 key、不自动改 `.env`；脚本与日志**不回显 API key**；
> `.env` 默认不纳入备份；
> - 停容器**绝不携带卷删除参数**（命名卷 `xhs-agent-pgdata` / `xhs-agent-redis-data` 永久保留）。

---

## 一、环境要求与前提

| 项 | 要求 | 说明 |
| --- | --- | --- |
| Windows | Windows 10/11 + Windows PowerShell 5.1 | 一键脚本按 PS 5.1 语法编写并实测 |
| Docker Desktop | 已安装（见 `scripts/install-docker.ps1`） | `db` / `redis` 容器必需；`dailyhot` 可容错缺失 |
| Node.js | 18+（本机 v22 / v24 实测均可） | 前端 `next dev` / `next start` |
| Python | `backend\.venv`（项目虚拟环境） | 未创建时一键脚本**只提示不代建** |
| 配置 | `backend\.env` 必须存在 | 从 `backend\.env.example` 复制后**人工填写**（脚本不代填） |

> 一键脚本对以上各项做**只检测、零副作用**的自检；缺失项给出明确命令指引后退出，不会产生任何文件。

---

## 二、一键启停（scripts\ops\）

### 2.1 最常用：双击入口

| 双击 | 等价命令 | 行为 |
| --- | --- | --- |
| `start.bat` | `scripts\ops\start.ps1` | 自检 → 起容器 → 迁移只读核对 → （生产模式按需构建）→ 起前后端 → 健康检查全绿 → 打开浏览器 |
| `stop.bat` | `scripts\ops\stop.ps1` | 优雅停止前后端（先温和后强杀）→ 端口释放核查 →（默认保留容器与数据） |

可传参：`start.bat -Dev`（开发模式）、`start.bat -Watch`（常驻监控）、`stop.bat -DownContainers`（连容器一起停，保留数据卷）。

### 2.2 start.ps1 开关与退出码

| 开关 | 作用 |
| --- | --- |
| `-Dev` | 开发模式：`uvicorn --reload` + `next dev`（跳过生产构建） |
| `-Watch` | 常驻监控：异常退出按退避自动重启（2/5/10/20/30/60 秒封顶）；手动 stop 不再拉起 |
| `-AutoDocker` | Docker 引擎未运行时**尝试启动** Docker Desktop（非管理员） |
| `-Migrate` | 数据库落后时执行 `alembic upgrade head`（有副作用，默认只提示） |
| `-NoBrowser` | 不自动打开浏览器 |
| `-SkipDailyhot` | 跳过 dailyhot（热搜刷新暂不可用；核心功能不受影响） |
| `-Root <dir>` | 指定项目根（默认按脚本位置推断；自检用） |

| 退出码 | 含义 |
| --- | --- |
| 0 | 已就绪（启动成功，或检测到已在运行且健康检查通过） |
| 1 | 其他错误（端口被外部进程占用 / 前端构建失败 / 意外异常） |
| 2 | 环境预检未通过（`.env` / venv / 前端依赖 / Node 缺失） |
| 3 | Docker 未就绪（未安装 / 引擎未运行 / 容器无法启动） |
| 4 | 等待健康检查超时 |
| 5 | 数据库迁移未到 head（且未给 `-Migrate`） |

要点：

- **幂等**：重复运行已启动的 `start.bat` 不会重复拉起进程，只做健康复核；
- **端口冲突**：8000 / 3000 被**外部进程**占用时明确报错退出（不静默强杀他人进程）；
 属本工程的残留进程则自动识别并接管/提示；
- **代理清洗**：脚本为子进程摘除 `HTTP(S)_PROXY` / `ALL_PROXY` 等变量并设 `NO_PROXY=localhost,127.0.0.1`
 ——避免本机回环请求被系统代理劫持（全工程统一做法）；
- **dailyhot 容错**：镜像不可用 / 容器不健康 **只 WARNING 不阻断**，db / redis 健康即继续。

### 2.3 stop.ps1

| 开关 | 作用 |
| --- | --- |
| `-DownContainers` | 一并 `docker compose down`（**不带卷删除参数**；命名卷与数据保留） |

- 先发关闭信号（`taskkill` 不带 `/F`）→ 等待 → 超时强杀（`/T` 覆盖子进程树；
 uvicorn `--reload` 的 reloader+worker、next 的服务子进程均在其中）；
- **PID 复用防护**：记录启动时刻，对不上则不杀（防误杀他人进程）；
- 残留端口归属判定：记录 PID + 祖先链 + 可执行文件路径三重识别；
- 若有 `-Watch` 监控在跑：先写停止请求（监控不再重启），再等其退出（超时强杀）；
- 结束后清理 `logs\run-*.json` 与 `stop.request`，并逐端口确认释放。

### 2.4 status.ps1（只读报告）

`powershell -ExecutionPolicy Bypass -File scripts\ops\status.ps1`
（`-NoHealth` 跳过健康检查）。

输出：容器健康（db/redis/dailyhot，dailyhot 标注容错）/ 前后端进程与端口 /
`-Watch` 状态 / 健康检查全文 / 计划任务注册状态（可撤销项）/ 最近日志清单。
**输出只使用项目相对路径，不回显绝对路径与密钥。**

---

## 三、常驻监控与崩溃自愈（-Watch）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\ops\start.ps1 -Watch
```

- 前台监控循环（每 5 秒巡检一次）；被监控进程异常退出 → **按退避自动重启**
 （等待序列 2 → 5 → 10 → 20 → 30 → 60 秒封顶，此后固定 60 秒）；
- 重启计数、原因写入 `logs\watch-*.log` 并更新 `run-*.json` 的 `restart_count`；
- 控制台每 60 秒打印一次心跳（PID 与重启次数）；
- **手动 stop 不重启**：`stop.ps1` 先写 `logs\stop.request` 标记 → 监控看到标记退出，
 不会把刚杀掉的进程再拉起来；
- 关闭方式：双击 `stop.bat`（推荐），或关闭监控窗口（Ctrl+C；服务进程会保留，需再跑 stop 清理）。

> 实测记录（本机 2026-10-13）：`taskkill /T /F` 强杀后端 → 约 5 秒内被监控检测到 →
> 等待 2 秒重启 → 新 PID 端口重新监听、健康检查恢复；`restart_count=1` 与日志行完整落盘。

---

## 四、开机自启（可选，默认不启用，可撤销）

```powershell
# 注册（登录后延迟 45 秒自动 start，不弹浏览器；窗口完全隐藏）
powershell -ExecutionPolicy Bypass -File scripts\ops\install-autostart.ps1
# 附带崩溃自愈（监控常驻）：
powershell -ExecutionPolicy Bypass -File scripts\ops\install-autostart.ps1 -Watch

# 撤销（删除即复原）
powershell -ExecutionPolicy Bypass -File scripts\ops\uninstall-autostart.ps1
```

- 实现方式：**任务计划程序**（`\xhs-agent\autostart`），登录触发器 + 延迟（默认 45 秒，
 给 Docker Desktop 留启动时间）+ `-AutoDocker -NoBrowser`；
- **取舍说明（计划任务 vs 启动文件夹）**：计划任务窗口隐藏、支持延迟与常驻监控、
 可在「任务计划程序」中随时查看/停用；启动文件夹方式更简单但登录时会闪控制台窗口、
 无延迟能力——本工程采用计划任务，未采用启动文件夹；
- 实测提示：登录触发器**必须绑定具体用户**（不带 `-User` 的「任意用户」触发器需要管理员权限，
 普通权限会被系统拒绝——本脚本已按当前用户绑定，实测非管理员注册/卸载均成功）；
- 卸载 = 删除该任务（回读确认无残留）；**不改注册表、不改任何其他系统设置**。

---

## 五、数据备份（backups\）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\ops\backup.ps1
powershell -ExecutionPolicy Bypass -File scripts\ops\backup.ps1 -RetentionCount 14
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `-OutputDir` | `backups` | 输出目录（相对项目根或绝对路径） |
| `-RetentionCount` | `7` | 保留最近 N 份（按文件名时间戳倒序），更旧自动清理并打印清单 |
| `-Database` | `xhs_agent` | 备份目标库 |

流程与保证：

1. 先检查 Docker 引擎与 `xhs-db` **healthy**（不健康拒绝备份，退出码 3）；
2. 容器内 `pg_dump -U postgres -d xhs_agent -Fc`（自定义压缩格式，可用 `pg_restore` 恢复）；
3. 容器内先做**完整性校验**：文件非空 + `pg_restore -l` 可列出归档条目
 （拒绝产出无效备份，退出码 1）→ 再整体拷出（二进制不经过管道，零截断风险）；
4. 结果追加 `logs\backup-history.log`（时间 / OK·FAIL / 文件 / 大小 / 保留与清理数）。

> ⚠️ `backend\.env` 含密钥，**默认不纳入备份**；如需单独备份请自行加密保管。
> ⚠️ 本脚本只读数据库、只写 `backups\` 与 `logs\`，绝不删除数据卷。

---

## 六、数据恢复（restore.ps1）

```powershell
# ① 验证性恢复（推荐先跑）：恢复到【临时验证库】，不触碰 xhs_agent
powershell -ExecutionPolicy Bypass -File scripts\ops\restore.ps1 -File backups\xhs_agent-20261013-210000.dump
# ② 验证 + 立即删除临时库
powershell -ExecutionPolicy Bypass -File scripts\ops\restore.ps1 -File backups\....dump -DropVerifyDb
# ③ 覆盖主库（危险操作：先停后端，再二次确认）
powershell -ExecutionPolicy Bypass -File scripts\ops\restore.ps1 -File backups\....dump -Force
```

| 参数 | 说明 |
| --- | --- |
| `-File` | 备份文件（相对项目根或绝对路径；不存在 → 退出码 2） |
| `-TargetDatabase` | 自定义目标库名（默认 `xhs_restore_verify_<时间戳>`） |
| `-Force` | **允许覆盖主库**（默认缺省时改为临时库；`-Force` 后需输入 `YES` 确认，脚本化再加 `-Yes`） |
| `-DropVerifyDb` | 验证完成后立刻删除临时库（覆盖主库模式下不适用，会被忽略） |

恢复后自动核对（任一结构项不过 → 退出码 1）：

- public 表数量（≥11：10 业务 + `alembic_version`）、`vector` 扩展、langgraph 框架表数；
- **9 张关键表行数与源库比对**（源库同时在被写入时可能有不一致，属提示级，非失败）。

> 安全设计：默认**绝不触碰** `xhs_agent`；覆盖主库需 `-Force` + 二次确认，且覆盖前对现库
> 执行带连接强制的 `DROP DATABASE`（不可恢复，务必先自行停写、确认备份新鲜度）。

---

## 七、定时备份任务（可选，默认不启用，可撤销）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\ops\install-backup-task.ps1 # 默认每天 03:30
powershell -ExecutionPolicy Bypass -File scripts\ops\install-backup-task.ps1 -DailyAt 04:15
powershell -ExecutionPolicy Bypass -File scripts\ops\uninstall-backup-task.ps1 # 撤销
```

- 任务名 `\xhs-agent\backup`（当前用户、仅登录时运行——Docker Desktop 属于用户会话）；
- **coalesce 语义**：已启用 `StartWhenAvailable` —— 休眠 / 关机错过时点后，
 唤醒 / 登录后**补跑一次**；失败不弹窗打扰，结果写 `logs\backup-history.log`；
- 单次执行上限 30 分钟；重复实例忽略（`IgnoreNew`）；
- 撤销只删「定时触发」这一层，**已生成的 backups 文件不会被删除**。

---

## 八、日志位置一览（logs\）

| 文件 | 内容 |
| --- | --- |
| `backend-<时间戳>.log` / `.err.log` | 后端（uvicorn）stdout / stderr |
| `frontend-<时间戳>.log` / `.err.log` | 前端（next dev / next start）stdout / stderr |
| `frontend-build-<时间戳>.log` | 生产构建输出（构建失败时排查用） |
| `watch-<时间戳>.log` | 常驻监控：重启原因、次数、结果 |
| `backup-history.log` | 备份历史（追加）：时间 / 结果 / 文件 / 大小 / 保留数 |
| `run-backend.json` / `run-frontend.json` | 运行状态：PID / 端口 / 模式 / 日志路径 / 启动时刻（相对路径） |
| `run-watch.json` | 常驻监控进程状态 |
| `stop.request` | 停止请求标记（仅存在片刻；stop 与监控之间握手用） |

> `logs\` 全部为运行时产物，可随时整体删除（不影响数据）；`backups\` 请按需保留。

---

## 九、环境自检（selftest_ops.py）

```powershell
backend\.venv\Scripts\python.exe scripts\selftest_ops.py
```

离线、可重复、不破坏真实数据；会短暂启动/停止前后端（-Dev、不点浏览器）、
注册/卸载同名计划任务后复原；**不联网、不消耗 token**。覆盖：

0. 静态检查：12 个 `.ps1` 全量可解析；ops 脚本与 bat 无真实密钥串 / 卷删除字样 /
 外部托管软件名 / 平台域名 / `.env` 回显；bat 为纯 ASCII；共享库函数与开关齐全；
1. 环境自检零副作用（缺 `.env` / 缺 venv / 缺依赖 / 非工程目录 四种伪根目录）；
2. 构建判定与 HTTP 探活的受控分支（含「不可达 → 优雅返回 false」）；
3. 启停往返：启动 → 健康全绿 → 幂等复核 → stop → 端口 / 状态文件回收；
4. 备份 → 恢复往返：真实备份 + 完整性校验 + 保留清理 + 临时库行数比对 + 负例；
5. 计划任务注册 / 卸载往返（自启 + 定时备份，含延迟与负例校验）；
6. `-Watch`：模拟崩溃 → 退避重启恢复 → stop 联动收尾；
7. 收口修复验证（组合 PATCH 单事务 / 重向量化失败降级 / 白名单负断言 / 热点去重）；
8. 零迁移断言（versions 恰 2 支 / `0002 (head)` / 零漂移 / 表数与名单）。

---

## 十、常见故障排查表

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| `start` 退出码 2：提示复制 `.env.example` | `backend\.env` 缺失 | 手动 `copy backend\.env.example backend\.env` 并填写（脚本不代填） |
| 退出码 3：Docker 引擎未运行 | Docker Desktop 未启动 | 手动启动；或重跑加 `-AutoDocker` |
| 退出码 3：db / redis 起不来 | 镜像缺失 / 网络受限 | 查看提示输出；运行 `scripts\install-docker.ps1`；必要时 `docker compose pull` |
| 退出码 4：健康检查超时 | 服务起没起来 / 配置错 | 看 `logs\` 最新 `backend-*.err.log`；确认 `.env` 数据库口令；`stop` 后重试 |
| 退出码 5：迁移未到 head | 库结构落后 | 重跑加 `-Migrate`，或 `cd backend` 手动 `alembic upgrade head` |
| 前端页面「共 0 条」/接口 503 | 代理劫持了 localhost | 一键脚本已自动清洗代理；手工启动时需同样 `unset` 代理变量（见工程 README 环境说明） |
| `dailyhot` 不健康 | 官方镜像拉取受网络限制 | 只影响热搜刷新；核心功能不受影响（容错已覆盖） |
| 端口 8000/3000 被外部进程占用 | 手动启动过旧服务 | `stop.bat` 后重试；仍占用则用任务管理器结束对应进程 |
| 计划任务注册「拒绝访问」 | 系统权限限制 | 以管理员身份运行安装脚本；或确认登录触发器已绑定具体用户（脚本已处理） |
| 恢复时目标库已存在 | 重名临时库 | 换 `-TargetDatabase`，或先手动删除该库 |

---

## 十一、可撤销项与清理说明

| 项 | 注册位置 | 撤销方式 |
| --- | --- | --- |
| 开机自启 | 计划任务 `\xhs-agent\autostart` | `uninstall-autostart.ps1`（删除即复原，回读确认） |
| 定时备份 | 计划任务 `\xhs-agent\backup` | `uninstall-backup-task.ps1` |
| 运行日志 | `logs\` | 直接删除目录（运行时产物） |
| 备份文件 | `backups\` | 按需保留；保留策略自动滚动 |
| 常驻监控 | `logs\run-watch.json` 记录的进程 | `stop.bat`（联动结束） |

> 任务计划程序中的任务文件夹 `\xhs-agent\` 会在两个任务都卸载后自动清理（脚本回读确认）。

---

## 十二、口径标注（此前「收口确定性小缺陷」评估结论）

| 项 | 评估结论 | 处理 |
| --- | --- | --- |
| covers PATCH 多字段原子性 | 普通字段与互斥选中原为**两次提交**，存在半成品窗口 | **已合并为单事务**（`crud_cover.update_with_selection`）；互斥语义与 end-state 不变，`selftest_cover` 79 项零回退 + `selftest_ops` 组合 PATCH 断言 |
| knowledge PATCH 多字段原子性 | status 与字段原为**两次提交**（`set_status` 一次、`update_fields` 一次） | **已合并为单事务**（`crud_knowledge.apply_admin_update`）；status 仍只经显式参数传入（白名单语义保留，负断言背书）；重向量化失败「置空向量 + 正文不丢」语义逐字保留，`selftest_knowledge_admin` 156 项零回退 |
| 选题显式 `hotspot_ids` 重复 | 原实现重复 id 会重复进提示词、挤占 `TOPIC_MAX_HOTSPOTS_PER_REQUEST` 名额 | **已按首次出现去重（去重先于截断）**，`missing` 同样去重；无重复输入的既有行为不变，`selftest_topic_agent` 42 项零回退 |
| agent 图 `parse_attempts` 合并口径 | 核对结论：harness 基座 `agent/state/base.py::merge_parse_attempts` **本就是「取 max」口径**（「反映最差情况，便于发现不稳定」）；且属 harness 基座（本次零改动约束） | **维持现状，不改**（无需修改即已满足取 max 语义） |
| 剪贴板兜底 / Windows 换行 | 此前 E2E 已对剪贴板断言做 `\r\n` 归一化；属测试侧问题，产品代码无缺陷 | 仅记录，不改 |
| 违禁词 snippet 归一化视图 | `crud.list_active_snippets` 已做 `ChunkType` 归一（`.value`）；前端 forbidden_word 行展示类别 | 仅核对，不改 |

> 以上为**纯逻辑、可离线验证**的评估面；需要真实 key / 真实模型的「未验证项」不在本次伪装完成。

---

## 十三、未验证项（如实标注）

1. **开机自启在真实重启后的效果**：注册/卸载往返已在脚本层面实测（回读可查/查无），
 但「重启电脑 → 登录 → 任务真实触发」需用户本机观察确认；
2. **计划任务在系统休眠 / 唤醒后的实际触发**（StartWhenAvailable 补跑）需用户本机观察；
3. **真实数据的备份 → 恢复**：本次自检只对**临时验证库**做完整往返（已过 9 表行数比对）；
 建议用户在非高峰执行一次真实备份，并用默认（验证库）模式核对一次；
4. **dailyhot 官方镜像**在网络受限时仍可能不可用（容错已覆盖，不影响主流程）；
5. 前端生产构建在本次未由 start.ps1 真实触发（按需构建分支经函数级判定 + 既有构建
 回归覆盖；本机 `.next` 为最新、走了跳过分支）。

## 十四、本次不做（边界）

- 公网部署 / 容器化前后端 / 反向代理；
- 自动安装依赖、自动填 key、自动改 `.env`；
- 引入 PM2 / NSSM / Nginx / 外部 supervisor（红线）；
- 数据库物理结构变更（零迁移；`migrations/versions` 仍恰 2 支）；
- harness 基座与既有六图拓扑的任何改动。
