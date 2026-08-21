# Urban Dossier 项目完整性审计（2026-08-20）

## 结论

当前仓库已经达到“本地工作站可运行、核心分析可验证、生产 agent 边界明确”的状态。数据、评分、双点对比、地图与 Vega 图表、OpenClaw agent、报告/海报入口均有实际接线；本轮修复后，完整 Python 测试、前端构建与浏览器烟测、数据发布门禁、依赖审计和在线五段健康检查均通过。

它还不是无条件的正式发布完成态。主要剩余缺口是：报告 scripts 模式引用的 SymGen resolve/verify 实现不在仓库中；CI 目前只做 Python 语法和非阻断 shellcheck，没有执行本轮本地通过的行为测试；GPU/cuML、DGX 和 mac profile 未在本轮跨硬件复测。

## 审计范围与结果

| 领域 | 实际检查 | 状态 |
| --- | --- | --- |
| 原始数据 | 18 个正式 CSV + 1 个 walking-graph auxiliary；列、行数、严格解析 | 通过 |
| Ready 发布层 | 46 个必需 Parquet + 1 个 auxiliary；压缩、行组、分数、坐标、季度、文件清单 | 通过 |
| 算法 | 评分覆盖、趋势、热点、H3 半径、情景模拟、比较差值、图表规格 | 通过；保留方法学限制 |
| 对比功能 | 双点 B-A 分数、Vega 对比图、GeoJSON 差值地图、加载/失败/取消状态 | 通过 |
| 可视化 | MapLibre 图层顺序、相机、四类 Vega 图、双变量图、20 期时间线、离线导出 | 通过 |
| Agent 链路 | FastAPI → OpenClaw Gateway → model → tools → evidence → answer | 在线实测通过 |
| Tools / Skills | 7 个 analyst tools、7 个项目 SKILL、可用性门禁、语料与文档契约 | 通过 |
| 报告提示词 | history 边界、reasoning 隔离、HTML 转义、OpenClaw fail-closed、计数/尺度说明 | 通过；SymGen 缺口见风险 |
| 依赖与结构 | 顶层/前端依赖引用、脚手架残留、死组件、安全公告 | 已精简，audit 0 |

## 架构设计问题清单

下表描述的是设计债务与发布约束，不是测试结果的重复表述。“部分缓解”表示已有保护或文档，但根因仍存在；行为测试通过只能证明当前受测路径未回归，不能把开放问题自动改写为已解决。

