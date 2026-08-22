# Urban Dossier 项目完整性审计（2026-08-20）

> **修订 2026-08-20（第二版）**：初版写成时，它所描述的 37 个文件改动尚未提交，
> `453 passed` 这类证据只存在于单个工作区，随时可能丢失。这些改动已提交为
> `fd5215b`，本报告与它同一提交，因此下文每条证据现在都可以按 commit 复现。
> 第二版另外做了三件事：逐条复核了初版的断言并标注复核结果；把机制描述不准的
> 条目改准（`SymGen`、`AGENT_BACKEND`）；补入初版写成后才测到的证据（真实模型
> 评测、pass^k 指标缺陷、fault 用例自相矛盾），以及初版完全没有覆盖的 RAG 退役。
> 复核方法与未覆盖范围见文末「复核记录」与「本报告未覆盖」。

## 结论

当前仓库已经达到“本地工作站可运行、核心分析可验证、生产 agent 边界明确”的状态。数据、评分、双点对比、地图与 Vega 图表、OpenClaw agent、报告/海报入口均有实际接线；本轮修复后，完整 Python 测试、前端构建与浏览器烟测、数据发布门禁、依赖审计和在线五段健康检查均通过。

同日稍早（`52555ff`）RAG 子系统被整体退役：`rag/` 包、`retrieve_dataset_docs`
工具、`embeddings` vLLM 服务与 6 个相关评测用例一并删除。本报告中的「7 个 tools」
「19 个 service eval cases」都是退役后的数字。判据是实测——15 个可查数据集的完整
真实 schema（106 列，从 Parquet 生成）约 400 token，上下文窗口 32,768；而
`rag/catalog.json` 的 dataset id 与 `query_dataset` 实际接受的**零重叠**，建索引会
把 agent 引向不存在的标识符。重启该话题的门槛记录在 README「RAG: retired」。

它还不是无条件的正式发布完成态。主要剩余缺口是：报告路径引用的 SymGen
resolve/verify 实现不在仓库中且**静默降级**；CI 目前只做 Python 语法和非阻断
shellcheck，没有执行本轮本地通过的行为测试；GPU/cuML、DGX 和 mac profile 未在本轮
跨硬件复测。

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

**级别读法（第二版新增）**：初版 24 条里有 16 条标 `高`，占三分之二，排序信息因此
接近于零，而文末「建议顺序」其实只排了 7 项——作者心里有更细的优先级，表格没表达
出来。第二版把 `高` 拆成两档，使表格与建议顺序对齐：

- **阻断**：在正式对外发布前必须关闭，否则产品承诺无法兑现或回归无人拦截；
- **高**：真实设计债务，但不阻断当前的工作站/演示用途。

下表描述的是设计债务与发布约束，不是测试结果的重复表述。“部分缓解”表示已有保护或文档，但根因仍存在；行为测试通过只能证明当前受测路径未回归，不能把开放问题自动改写为已解决。

| 级别 | 问题 | 影响 | 状态 |
| --- | --- | --- | --- |
| 中 | `/api/agent/ask` 与 report/poster/refine 仍是两套编排，但**第二套的分支已在 2026-08-22 收敛为一条**：scripts 模式、direct-vLLM 客户端（`_get_openai_client` / `_llm_chat` / `_llm_chat_multi`）与 SymGen 一并删除，`agent_service` 现在没有自己的模型客户端，所有模型调用都经 OpenClaw 网关进沙箱——沙箱边界从条件判断变成结构性事实。剩余差异是提示词与产物形态不同。 | 工具选择、history、安全边界、错误语义和证据规则可能漂移；同一会话在 ask 与报告输出中可能采用不同事实链路。 | **开放（已降级 高→中）**：分支已收敛、边界已结构化，但两套提示词与两种产物形态仍未统一到一个编排内核。 |
| ~~阻断~~ | 报告路径声明依赖 SymGen 的 `resolve_symgen.py` / `verify_narrative.py`，实现并不在仓库（导入自不存在的 `skills/blocksense-report/scripts`）。 | **不是崩溃，是静默降级**：两个调用点（`agent_service.py:690`、`:799`）都有 `if resolve_symgen is None: return` 守卫，导入失败只记一条 `logger.warning` 且不再重试，报告照常产出，产物里没有任何字段标明 grounding 未执行。对一个防幻觉管线而言，这比 ImportError 更危险——下游无法区分「已核验」和「跳过核验」。 | **已关闭 2026-08-22**：选择了删除。`resolve_symgen` / `verify_narrative` 及其整条 direct-scripts 报告路径已移除（`agent_service.py` 1,208 → 768 行）。查证发现问题比初版描述的更实：提示词第 261 行要求模型对**所有**数字使用 `{{ref}}` 占位符并禁止裸数字，而解析器从不加载——该路径产出的报告里留着字面的 `{{rodent_count}}`，不是「少了核验」而是「数字没被填进去」。同时新增 `GROUNDING_NONE`：report/poster/refine 的每个成功响应都带 `grounding` 字段、HTML 页脚都带说明，产物因此能自述是否经过核验。实测确认（真实报告：`grounding.verified=false`、页脚在、无占位符）。 |
| 高 | 数据契约分散在 metric registry、预处理脚本、provider 表映射、validator 常量和文档中；部分 ready legacy 表没有逐文件、含输入哈希的完整 manifest。 | 新增或更名发布物容易出现“数据有效但门禁误报”或“文件存在但方法/输入已陈旧”；不能仅凭 Parquet 可读证明可发布。 | **部分缓解**：本轮修复 validator 清单漂移，NYCCAS、HVI、敏感性等新产物已有 publication gate；统一 catalog 和 legacy manifest 迁移仍开放。 |
| ~~阻断~~ | CI 没有行为门禁，只做 Python compile，shellcheck 仍非阻断。 | 本地通过的评分、API、agent、前端构建和浏览器烟测不会在合并时自动阻止回归。 | **已关闭 2026-08-22**：新增 `python-tests`（423 个数据无关测试）与 `frontend`（tsc / build / 图层顺序 / 三个 node 契约）两个 job。分界是实测出来的：把数据根指向空目录跑一遍，得到 427 通过 / 22 失败，那 22 个集中在 5 个文件，现由 `needs_data` 标记管理（清单与理由在 `conftest.py`）。工作站门禁跑全部 461 个并加 `--require-data`——**跳过一个 data 测试即判失败**，因为「路径写错导致静默变绿」正是本项目踩过的坑。浏览器烟测需要活服务与真实瓦片，留在工作站门禁。shellcheck 维持非阻断且注明原因：本机没装 shellcheck，无法验证改成阻断会不会直接让 main 变红。 |
| 高 | backend 通过修改 `sys.path` 直接依赖仓库 `skills/` 下的 Python 包。 | 包边界、安装方式和运行目录耦合；wheel/container 或目录重组后可能 import 失败，也难以独立版本化与测试。 | **开放**：当前路径可运行，但尚未改成正式 package/dependency contract。 |
| 中 | `SkillDataProvider` 仍是占位实现。 | 显式 `data_mode=skill` 不具备生产能力；`auto` 回落可能掩盖用户以为 skill 数据路径已启用的事实。 | **开放**：当前 fail/fallback 行为受测；应实现后再公开，或删除该模式。 |
| 中 | `AGENT_BACKEND` 有两份定义且默认值相反：`config.py:77` 默认 `scripts`，`agent_service.py:114` 默认 `nemoclaw`。 | 复核后**影响小于初版描述**：`config.AGENT_BACKEND` 全仓无任何消费者（已 grep 确认，仅 `agent_service` 的那份被使用），而它恰好是不安全的那个默认值。因此当前没有部署会因此走错边界；风险是潜伏的——将来任何人 import `config.AGENT_BACKEND` 都会静默拿到 `scripts`。 | **开放**（复核 2026-08-20：降级 高→中，结论改为删除而非统一）：直接删除 `config.py` 中的死定义，比「统一为单一配置源」更小且更彻底。 |
| 中 | Agent session 仅保存在进程内存。 | 多 worker 间不可见，重启即丢失；负载均衡下 history/report 可能落到不同会话，无法提供可靠续聊或审计留痕。 | **开放**：前端切点/改半径时的 session 串线已修，但持久化和多进程一致性未解决。 |
| 中 | `server.js`、`DirectQueryDataProvider` 和前端 `Map` 仍是超大单体。 | 数据查询、缓存、地图状态和接口逻辑高度耦合，修改影响面大，局部测试与所有权边界不清，重复/死代码更易累积。 | **开放**：本轮只做安全的局部清理；未进行大规模拆分。 |
| 中 | Pydantic 请求/响应模型与 TypeScript 类型和消费逻辑手工双写。 | 字段新增、nullable、枚举或错误响应容易只更新一端；编译通过不能验证运行时 payload 完全一致。 | **开放**：已有若干契约测试，但尚未由 OpenAPI/codegen 生成单一类型来源。 |
| 中低 | `PROJECT_PLAN`、`EXPANSION_PLAN`、部署文档和历史版本说明同时存在，部分“计划中/已完成”表述未随代码同步。 | 新维护者可能把历史意图当成当前事实，重复实现已退休功能，或基于过期数据量、工具数和方法版本做决策。 | **部分缓解**：本轮修正若干数字和入口说明；仍需给历史文档加状态/版本横幅并建立当前事实索引。 |