| 级别 | 问题 | 影响 | 状态 |
| --- | --- | --- | --- |
| 高 | `/api/agent/ask` 与 report/poster/refine 形成两套编排：前者走 OpenClaw agent loop，后者还保留 scripts/OpenClaw 分支、独立提示词与验证逻辑。 | 工具选择、history、安全边界、错误语义和证据规则可能漂移；同一会话在 ask 与报告输出中可能采用不同事实链路。 | **开放**：本轮收紧了两条路径的输入与 fail-closed 行为，但尚未统一编排内核。 |
| 高 | scripts 报告路径声明依赖 SymGen 的 `resolve_symgen.py` / `verify_narrative.py`，实现并不在仓库。 | 无法复现所声称的确定性数字解析与叙事核验；部署到新环境时会直接缺模块，或退化为没有同等级 grounding 的生成。 | **开放/发布阻断**：应补真实实现与契约测试，或删除 SymGen 声明并降低产品承诺。 |
| 高 | 数据契约分散在 metric registry、预处理脚本、provider 表映射、validator 常量和文档中；部分 ready legacy 表没有逐文件、含输入哈希的完整 manifest。 | 新增或更名发布物容易出现“数据有效但门禁误报”或“文件存在但方法/输入已陈旧”；不能仅凭 Parquet 可读证明可发布。 | **部分缓解**：本轮修复 validator 清单漂移，NYCCAS、HVI、敏感性等新产物已有 publication gate；统一 catalog 和 legacy manifest 迁移仍开放。 |
| 高 | CI 没有行为门禁，只做 Python compile，shellcheck 仍非阻断。 | 本地通过的评分、API、agent、前端构建和浏览器烟测不会在合并时自动阻止回归。 | **开放/发布阻断**：本轮测试结果是一次性证据，不等于 CI 缺口已解决。 |
| 高 | backend 通过修改 `sys.path` 直接依赖仓库 `skills/` 下的 Python 包。 | 包边界、安装方式和运行目录耦合；wheel/container 或目录重组后可能 import 失败，也难以独立版本化与测试。 | **开放**：当前路径可运行，但尚未改成正式 package/dependency contract。 |
| 中 | `SkillDataProvider` 仍是占位实现。 | 显式 `data_mode=skill` 不具备生产能力；`auto` 回落可能掩盖用户以为 skill 数据路径已启用的事实。 | **开放**：当前 fail/fallback 行为受测；应实现后再公开，或删除该模式。 |
| 高 | `AGENT_BACKEND` 在两处入口的默认值相反，一处默认 scripts，另一处默认 nemoclaw。 | 同一套环境变量缺省部署可能走不同网络边界、模型和工具链；排障及安全审计结果不可移植。 | **开放**：本轮只强化显式模式下的 fail-closed，默认值尚未统一为单一配置源。 |
| 中 | Agent session 仅保存在进程内存。 | 多 worker 间不可见，重启即丢失；负载均衡下 history/report 可能落到不同会话，无法提供可靠续聊或审计留痕。 | **开放**：前端切点/改半径时的 session 串线已修，但持久化和多进程一致性未解决。 |
| 中 | `server.js`、`DirectQueryDataProvider` 和前端 `Map` 仍是超大单体。 | 数据查询、缓存、地图状态和接口逻辑高度耦合，修改影响面大，局部测试与所有权边界不清，重复/死代码更易累积。 | **开放**：本轮只做安全的局部清理；未进行大规模拆分。 |
| 中 | Pydantic 请求/响应模型与 TypeScript 类型和消费逻辑手工双写。 | 字段新增、nullable、枚举或错误响应容易只更新一端；编译通过不能验证运行时 payload 完全一致。 | **开放**：已有若干契约测试，但尚未由 OpenAPI/codegen 生成单一类型来源。 |
| 中低 | `PROJECT_PLAN`、`EXPANSION_PLAN`、部署文档和历史版本说明同时存在，部分“计划中/已完成”表述未随代码同步。 | 新维护者可能把历史意图当成当前事实，重复实现已退休功能，或基于过期数据量、工具数和方法版本做决策。 | **部分缓解**：本轮修正若干数字和入口说明；仍需给历史文档加状态/版本横幅并建立当前事实索引。 |

## 算法与方法学问题清单