## 算法与方法学问题清单

**状态读法（第二版新增 `恒定约束`）**：下表有四条永远不会变成「已完成」——0–100
不是概率、elasticity 是相关不是因果、混用空间粒度必然带 MAUP 与生态谬误、指标本身
就有不同 as-of。把它们记作「开放」会让审计的未关闭数永远有个 4 的下限，每轮重数一
遍。它们标为 **恒定约束**：需要的是持续的措辞纪律和防回退门禁，不是完工。


| 级别 | 问题 | 影响 | 状态 |
| --- | --- | --- | --- |
| 恒定 | 所有 0–100 数值是归一化综合指数，不是概率、风险率、评级置信度或“百分之多少安全”。 | 若报告或 UI 使用概率语言，会造成错误的绝对风险解释；不同方法版本的 70 分也不天然可比。 | **恒定约束**（第二版重分类）：registry、方法版本、报告提示和 uncertainty 已披露尺度。可关闭的部分只有「防回退」：为 UI/报告/提示词加一条禁用概率措辞的检查。 |
| 恒定 | H3 单元、任意点半径、ZIP 和 NTA 被放在同一产品界面比较，存在 MAUP 与生态谬误。 | ZIP/NTA 聚合关系不能推断到具体建筑或个人；改变边界或空间粒度可能改变排名和相关性。 | **恒定约束**（第二版重分类）：grain metadata 保留原始尺度。可关闭的部分是一次跨粒度敏感性分析——量化它有多大，而不是消除它。 |
| 恒定 | 指标混合 rolling days、季度、年度普查和不定期基础设施快照。 | 综合分可能把不同 as-of 的条件当成同一时点；“趋势”也不能覆盖没有时间序列的静态指标。 | **恒定约束**（第二版重分类）：数据源的更新周期不由本项目决定，混合无法消除。可关闭的部分是**统一 as-of/freshness contract**——让每个数字都能说出自己的时点。 |
| 高 | prepared percentile scores 与 runtime fallback 公式是两套数值路径。 | 数据发布门禁失败或表缺失时，不只精度下降，数值定义也会改变；相同地点可能因部署状态得到不同分数。 | **部分缓解**：coverage/source 会披露路径，本轮修复 collision fallback 漏算；双路径本身仍开放，建议退役 fallback 或明确标为 degraded result。 |
| 高 | 缺失 category 时 overall 会在剩余 category 上重归一。 | 一个只依赖 safety 的 overall 与三类齐全的 overall 都可能显示 70，但证据范围不同，跨地点/时点直接比较不成立。 | **部分缓解**：`score_coverage` 和 `effective_ratio` 已发布，敏感性分析覆盖缺失规则；headline 仍保留重归一分数，方法学决策未关闭。 |
| 高 | comparison delta 只传播点估计，没有传播两点 uncertainty、coverage 差异或差值区间。 | 小幅 B−A 差值可能被展示为确定差异，即使两个分数区间高度重叠或证据覆盖不同。 | **开放**：方向和色标由服务端锁定，但不能据此声称差异显著。 |
| 中 | 半径查询以 H3 cell 中心是否落入半径选单元。 | 点或半径轻微移动会使边缘单元整块进入/退出，产生阶跃；这不是连续的真实影响范围。 | **开放**：本轮让 scenario 与点评分采用同一规则，解决的是一致性，不是边缘跳变本身。 |
| 中 | hotspot 只使用 detail payload 中最多 60 个 `map_points`。 | 密集区域的截断样本可能改变 DBSCAN 簇数量、主类型与半径，不能视为全量空间统计。 | **部分缓解**：本轮修复坐标源、事件过滤、非法坐标和米制距离；60 条上限仍开放。 |
| 恒定 | intervention elasticity 是观察数据上的相关关系，不是因果效应。 | “增加一个设施后分数提高 X”可能混入选址、密度和其他共同原因，不能作为政策效果预测。 | **恒定约束**（第二版重分类）：响应已标 `causal=false` 并给 fit/caveat。除非引入真正的因果设计，否则这条永久成立；可关闭的只有防回退门禁。 |
| 高 | raw audit 对多数正式 CSV 只检查列、行数与可解析性，没有逐文件 hash、来源 snapshot ID、as-of 和 freshness SLA。 | 上游内容可在文件名和行数相同的情况下变化；门禁无法证明数据来源、时点或是否过期，结果难以完整复现。 | **部分缓解**：本轮修复 audit 状态、退出码和 auxiliary 模型；内容身份与新鲜度契约仍开放。 |
| 高 | Agent eval 以路由和响应契约为主，语义事实性、证据充分性、数字一致性和拒答质量门禁不足。 | schema 与工具调用均通过的回答仍可能错误归因、过度概括或给出证据不支持的建议。 | **开放**（复核 2026-08-20，补入初版缺的模型侧证据）：初版引用的「19 cases 契约通过 / routing 4/4」是**离线契约测试，不含模型**。同日补跑了一轮真实模型评测（30 用例、生产循环、本地 Nano）：29 executed、pass_rate **0.931**、p50 15.2 s、wall_max 91.5 s，与退役 RAG 前的生产基线逐位一致（同为 29 executed / 0.931），说明退役未造成可测退化。但该套判分仍以正则与工具调用为主，**结论不变**：需要固定模型与人工标注质量集。 |
| 中 | transient tool error 缺少统一、确定性的重试策略。 | 网络抖动、Gateway 短暂失败或读超时时，相同请求可能随机失败；模型自行重试还可能重复昂贵调用或产生不同轨迹。 | **开放（范围已缩小，2026-08-21）**：harness 已修（见下），随后 `fault-score-tool-flaky` 由「12 次尝试失败 10 次」变为 **3/3 全过**，`fault-score-tool-down` 维持 3/3——**「agent 不重试」这条从来不是产品缺陷，是 harness 在叫它放弃**。仍然开放的是更窄的一条：没有按错误类型/次数/退避/幂等性定义的显式重试策略。实测规律是**错误带不带修复指令**决定重试与否——`query_dataset` 的错误附带 `available_datasets` 全量合法 id，agent 一次往返即自纠；而 `fault-score-tool-flaky` 注入的错误原文是 `retry_hint: 'This tool is failing. Do not report its numbers.'`，**在叫模型停止调用**，该用例却断言 `min_tool_calls: 2` 要求它重试。测试自带指令与自身断言互相矛盾，因此不能用它证明「agent 不会重试」。修 harness 应先于修产品。 |
| 高 | 评测的 `pass^k` 指标被系统性低估约 13.8 个百分点，所有历史报告均受影响。 | 4 个 `routing` 用例走确定性意图路由、不经过模型，因此从不经过 `collapse_attempts()`，`pass_hat_k` 恒为 `None`——**却照样计入分母**。本轮实测 `pass^k` 报 23/29 = 0.793，实际应为 27/29 = 0.931。已核对 08-20 的 cutlass/marlin A/B 报告，同为 `{1.0: 23, None: 4, 0.0: 2}`，说明偏差存在于全部历史数字（0.793 / 0.724 / 0.690 / 0.655）。 | **已关闭 2026-08-21**：`run_routing_case()` 现返回 `attempts` 与 `pass_hat_k`，并有两个测试守住（一个钉住 routing 结果带该键，一个钉住 repeat=1 时 `pass^k == pass_rate` 的不变量）。修后实测 `pass^k` 0.931。**注意历史数字仍带旧偏差**：08-20 及之前报告中的 0.793 / 0.724 / 0.690 / 0.655 未追溯重算，与新数字不可直接比较。 |
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
- Agent eval（离线契约）：service corpus 19 cases 通过；deterministic routing 4/4。
- **Agent eval（真实模型，第二版补测）**：`model_cases` schema 1.2，30 用例经生产
  循环打到本地 Nano `:8000`。29 executed / 1 skipped（`tool-find-similar`，门禁
  未放行）；26 pass、1 warn、2 fail；`pass_rate` **0.931**；p50 15.22 s、
  max 91.54 s、总 609 s；285.2 tok/s。报告存于
  `/mnt/data/urban-dossier-state/evals/post_rag_retire_20260820.json`。
  两个 fail 分别是 `tool-compare-two-places`（已知高频抖动，MODEL_CANDIDATES
  记有「两模型均不稳定，单轮不足以定论」）与 `fault-score-tool-flaky`（见架构表
  中 harness 自相矛盾一条）。
- **与退役 RAG 前的对照**：cutlass 生产基线为 29 executed / `pass_rate` 0.931，
  与本次逐位一致。该比较跨越了 schema 1.1→1.2，通常不成立；此处成立是因为被删
  的用例在旧跑中本就是 skip，两侧 `cases_executed` 同为 29。
- Python 环境：87 packages compatible。
- 格式：`git diff --check` 通过。
- **证据可复现性**：以上全部对应提交 `fd5215b`。初版写作时这些改动尚未提交，
  证据只存在于单个工作区。

## 剩余风险与建议顺序

> **第二版新增第 0 项，第三版（2026-08-21）已完成它。** 评测框架自身的缺陷会污染
> 此后所有用评测数字做的判断，所以它排在产品修复之前。

0. ~~**最先：修评测框架的两个缺陷。**~~ **已完成 2026-08-21。**
   原始条目：（a）`fault-score-tool-flaky` 与 `fault-score-tool-down` 共用同一个
   注入 payload，而那句 `retry_hint` 在叫模型停止调用，与 `flaky` 用例
   `min_tool_calls: 2` 的断言直接矛盾；（b）`pass^k` 的分母包含 4 个永远拿不到该键
   的 routing 用例，导致历史 `pass^k` 低估约 13.8 个百分点。

   两项均已修，各有测试守住（`test_business_eval.py` 新增 4 个用例）。修后实测：
   `pass^k` 0.793 → **0.931**（repeat=1 时与 `pass_rate` 相等，恢复文档所述不变量）；
   `fault-score-tool-flaky` 由四种服务配置下 12 次尝试失败 10 次 → **3/3 全过**，
   同时 `fault-score-tool-down` 维持 3/3，证明不是把测试改松了。**由此推翻了一条
   长期被当作最高优先级产品缺陷的结论**：agent 并非不会重试，是 harness 在叫它放弃。
   详见 `evals/agent/README.md` §「Retracted」。