| 级别 | 问题 | 影响 | 状态 |
| --- | --- | --- | --- |
| 高 | 所有 0–100 数值是归一化综合指数，不是概率、风险率、评级置信度或“百分之多少安全”。 | 若报告或 UI 使用概率语言，会造成错误的绝对风险解释；不同方法版本的 70 分也不天然可比。 | **部分缓解**：registry、方法版本、报告提示和 uncertainty 已披露尺度；所有下游文案仍需持续门禁。 |
| 高 | H3 单元、任意点半径、ZIP 和 NTA 被放在同一产品界面比较，存在 MAUP 与生态谬误。 | ZIP/NTA 聚合关系不能推断到具体建筑或个人；改变边界或空间粒度可能改变排名和相关性。 | **开放且已披露**：grain metadata 保留原始尺度，但尚无跨粒度可比性校正或专门敏感性分析。 |
| 高 | 指标混合 rolling days、季度、年度普查和不定期基础设施快照。 | 综合分可能把不同 as-of 的条件当成同一时点；“趋势”也不能覆盖没有时间序列的静态指标。 | **开放**：部分指标记录 vintage/temporal grain，季度完整性已修；统一 as-of/freshness contract 尚缺。 |
| 高 | prepared percentile scores 与 runtime fallback 公式是两套数值路径。 | 数据发布门禁失败或表缺失时，不只精度下降，数值定义也会改变；相同地点可能因部署状态得到不同分数。 | **部分缓解**：coverage/source 会披露路径，本轮修复 collision fallback 漏算；双路径本身仍开放，建议退役 fallback 或明确标为 degraded result。 |
| 高 | 缺失 category 时 overall 会在剩余 category 上重归一。 | 一个只依赖 safety 的 overall 与三类齐全的 overall 都可能显示 70，但证据范围不同，跨地点/时点直接比较不成立。 | **部分缓解**：`score_coverage` 和 `effective_ratio` 已发布，敏感性分析覆盖缺失规则；headline 仍保留重归一分数，方法学决策未关闭。 |
| 高 | comparison delta 只传播点估计，没有传播两点 uncertainty、coverage 差异或差值区间。 | 小幅 B−A 差值可能被展示为确定差异，即使两个分数区间高度重叠或证据覆盖不同。 | **开放**：方向和色标由服务端锁定，但不能据此声称差异显著。 |
| 中 | 半径查询以 H3 cell 中心是否落入半径选单元。 | 点或半径轻微移动会使边缘单元整块进入/退出，产生阶跃；这不是连续的真实影响范围。 | **开放**：本轮让 scenario 与点评分采用同一规则，解决的是一致性，不是边缘跳变本身。 |
| 中 | hotspot 只使用 detail payload 中最多 60 个 `map_points`。 | 密集区域的截断样本可能改变 DBSCAN 簇数量、主类型与半径，不能视为全量空间统计。 | **部分缓解**：本轮修复坐标源、事件过滤、非法坐标和米制距离；60 条上限仍开放。 |
| 高 | intervention elasticity 是观察数据上的相关关系，不是因果效应。 | “增加一个设施后分数提高 X”可能混入选址、密度和其他共同原因，不能作为政策效果预测。 | **开放且已披露**：响应标记 `causal=false` 并给 fit/caveat；在没有因果设计前不得提升措辞。 |
| 高 | raw audit 对多数正式 CSV 只检查列、行数与可解析性，没有逐文件 hash、来源 snapshot ID、as-of 和 freshness SLA。 | 上游内容可在文件名和行数相同的情况下变化；门禁无法证明数据来源、时点或是否过期，结果难以完整复现。 | **部分缓解**：本轮修复 audit 状态、退出码和 auxiliary 模型；内容身份与新鲜度契约仍开放。 |
| 高 | Agent eval 以路由和响应契约为主，语义事实性、证据充分性、数字一致性和拒答质量门禁不足。 | schema 与工具调用均通过的回答仍可能错误归因、过度概括或给出证据不支持的建议。 | **开放**：现有 19-case/确定性 routing 结果不代表语义质量问题已解决，需要固定模型与人工标注质量集。 |
| 中 | transient tool error 缺少统一、确定性的重试策略。 | 网络抖动、Gateway 短暂失败或读超时时，相同请求可能随机失败；模型自行重试还可能重复昂贵调用或产生不同轨迹。 | **开放**：个别在线样例发生过重试并成功，但尚无按错误类型、次数、退避和幂等性定义的公共策略。 |
| 高 | GPU/cuML 与不同模型、DGX/mac/CPU fallback 的跨配置结果未验证。 | 聚类标签、数值精度、模型工具选择和输出质量可能随硬件/版本变化；当前工作站测试不能外推到发布矩阵。 | **开放/发布前验证项**：CPU 与当前模型路径受测；需固定数据、随机种子、依赖版本和模型质量集执行硬件矩阵。 |

## 数据快照

- Raw：18/18 正式数据集有效，82,376,420 行，33,575,088,233 bytes；`transit/nyc_street_centerline.csv` 明确归为 auxiliary。
- Ready：46/46 必需发布物有效，另有 `transit_risk_scores_h3.parquet` auxiliary；合计 47 个文件、48,256,753 行、261,091,401 bytes。
- 两个门禁均返回 `status=ok`；missing、unexpected、invalid、partial 均为 0。
- 未修改或删除任何数据文件。

## 本轮修复与清理

### 数据与算法

- 修复 raw audit 即使失败也退出 0 的门禁缺陷；unexpected 文件现在参与失败判定，已知 auxiliary 单独呈现。
- 补齐 ready validator 对 population、provenance、HVI、NYCCAS 四个正式产物的登记。
- 缺失/非有限趋势值不再被当成 0，从而避免伪造正负 100% 趋势。
- 修复 prepared score 不可用时碰撞数据落在 legacy `transit` payload、却没有进入 safety fallback 的问题。
- 热点改用带坐标的 `map_points`，过滤非事件和非法坐标；DBSCAN 改为本地米制投影，修复 NYC 东西方向约 24% 的距离偏差。
- 情景模拟复用点评分的精确 H3 半径选择，不再把 `grid_disk` 边角越界单元算入。
- 数据路径配置始终返回 `Path`，移除 import-time 建目录副作用和无数据 worktree 收集崩溃。

### 对比与可视化

- 对比 hook 新增 loading/error/abort/reset；服务端响应未到或失败时显示已加载的 pinned/current 快照，不再整块显示 `--`。
- 服务端 `chart_specs` 和 `delta_map` 返回后仍作为唯一计算真值接入 Vega 和地图。
- Agent session 绑定坐标与半径，切点或改半径后重建 panel，避免旧聊天、报告和异步 session 串入新位置。
- 删除 4 个全仓零引用 UI 组件和无用 CSS imports。

### Agent、tools、skills 与报告