1. ~~**高：报告 grounding 实现缺失。**~~ **已完成 2026-08-22**：走了「删除声明 + 产物显式标注」这条路。原文如下——

1. ~~高：报告 grounding 实现缺失。~~ `agent_service.py` 的 scripts 模式引用 `resolve_symgen.py` / `verify_narrative.py`，但仓库没有这两个实现；生产 OpenClaw 报告路径也没有同等级的确定性数字核验。发布报告功能前应补齐一个真实、测试覆盖的 grounding 实现，或删除所有 SymGen 声明并把报告明确标成仅基于所给 evidence 的模型摘要。
2. ~~**高：CI 不执行行为测试。**~~ **已完成 2026-08-22**（见架构表对应条目）。原文如下——

2. ~~高：CI 不执行行为测试。~~ `.github/workflows/lint.yml` 目前只做 Python compile，shellcheck 还被 `|| true` 设为非阻断。本轮 453 个 Python tests、前端 build/smoke、Node contracts 和无数据 agent eval 应分层进入 CI；依赖真实数据/浏览器/服务的测试可单独作为 workstation release gate。
3. **中：`SkillDataProvider` 是占位实现。** `data mode=skill` 会失败，`auto` 会安全回落 direct。若不计划支持，应删除该模式；若计划支持，应先完成实现和契约测试再公开。
4. **中：热点输入最多 60 个 map points。** 密集区域会被截断，适合 UI 提示而不是全量空间统计；应为热点提供独立、可分页的数据查询。
5. **中：情景模拟是相关性模型。** Intervention elasticity 不能表述为因果预测，UI、报告和提示词必须继续保留此限制。
6. **中低：可访问性与 bundle。** Modal 缺完整 focus trap/Escape，部分 icon-only 按钮缺 aria-label；Vega embed 约 855 KB、MapLibre 约 1.05 MB，已分 chunk 但仍值得按路由动态加载。
7. **环境覆盖：** GPU/cuML、DGX Spark、mac profile 未在本轮实际运行；正式发布前分别执行硬件矩阵和固定模型质量集。