- `/api/agent/ask` 仅接受 user/assistant history，最多 20 条、每条 4,000 字符；loop 二次过滤，拒绝伪造 system/tool 消息。
- `reasoning` / `reasoning_content` 只保留在 trace，不再作为截断响应的用户答案。
- 报告 Markdown 和 refine feedback 在生成 HTML 前转义，关闭原始 HTML/XSS 路径。
- 报告 skill 默认路径由失效的 `~/xhh_code` 改为仓库内 `skills/`。
- report/poster/refine 在 `nemoclaw` 模式下 fail closed；只有显式 `AGENT_BACKEND=scripts` 才可访问 host vLLM。
- 修正文档中的 7 tools、18 datasets、0–100 分数尺度、19 service eval cases，并收窄数据准备 skill 的触发描述。

### 结构与依赖

- 顶层移除无引用的 `@mapbox/mbtiles` 和 Leaflet bridge；把 standalone 页面真实使用的 `maplibre-gl` 从隐式依赖改为直接依赖。
- 前端移除未使用的 shadcn/MCP/Hono/Express、Leaflet/react-leaflet、dotenv、重复 Vega 包和脚手架依赖；依赖总数由 650 降至 289。
- 前端包名由 `react-example` 改为 `urban-dossier-frontend`；Vite 收敛为 dev-only 7.3.6。
- 生产 CSS 由 152.64 KB 降至 130.32 KB，main JS 由 503.48 KB 降至 490.70 KB。
- 顶层与前端完整 npm audit 均为 0 漏洞。

## 验证证据

- Python：`453 passed`，仅 1 个 Starlette/httpx2 迁移 warning。
- 前端：TypeScript、生产 build、layer order、camera smoke、chart smoke、offline export、methodology tests 全通过。
- 浏览器烟测：4 个 Vega 图；comparison delta 5 features / 4 layers；985 个双变量 features；20 个 timeline periods；离线导出 3 charts、0 external requests。
- 在线健康：vLLM、OpenClaw、FastAPI、agent status、Node/frontend 5/5。
- 在线 agent：5 次迭代，3 次地址检索（含重试）+ 1 次比较工具，返回 4 条 evidence 和带 500 m 半径的 grounded 回答。
- Agent eval：service corpus 19 cases 契约通过；deterministic routing 4/4。
- Python 环境：87 packages compatible。
- 格式：`git diff --check` 通过。

## 剩余风险与建议顺序

1. **高：报告 grounding 实现缺失。** `agent_service.py` 的 scripts 模式引用 `resolve_symgen.py` / `verify_narrative.py`，但仓库没有这两个实现；生产 OpenClaw 报告路径也没有同等级的确定性数字核验。发布报告功能前应补齐一个真实、测试覆盖的 grounding 实现，或删除所有 SymGen 声明并把报告明确标成仅基于所给 evidence 的模型摘要。
2. **高：CI 不执行行为测试。** `.github/workflows/lint.yml` 目前只做 Python compile，shellcheck 还被 `|| true` 设为非阻断。本轮 453 个 Python tests、前端 build/smoke、Node contracts 和无数据 agent eval 应分层进入 CI；依赖真实数据/浏览器/服务的测试可单独作为 workstation release gate。
3. **中：`SkillDataProvider` 是占位实现。** `data mode=skill` 会失败，`auto` 会安全回落 direct。若不计划支持，应删除该模式；若计划支持，应先完成实现和契约测试再公开。
4. **中：热点输入最多 60 个 map points。** 密集区域会被截断，适合 UI 提示而不是全量空间统计；应为热点提供独立、可分页的数据查询。
5. **中：情景模拟是相关性模型。** Intervention elasticity 不能表述为因果预测，UI、报告和提示词必须继续保留此限制。
6. **中低：可访问性与 bundle。** Modal 缺完整 focus trap/Escape，部分 icon-only 按钮缺 aria-label；Vega embed 约 855 KB、MapLibre 约 1.05 MB，已分 chunk 但仍值得按路由动态加载。
7. **环境覆盖：** GPU/cuML、DGX Spark、mac profile 未在本轮实际运行；正式发布前分别执行硬件矩阵和固定模型质量集。

## 发布判断

- 本地演示/工作站日常分析：**可用**。
- 数据与算法发布门禁：**通过当前快照**。
- 前端对比与可视化：**可用且已回归**。
- Agent ask：**生产边界内可用**。
- 报告/海报正式对外发布：**有条件可用**，需先决定并落实 grounding 契约。
- 跨平台正式 release：**尚未完成**，受 CI 与硬件矩阵缺口约束。