## 复核记录（第二版）

初版的断言逐条按 `fd5215b` 复核，方法与结果如下。**复核的是仓库，不是初版文本**。

| 初版断言 | 复核方法 | 结果 |
| --- | --- | --- |
| `453 passed` | 在 `fd5215b` 上重跑完整套件 | ✅ 属实 |
| 7 个 analyst tools | `len(tools.TOOLS)` | ✅ 7 |
| 19 个 service eval cases | 读 `business_cases.json` | ✅ 19，schema 1.1 |
| 前端依赖已精简、包名已改 | 读 `package.json` | ✅ 直接依赖 20 个，名为 `urban-dossier-frontend` |
| SymGen 实现不在仓库 | 全仓 `find` + 读调用点 | ✅ 属实，但机制是静默降级而非崩溃（已改准） |
| CI 只做 compile + 非阻断 shellcheck | 读 `.github/workflows/lint.yml` | ✅ 仅两个 job，shellcheck 带 `\|\| true` |
| `SkillDataProvider` 是占位 | 读 `skill_provider.py` | ✅ 每个方法都 `raise RuntimeError` |
| backend 依赖 `sys.path` 注入 skills | 读 `app.py:27-33` | ✅ 属实 |
| Agent session 仅进程内存 | 读 `agent_session.py:37-40` | ✅ 内存 dict + 锁 |
| `AGENT_BACKEND` 两处默认值相反 | 全仓 grep 消费者 | ⚠️ 属实但影响被高估，`config.py` 那份无人使用（已降级并改结论） |
| 初版未提及 RAG 退役 | grep 全文，3 处 "rag" 均为 `cove**rag**e` 子串 | ❌ 覆盖缺口，已补 |

## 本报告未覆盖

明确声明边界，比留白更可信。以下均**未**在本轮验证：

- **硬件矩阵**：GPU/cuML、DGX Spark、mac profile 一次都没跑；聚类标签、数值精度与
  模型工具选择都可能随硬件与依赖版本变化。
- **并发与多进程**：session 只在单进程内存中验证过。多 worker 下的可见性、负载
  均衡后的续聊、以及重启后的留痕，均无测试。
- **长会话**：没有测过 context 溢出边界。32K 窗口下多轮叠加工具观测何时触顶、
  触顶后如何降级，未知。
- **负载与稳定性**：没有压测、没有长稳跑；本轮最慢用例 91.5 s，但无并发场景数据。
- **语义质量**：判分仍以正则与工具调用为主，没有人工标注质量集（见算法表对应条目）。

## 发布判断

- 本地演示/工作站日常分析：**可用**。
- 数据与算法发布门禁：**通过当前快照**。
- 前端对比与可视化：**可用且已回归**。
- Agent ask：**生产边界内可用**。真实模型评测 29 executed / `pass_rate` 0.931，
  与退役 RAG 前的基线一致；但该分数出自以正则与工具调用为主的判分器，不等于语义
  质量已验证。
- 报告/海报正式对外发布：**可用**（2026-08-22 起）。grounding 契约已落实：每个
  产物带 `grounding` 字段并在页脚声明「数字取自所给 evidence，生成后未经独立复核」。
  这不是更强的保证，而是**诚实的、可被下游读取的**保证；要提升到「已核验」需要一个
  真实的 grounding 实现，届时把 `method` 从 `none` 改掉即可。
- 跨平台正式 release：**尚未完成**，受硬件矩阵约束（CI 缺口已于 2026-08-22 关闭）。
- **把评测数字当门禁**：**尚不可**。`pass^k` 低估约 13.8 个百分点、且有一个 fault
  用例的注入指令与自身断言矛盾；先修框架（建议顺序第 0 项），再谈门禁。
