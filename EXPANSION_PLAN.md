# Urban Dossier 扩展计划

> 文档状态：Planning
> 基线日期：2026-08-03
> 调研方式：代码库审查 + 工作站实测 + 一手来源联网调研
> 与 [`PROJECT_PLAN.md`](PROJECT_PLAN.md) 的关系：PROJECT_PLAN 定义"收口"——修正确性、统一契约、建立可复现内核；本文件定义收口之上的六个扩展方向。**两者冲突时以 PROJECT_PLAN 为准。**

## 0. 范围与前提

### 0.1 六个扩展方向与本文件的对应

| 方向 | 本文件章节 |
| --- | --- |
| 1. 优化评分与评价系统，参考市政/调查机构规范，扩展数据集 | [第 1 节](#1-评分系统规范化) |
| 2. 提高数据结论可视化、富文本对比、地图交互与配色 | [第 2 节](#2-可视化与地图渲染) |
| 3. 优化 Agent 框架与 chat/analyst 功能 | [第 3 节](#3-agent-框架与数据分析管线) |
| 5. 强化数据分析能力，补齐 workflow 管线与 tool/skill 规范 | [第 3 节](#3-agent-框架与数据分析管线)（与方向 3 合并） |
| 4. 本地模型升级，增强工具调用与多模态 | [第 4 节](#4-本地模型升级) |
| 6. 框架确定后压榨硬件性能与优化算法 | [第 5 节](#5-硬件压榨与框架优化) |

方向 3 与方向 5 合并的原因：工具注册表、参数校验、payload 策略和意图路由既是 Agent 框架，也是数据分析管线的执行层。拆成两节会让同一批文件出现在两处工作项里。

### 0.2 硬前提

本文件的所有工作项都排在 PROJECT_PLAN 第 18.4 节定义的纵向收口之后：

```text
统一 /api/agent/ask 契约 -> Node pass-through proxy -> UI 迁移
  -> evidence/trace 结构化 -> 删除旧直连 vLLM ReAct 生产声明
  -> Node 重评分与无版本缓存移除
```

理由不是流程洁癖，是三个已确认的事实：

1. 测试基线为 4 个 smoke 文件共 495 行，前端零测试，CI 不跑 pytest 也不跑 tsc。已经发生的评分语义缺陷（sanitation 用了 rodent baseline、collision 分类错位，见 PROJECT_PLAN P0-02）正是缺回归测试的直接代价。
2. `/api/agent/chat` 与 `/api/agent/ask` 并存且后者签名断裂。任何新工具接在断裂的入口上都要返工两次。
3. [`server.js`](server.js) 仍在做点位重评分，与 FastAPI 的分数语义并存。新增可视化如果消费这条路径，会把视觉插值固化成业务事实。

### 0.3 不变的架构约束

扩展过程中以下约束不得放宽，它们是本项目区别于通用 BI 工具的根据：

- 数值由 DuckDB/Parquet 确定性计算，LLM 只解释既有证据，不计算也不猜测；
- 图表 spec 由后端规则生成，LLM 不产出图表配置；
- 未实现的工具不向模型暴露（PROJECT_PLAN 8.3 工具上线门槛）；
- 新数据集必须通过 manifest 与 schema validation 才能进入分析层，失败导入不污染当前快照；
- 插值与视觉平滑只能进入展示层，不得进入报告、比较或 Agent evidence。

---

## 1. 评分系统规范化

### 1.1 目标

把评分从"自定权重的加权平均"升级为可审计的复合指标：方法有版本、缺失有策略、权重有敏感性分析、粒度有披露。

### 1.2 现状与差距

现状：四类别 safety 0.40 / transit 0.30 / amenities 0.30 / building 0.0（[`categories.py`](backend/src/urban_dossier_backend/categories.py)），子指标经验百分位归一化到 0–100（[`preprocess_common.py`](backend/scripts/preprocess_common.py) `percentile_score()`），类别内按非空子项重归一化加权（[`secondary_scoring.py`](backend/src/urban_dossier_backend/secondary_scoring.py) `_weighted_score()`），overall 支持用户优先级几何衰减权重（[`utils.py`](backend/src/urban_dossier_backend/utils.py) `build_priority_weights()`，衰减系数默认 0.72，定义在 [`config.py`](backend/src/urban_dossier_backend/config.py) 并可由 `URBAN_DOSSIER_PRIORITY_DECAY` 覆盖）。

参照 OECD/JRC《Handbook on Constructing Composite Indicators》十步框架（理论框架 → 指标选择 → 缺失插补 → 多元分析 → 归一化 → 加权聚合 → 不确定性与敏感性分析 → 回溯分解 → 关联外部变量 → 呈现），差距集中在三步，且都不在归一化上：

| 手册步骤 | 现状 | 差距性质 |
| --- | --- | --- |
| 缺失插补 | building 权重 0，实为隐式 listwise 删除；无覆盖度披露 | 缺显式策略 |
| 多元分析 | 类别内子指标相关结构未检验 | 可能重复计权 |
| 不确定性与敏感性分析 | 完全缺失 | 机构级与业余的分界线 |
| 归一化 | 经验百分位 | **手册认可的方法，不是短板**，需固化参照分布并写入方法版本 |

经验百分位归一化在 v3.7.8 已替换旧的线性 clip 公式，替换原因（旧公式把大量 cell 压成 0 或 100，梯度消失）记录在 [`preprocess_common.py`](backend/scripts/preprocess_common.py) 文件头注释。该决策保留。

### 1.3 可借鉴的机构先例

| 先例 | 可借鉴的具体做法 |
| --- | --- |
| AARP Livability Index | 类别 0–100、全国均值锚定 50（相对全国分布而非样本内百分位）；逐指标披露地理粒度而不做硬插值 |
| CCC Community Risk Ranking（NYC 本地） | 用 5 档分级代替精确分数，以"降精度"表达不确定性，同时回避权重争论 |
| CDC PLACES | 每个 tract 估计附 95% CI 并在面向公众的 explorer 中直接展示——证明公开 UI 呈现不确定性可行 |
| OECD Better Life Index | 维度内等权 + 跨维度用户自定义权重，与本项目的优先级机制同构 |

学界对用户自定义权重的主要批评是排名对权重扰动过敏。结论是**补敏感性分析比更换权重方案更对症**：现有优先级机制保留，用扰动区间兜底。

Eurostat EU Quality of Life 采取不聚合的仪表盘路线，只能作为维度框架参照，不能作为聚合方法参照。

### 1.4 工作项

| # | 工作项 | 内容 | 验收判据 |
| --- | --- | --- | --- |
| 1.1 | MetricDefinition 注册表 | 即 PROJECT_PLAN P0-02 交付物，补 `methodology_version` 字段 | 任一 UI 分数可反查其定义、单位、方向与方法版本 |
| 1.2 | 缺失数据显式化 | 每个 H3 cell 与 NTA 携带 coverage（n/N）；no-data 明确呈现 | 缺失不被静默解释为 0，也不被隐式剔除 |
| 1.3 | 子指标相关性检验 | 计算类别内 Spearman 相关矩阵，识别重复计权（重点核查 311-sanitation 与 rodent） | 产出相关性报告与权重调整决议，决议写入方法论文档 |
| 1.4 | 敏感性分析管道 | 权重 ±25% 与归一化方法替换的 Monte Carlo，离线批算并落盘 | API 返回分数区间；UI 呈现档位而非伪精确值 |
| 1.5 | 数据集扩展 | 见 1.5 节清单 | 每个新指标走完 1.1–1.4 全流程才允许上线 |
| 1.6 | 公开方法论页 | JRC 统计审计式：逐指标列出来源、粒度、归一化、权重、方法版本 | 页面版本号与代码 `methodology_version` 一致 |

敏感性分析属于离线批处理，不进在线请求路径，与 PROJECT_PLAN 7.1 的在线/离线划分一致。

### 1.5 扩展数据集清单

以下 ID 与粒度经 2026-08-03 实测确认，2026-08-11 复核并全部本地快照（8/8 落盘于
`/mnt/data/urban-dossier-state/datasets/raw-expansion/`，逐集 manifest 含 SHA-256 与
意外记录，汇总见该目录 `INVENTORY.md`）。表中两处按复核结果就地更正并标注。

| 类别 | 数据集 | ID / 入口 | 空间粒度 | 更新 | 备注 |
| --- | --- | --- | --- | --- | --- |
| 健康 | CDC PLACES（tract 版） | `ai6z-tcin` @ data.cdc.gov | census tract | 年 | 约 40 项指标，自带 95% CI |
| 健康 | City Health Dashboard | cityhealthdashboard.com | city + tract | 年 | 37 指标，其中 22 项到 tract |
| 环境 | NYCCAS 空气污染栅格 | `q68s-8qxv` | 约 300m 栅格 | 年 | **与 H3 r9 适配最好**，优先于汇总版 `c3uy-2p5r`。2026-08-11 实测：非表格资产（blobby），实为年均栅格 zip + 数据字典，接入走栅格采样而非 SODA |
| 环境 | 热脆弱性指数 | `4mhf-duep` | **ZCTA**（2026-08-11 实测更正，原记 NTA 有误） | 静态 | 需 ZCTA→NTA 转换表后才能进 NTA 视图 |
| 环境 | 311 噪音类投诉 | `erm2-nwe9` | 点 | 日 | 该 ID 现仅含 2020 起数据，2010–2019 在 `76ig-c548` |
| 教育 | School Quality Reports | `dnpx-dfnc` | 学校点位 | 年 | |
| 住房 | HPD 住房维护违规 | `wvxf-dwi5` | 建筑 / BBL | 日 | 已在用，可深化 |
| 住房 | DOF Rolling Sales | `usep-8jbt` | 地块 | 月 | 房价维度；年度版 `w2pb-icbu` |
| 社会经济 | ACS 5-year | ~~Census API `acs/acs5`~~ www2.census.gov 汇总文件 | tract | 年 | 自带 MOE；租金负担、收入。2026-08-11 实测：api.census.gov 已全面强制 API key（无 key 302 至 missing_key.html）；官方 table-based 汇总文件无 key 可用，估计值与 MOE 一致 |
| 参考 | NYU Furman CoreData | furmancenter.org/coredata | sub-borough / CD | 年 | 粒度过粗，仅作交叉验证参照 |

> 实施记录（2026-08-12）：NYCCAS year-16 NO 已作为首个 1.5 指标完成
> raw hash 校验、原生栅格 H3 r9 centroid lookup、ready artifact + 3.9.0
> manifest、注册表、相关性、1000 次敏感性、API、方法论页和详情卡全链路。
> 7,414 个陆地 centroid 中发布 7,413 个；不插值，缺失不置零。由于它是
> 约 300m 的年均统计模型而非监管监测，且六维权重尚未决策，当前仅作
> context，综合分权重为 0。复现与限制见
> [`docs/methodology/nyccas-no.md`](docs/methodology/nyccas-no.md)。其余七个
> 快照仍未完成本节发布门，1.5 状态保持 partial。

> 实施记录（2026-08-12，第二项）：HVI `4mhf-duep` 的 184 个 ZCTA
> quintile 已完成 CSV + 官方 metadata 双哈希校验、原生 ZIP ready artifact、
> 3.9.0 manifest、注册表、ZIP 相关性、敏感性、API 和在线/离线卡片。
> HVI 1–5 仅按 `(5-HVI)*25` 反转显示，不插值；它通过逐指标
> `metric_scores` 独立呈现，category/overall 权重均为 0，避免与 NYCCAS
> 任意合成。NTA 地图仍须显式 crosswalk，因此没有越过本节粒度规则。
> 复现、component vintage 与 ZIP/ZCTA 限制见
> [`docs/methodology/heat-vulnerability.md`](docs/methodology/heat-vulnerability.md)。

粒度处理规则（采纳 AARP 先例，披露而非硬插值）：

- 点数据（311 / HPD / 学校 / 销售）直接聚合到 H3 r9；
- tract 级指标（PLACES / ACS）**首版只在 NTA 视图呈现，不下沉到 hex**，避免方差稀释被当成观测精度；
- UHF42 / NTA / sub-borough 级指标在指标卡上标注其原始尺度。

### 1.6 本节的产品决策依赖

四类扩到六类会重定义权重结构。建议扩类后先按 AARP 先例等权上线，用户优先级机制保留，由 1.4 的敏感性分析兜底。该决策与 building 类别定位（正式第四维度或独立 Risk Flag，PROJECT_PLAN P0-02 要求二选一）同时决定。

---

## 2. 可视化与地图渲染

### 2.1 目标

补齐图表层，让对比差异一眼可见；地图从单变量 choropleth 扩展到差值图、双变量与时间轴。

### 2.2 现状

前端为 React 19 + Vite 6 + MapLibre GL 5，[`Map.tsx`](interactive-map-explorer/src/components/Map.tsx) 手写二十余个图层的矢量瓦片样式、3D 建筑挤出与 H3/NTA choropleth，地图表达力已在同类项目上游。但**全仓库没有引入任何图表库**：唯一的图形化数据呈现是 [`App.tsx`](interactive-map-explorer/src/App.tsx) 中手写的内联 SVG sparkline（collision / rodent / violation 三条趋势线）。分数本身仍只有色块与文本，后端已算出的季度序列、类别构成与覆盖度缺少可视化载体。

[`App.tsx`](interactive-map-explorer/src/App.tsx) 为 1184 行单文件、约 30 个 `useState` 顶层提升、无路由。图表基建落地前需先执行 PROJECT_PLAN 9.1 的 feature 拆分，否则新组件会继续加重顶层状态。

### 2.3 选型决策（已裁决，不再比选）

**图表：Vega-Lite + vega-embed。** 判据是与既定架构的契合度——Vega-Lite 是纯 JSON 声明式 grammar，后端确定性生成 spec、前端一行渲染，与"后端产 spec、LLM 零参与"一致；vega-embed 为框架无关的 DOM 挂载，绕开 React 19 wrapper 兼容问题；支持从 NPM 本地打包内联进导出 HTML，满足离线自包含。代价是 bundle 约 200–300KB，复杂联动刷选需下沉到 Vega 层。

ECharts 6 保留为报告导出的备选：其零依赖服务端 SVG 渲染可实现"后端直接渲出 SVG 嵌入 HTML、导出件不含 JS"。Recharts 与 Observable Plot 排除，原因是 JSX 组树 / JS API 调用形式无法由后端序列化生成。

**色彩：`d3-scale-chromatic` + `d3-scale`，不自造色带。** 规则：

- 分级断点在**后端**计算（偏斜的城市指标默认 quantile；有天然聚簇时用 Jenks / ckmeans），前端 MapLibre `step` / `match` 表达式只做查表——保证同一指标在地图与图表中用色一致；
- 分级数 5–7；
- 差值图使用 diverging 色带并以 0 为固定中点（`scaleDiverging`），不得拿 sequential 色带硬掰；
- **禁用红绿组合**（RdYlGn）作为差值配色，改用 RdBu 或 PuOr；
- 双变量 choropleth 使用 Stevens 的现成 3×3 九色方案，不自行配色。

**H3 渲染：维持 MapLibre fill layer。** 当前四个 r8 图层各 1,171–1,232 个 cell，远未到瓶颈；十万级或需要 3D 拉伸动画时再引入 deck.gl `H3HexagonLayer`。

**时间轴：MapLibre 5.x `global-state` 表达式。** paint 表达式引用全局状态，每 tick 调用一次 `setGlobalState` 驱动全图重求值，避免逐 feature `setFeatureState`。

**对比模式：两条都做，差值图为主。** 后端计算 delta 字段 + diverging 单图的感知效率高于并排对比；`@maplibre/maplibre-gl-compare` 的滑动分割作为辅助视图。

### 2.4 工作项

| # | 工作项 | 依赖 | 验收判据 |
| --- | --- | --- | --- |
| 2.1 | 后端 ChartSpec 生成器 + 前端 VegaChart 组件 | App.tsx 状态拆分 | spec 携带 `code_ref` 溯源；断网可渲染 |
| 2.2 | 分数富文本卡片：分布直方图标注"你在这里"、类别构成条形、趋势 sparkline | 2.1 | 每个对外分数至少一种图形化呈现 |
| 2.3 | 对比视图：差值地图 + Compare 工作台图表 | 2.1 + Milestone B 后端 delta | 对比不在前端临时相减 |
| 2.4 | 断点与色彩服务端化；双变量 choropleth | 2.1 | 同一指标地图与图表配色一致；通过色盲安全检查 |
| 2.5 | 时间轴动画（global-state 驱动） | PROJECT_PLAN P0-03 时间对齐修复 | 季度序列按真实 period key 播放，非数组位置 |
| 2.6 | 自包含 HTML 报告导出（内联 vega bundle） | 2.1–2.3 | 断网可打开；含方法版本与生成时间 |

2.5 强依赖 P0-03：当前季度标签按执行当天倒推、序列按数组右端对齐，在此基础上做时间轴动画会把对齐缺陷放大成可见的错误动画。

---

## 3. Agent 框架与数据分析管线

### 3.1 目标

单一 Agent 入口与结构化 evidence/trace；在其上把"任意 NYC 数据集接入 → 分析 → 可视化"做成受控管线。

### 3.2 现状

- 两套 Agent 并存：[`agent_service.py`](backend/src/urban_dossier_backend/agent_service.py) 的 OpenClaw 专项 agent 为生产路径，[`agent_loop.py`](skills/urban_dossier_analyst/agent_loop.py) 的直连 vLLM ReAct 为实验路径；
- 防幻觉机制已有实质实现：[`evidence.py`](backend/src/urban_dossier_backend/evidence.py) `verify_priority_actions()` 丢弃引用了不存在 `evidence_id` 的行动项；
- **RAG 子系统建成但未接线**：[`rag/`](rag/) 下 ingest / embed / vector_index / retrieve / rerank 五件套共约 1,214 行实现完整，`rag/catalog.json` 有 18 条 12 字段元数据（含 `gotchas`、`join_keys`、`sample_queries`），但 backend 与 scripts 中无任何 `import rag`，索引从未构建；
- [`skills/`](skills/) 中已有完整 prep-data 流水线（discover → clean → report，含 profile / clean / validate 脚本），是数据接入的现成逻辑来源；
- [`tools.py`](skills/urban_dossier_analyst/tools.py) 中 `compare_neighborhoods`、`walking_isochrone`、`simulate_intervention`、`retrieve_dataset_docs` 为 `NotImplementedError` 桩。

### 3.3 数据接入的外部可行性（2026-08-03 实测）

| 能力 | 端点 | 实测结果 |
| --- | --- | --- |
| 数据集发现 | Socrata Discovery API `api.us.socrata.com/api/catalog/v1?domains=data.cityofnewyork.us` | 无认证可用，NYC 域返回 3,014 个资产 |
| Schema 拉取 | `data.cityofnewyork.us/api/views/{4x4}.json` 与 `api/views/metadata/v1/{id}` | 返回完整列定义与 `rowsUpdatedAt` |
| 增量同步 | SODA 2.1 系统字段 `:updated_at` 配合 `$where` / `$limit` / `$offset` | 可查，可做水位线增量 |
| 官方资产清单 | Local Law 251 Published Data Asset Inventory `5tqd-u88y` | 可作为 catalog 的交叉校验源 |

风险：Socrata 被 Tyler Technologies 收购后平台更名 Data & Insights，开发者文档 changelog 停更于 2016，生态处于"可用但不再演进"状态。曾检索到第三方来源声称 SODA2 已弃用并强制认证，与本次实测（无 token 返回 200）直接矛盾，按实测为准；但**上游 API 变更必须按一等风险对待**，下载器保持严格校验与 quarantine 是既有的正确防御。批量遍历前应申请免费 app token 以规避未认证限流。

### 3.4 可借鉴的工程模式

参照对象为同机的 Analyst copilot（本地优先 EDA 平台，已验证可运行）：

- **三档 payload policy**（`schema_only` / `schema+aggregates` / `+sample`）把"给模型看多少数据"做成显式产品开关；
- **图表确定性生成，LLM 只叙述**——与本项目约束一致，可直接采用；
- **每个工具参数过本地 column / enum / range 校验**，模型只能选工具不能越权；
- **CSV 加载器的编码嗅探与词法列保护**（扫前若干行把 zip / phone / 前导零整数强制为 string，避免 `007` 变成 `7`）；
- **非破坏式清洗 recipe**：preview → approve → 产出新版本目录，源文件永不原地修改；
- **主键教训**：内容哈希派生的 id 跨 run 不唯一，任何以其为唯一主键的表都会串数据，须复合 `(content_id, session_id)`。

需要规避的坑（来自该项目自身的 review 记录）：资源不足时几乎未执行却把会话标记为 completed 的 fail-open；采样验证的 join 仍标记为 verified；前后端对同一语义采用两套判定口径。本项目的 audit / quarantine 文化是这些问题的解药，扩展时不得为了流畅性放宽。

技术路线选择：**text-to-SQL + 语义层**，不做代码生成沙箱。理由是数值路径必须可审计——语义层路线下数值全部由 DuckDB 计算、LLM 只做自然语言到查询的翻译与结果解释，与 0.3 节约束同构；代码生成沙箱允许模型编写任意计算逻辑，数值来源不可审计。

### 3.5 工作项

| # | 工作项 | 内容 | 验收判据 |
| --- | --- | --- | --- |
| 3.1 | `/ask` 契约统一、`/chat` 弃用 | PROJECT_PLAN 18.4，本文件全部工作项的前置 | 双入口合一；evidence/trace 结构化 |
| 3.2 | 类型化工具注册表 | 每工具 Pydantic schema + 本地 column/enum/range 校验 | 参数越界在本地被拒绝，不进入执行层 |
| 3.3 | 三档 payload policy | 显式开关，记录到 AnalysisRun | 每次 LLM 调用可审计其数据可见范围 |
| 3.4 | 意图路由 | ask_from_evidence / new_analysis / meta_help / out_of_scope | out_of_scope 不进入分析链路 |
| 3.5 | Socrata 接入管线 | Discovery 遍历 → catalog 入库 → `:updated_at` 水位线增量；复用 [`download_socrata_snapshot.py`](scripts/download_socrata_snapshot.py) 的分页校验与断点续传 | 新数据集经 raw-audit 才发布；失败导入进 quarantine |
| 3.6 | 自动 profile 与语义类型推断 | 含 NYC 空间列检测（lat/lon、BBL、BIN、ZIP、NTA、community board）→ 自动 H3 索引 | 前导零字段不被降级为整数；空间列检出即可出图 |
| 3.7 | 受控 text-to-SQL | 只读 DuckDB + SELECT 白名单 + 列名校验 + 失败回灌一次 | 所有数值来自 DuckDB 执行结果 |
| 3.8 | catalog RAG 接线 | 启用现成的 [`rag/`](rag/) 五件套，用于数据集发现与字段理解 | 自然语言描述能召回正确数据集与字段 |
| 3.9 | legacy skills 收编 | prep-data 流水线迁移为受控 job；桩工具从模型可见列表移除 | 未实现工具不暴露给模型 |

### 3.6 接入数据的产品边界

导入的数据集默认进入**探索层**：可查询、可出图、可被 Agent 引用为证据，但**不自动进入评分体系**。metric mapping 需人工确认并生成 MetricDefinition（1.1）后，才允许参与打分。该边界守住第 1 节建立的方法论严肃性，也符合 PROJECT_PLAN Milestone D "UI 和 Agent 只能看到已发布数据集"的验收标准。

---

## 4. 本地模型升级

### 4.1 目标

以工具调用能力为第一优先级更换主力模型；评估多模态副手。

### 4.2 候选与判据

现状为 `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4`，稳态约 40.8 GB，实测单并发 266 tok/s、四并发 672 tok/s。

| 候选 | 显存 | 工具调用证据 | vLLM 生态 | 结论 |
| --- | --- | --- | --- | --- |
| Nemotron-3-Super-120B-A12B-NVFP4 | 约 77 GB（须 `--gpu-memory-utilization 0.85`） | 官方 TauBench-V2：Airline 56.25 / Retail 63.05 / Telecom 63.93；未公布 BFCL | 官方配方完整：`--tool-call-parser qwen3_coder --reasoning-parser nemotron_v3` | **首选**，同家族迁移成本最低 |
| Qwen3 系列 | 视量化 | 第三方 BFCL-V4 快照中开源最高分（self-reported，可信度打折） | `hermes` parser，生态最成熟 | 次选 |
| GLM-4.5-Air | 106B，须 4bit 才进 96 GB | 官方技术报告工具调用成功率 90.6% | `glm45` parser，生态弱于 Qwen | 备选 |
| GPT-OSS-120B | 约 63 GB | — | Chat Completions 端点工具不触发、`tool_choice` 仅支持 auto | **排除**，与受控工具选择诉求冲突 |

已知坑：Nemotron-3-Super 开启 MTP / speculative decoding 会额外占用 20 GB 以上并导致 OOM，必须关闭。

多模态：Qwen3-VL 是最接近"单模型兼得"的选项，vLLM 部署与文本版同路径（`--tool-call-parser hermes`），但社区共识是 VL 版纯文本 agent 能力略逊同级文本版。**采用双模型架构**：文本主力 + Qwen3-VL-8B-FP8（约 12 GB）常驻做地图截图与街景理解。

推理开关：Nemotron 使用 `reasoning_budget` 硬顶推理 token（该系列独有），在多步工具循环中兼顾精度与延迟；Qwen3 / GLM 按步骤类型切换（规划步开、机械工具调用步关）。Qwen 官方明确警告 reasoning 模型不要使用 ReAct 这类 stopword 模板——思考段可能输出 stopword 导致解析错乱，若保留 [`agent_loop.py`](skills/urban_dossier_analyst/agent_loop.py) 需核查此点。

### 4.3 工作项

| # | 工作项 | 验收判据 |
| --- | --- | --- |
| 4.1 | **建立固定业务评测集**：20–30 个真实 Urban Dossier 问答与工具调用轨迹 | 评测集入库并可重复执行；**无评测集不做任何模型切换决策** **已完成 2026-08-13**：[`evals/agent/model_cases.json`](evals/agent/model_cases.json) 24 用例（路由/工具调用/证据纪律/多步/格式/鲁棒性），[`scripts/vllm/business_eval.py`](scripts/vllm/business_eval.py) 驱动真实生产循环对任意端点判分，离线单测钉住判分器；Nano 基线报告存于 `/mnt/data/urban-dossier-state/evals/` |
| 4.2 | FP8 KV vs BF16 KV 答案质量 A/B（PROJECT_PLAN 17.3 已列为发布前置） | 在 4.1 评测集上无显著回退 **已完成 2026-08-20**：同卡共存两个 Nano 实例，除 `--kv-cache-dtype` 外参数逐字相同，eval 1.1 全 31 用例 ×3 轮，报告 `kv_ab_20260820.json`。**结论：FP8 KV 无可测质量回退，发布前置解除。** fp8 pass_rate 0.931 / pass^3 0.690 / 254.4 tok/s，bf16 0.897 / 0.724 / 235.2 tok/s——两个质量指标指向相反方向，本身就说明差异在噪声内。逐用例 7 处分歧全部双向（fp8 好 3、bf16 好 3），且每一处在至少一端的三轮内自己就会翻转；唯一确定性结果是 `fault-score-tool-flaky` 两端 3/3 全败，与 KV dtype 无关（见下）。**注意选参陷阱**：`--kv-cache-dtype auto` 在这个 modelopt_fp4 checkpoint 上解析为 `fp8_e4m3` 而非 bf16，必须显式写 `bfloat16`，否则整个 A/B 是 fp8 对 fp8 |
| 4.3 | 三候选 benchmark：Super-120B-NVFP4 / Qwen3 / 维持 Nano-30B | 报告工具调用成功率、P95 延迟、实测稳态显存 **Qwen 侧已完成 2026-08-14**：Qwen3.8-27B-NVFP4（dense，GDN 混合）上线 `candidate-qwen`:8004，与 Lightning 同代码三轮业务评测 + 吞吐 A/B，详见 [`MODEL_CANDIDATES.md`](MODEL_CANDIDATES.md)。结论：单流 8.5× 慢于 Lightning，不作服务替代；但**在自荐采样下是唯一零硬失败的候选**，且在 `compare_neighborhoods` 上正确而 Lightning 三轮全错。副产物：`agent_loop` 中途注入 `role="system"` 的跨模型不兼容已修（Qwen 模板直接 400），及 `pytest.ini` 未纳管导致 worktree 测到主 checkout 代码的假绿 |
| 4.4 | Qwen3-VL-8B 副手试点：地图截图解读 | 与主力模型显存共存实测通过 |
| 4.5 | 生产切换，parser 与 reasoning 配置固化进 [`compose.gpu.yml`](deploy/compose.gpu.yml) | 契约测试全绿后切换 |

4.1 是本节唯一零依赖项，且同时是模型切换、FP8 A/B 与 prompt 迭代三件事的共同地基，**建议最先启动**。

---

## 5. 硬件压榨与框架优化

框架稳定后执行。本节工作项均不改变业务语义。

| # | 工作项 | 触发条件 |
| --- | --- | --- |
| 5.1 | 确认 NVFP4 MoE 实际启用的后端（读 vLLM 启动日志判断 CUTLASS 或 Marlin 回退） | 下次启动 LLM 时顺带完成；若在走 Marlin 回退则存在未取的性能 |
| 5.2 | vLLM sleep mode 编排：Level 1 权重卸载至 CPU RAM（128 GB 内存充裕） | 采用模型舰队方案后 |
| 5.3 | 预处理从 pandas 全量物化改为 DuckDB / Polars streaming（吃满 32 核） | 数据集扩展导致刷新耗时上升时 |
| 5.4 | cuVS 评估门槛上调至百万级向量 | catalog 向量规模实测超过阈值时；当前约 90 chunks，扩展后仍在万级 |
| 5.5 | 改写 [`DATA_ARCHITECTURE.md`](DATA_ARCHITECTURE.md) 与 [`PROJECT_PLAN.md`](PROJECT_PLAN.md) 中的 cuSpatial 条目：保留 GeoParquet metadata，移除 cuSpatial 动机 | **已完成 2026-08-03** |
| 5.6 | 系统盘容量清理（`/` 已用 89%） | 任意时间；数据盘 `/mnt/data` 1.7 TB 可用，不受影响 |

5.5 的依据与退役核实（2026-08-03）：RAPIDS cuSpatial 仓库 `archived: true`，最后 push 2025-07-28，终版 v25.04.00。退役前已全库验证其确为未采用状态——无 `import cuspatial`、无依赖清单条目、无配置键或 flag、无产物目录，工作站 venv 未安装；原文两处均为条件式未来计划而非既有依赖。因此改写只影响文档，不涉及代码或运行时产物。GeoParquet metadata 作为独立工作项保留，动机改为标准工具链互操作性。

需要注意的是，实际承担 GPU 空间计算的是 [`hotspot_engine.py`](backend/src/urban_dossier_backend/hotspot_engine.py) 的 cuML DBSCAN，它经 [`service.py`](backend/src/urban_dossier_backend/service.py) 接入 analyze-point 并输出到前端 hotspot 图层。但工作站 backend venv 未安装 cuml/cudf/cupy/rmm，该路径与 [`gpu_accel.py`](backend/src/urban_dossier_backend/gpu_accel.py)、[`gpu_queries.py`](backend/src/urban_dossier_backend/providers/gpu_queries.py) 一样长期走 CPU fallback 分支。这符合 PROJECT_PLAN 7.4 的隔离策略，属于活代码而非死代码，**不得按死代码清理**；其风险见第 7 节待定问题 6。

DuckDB 与 cuDF 的取舍维持 PROJECT_PLAN 7.4 的实测结论（热缓存聚合 DuckDB 4–12 ms、cuDF 8–19 ms），不变。本机被低估的资源是 32 核 Threadripper 而非 GPU：5.3 的收益在 CPU 侧。

---

## 6. 阶段表

| 阶段 | 内容 | 依赖 | 对应方向 |
| --- | --- | --- | --- |
| Phase 0 | PROJECT_PLAN 收口：`/ask` 统一、评分语义修复、fixture 与 contract test、Node 重评分移除 | 无 | 3 的前半、1 的前提 |
| Phase 1a | 评分规范化 1.1–1.4 | Phase 0 的评分修复 | 1 |
| Phase 1b | 可视化基建 2.1–2.4（与 1a 并行，问题空间不重叠） | 前端 feature 拆分 | 2 |
| Phase 2 | 数据集扩展 1.5–1.6 + 数据接入管线 3.5–3.8 | Phase 1a（新指标须过规范流程）+ Phase 0 | 1、5 |
| Phase 3 | Agent 工具体系 3.2–3.4、3.9 + 模型评测与切换 4.1–4.5 | Phase 0；4.1 可提前启动 | 3、4 |
| Phase 4 | 时间轴与对比高级功能 2.5–2.6 + 硬件优化 5.x | 前序稳定 | 2、6 |

Phase 1a 与 1b 可并行，因为两者文件所有权互斥（后端评分 vs 前端图表）。其余阶段串行。

零依赖、可立即启动项：4.1（业务评测集）。5.5 已于 2026-08-03 完成。

---

## 7. 待定问题

以下问题在计划步骤中不得以模糊措辞掩盖，须单独决策后再进入相关阶段：

1. **模型舰队 vs 单大模型独占**。96 GB 下 Nemotron-3-Super-120B（约 77 GB 独占）与"Nano-30B + VLM + embedding + ASR"舰队（约 59 GB，余 35 GB 给 KV 弹性）不可兼得。影响 Phase 3 与 Phase 4 全部排期。可用 sleep mode 做分时折中，但需先确认切换延迟在产品可接受范围内。
2. **building 类别定位**：正式第四维度或独立 Risk Flag。PROJECT_PLAN P0-02 已要求二选一，且是 1.6 权重结构决策的前提。
3. **四类扩六类后的权重结构**。倾向扩类后等权上线并由敏感性分析兜底，但需产品确认。
4. **tract 级指标是否最终下沉到 H3 r9**。首版按 1.5 节规则只在 NTA 视图呈现；是否引入带 provenance 的面积/人口加权分配，取决于 1.4 敏感性分析对方差稀释的量化结果。
5. **GPU adapter 层的 CPU/GPU 一致性无测试覆盖**。[`hotspot_engine.py`](backend/src/urban_dossier_backend/hotspot_engine.py)、[`gpu_accel.py`](backend/src/urban_dossier_backend/gpu_accel.py)、[`gpu_queries.py`](backend/src/urban_dossier_backend/providers/gpu_queries.py) 均为双路径实现，但工作站 backend venv 未安装 RAPIDS，生产中只有 CPU 分支被执行过。PROJECT_PLAN 11.2 已把"Mac/CPU 与 CUDA reference output 对比"列为关键回归样例，而该测试并不存在。需二选一：在 Phase 0 测试基线中补一致性测试，或明确声明 backend venv 永不安装 RAPIDS、GPU 计算只存在于隔离容器（并据此把双路径代码降级为单路径）。
6. **BFCL V4 官方名次未核实**（榜单页面为动态渲染，静态抓取失败）。不阻塞 4.3——自建 benchmark 本就是决策依据，第三方榜单仅作参考。

---

## 8. 主要外部来源

方法论与指数先例：

- OECD/JRC, *Handbook on Constructing Composite Indicators* — https://www.oecd.org/en/publications/handbook-on-constructing-composite-indicators-methodology-and-user-guide_9789264043466-en.html
- JRC Composite Indicators（COIN Tool / COINr） — https://knowledge4policy.ec.europa.eu/composite-indicators/about_en 、 https://bluefoxr.github.io/COINr/
- Saisana, Saltelli & Tarantola (2005), 不确定性与敏感性分析 — https://rss.onlinelibrary.wiley.com/doi/abs/10.1111/j.1467-985X.2005.00350.x
- AARP Livability Index 方法 — https://livabilityindex.aarp.org/methods-sources
- CCC Community Risk Ranking（NYC） — https://cccnewyork.org/introducing-cccs-community-risk-ranking/
- CDC PLACES 方法（tract 级 95% CI） — https://www.cdc.gov/places/methodology/
- 复合指数权重敏感性的批评 — https://arxiv.org/pdf/1608.04556

数据接入：

- Socrata Discovery API — https://dev.socrata.com/docs/other/discovery
- SODA 查询与系统字段 — https://dev.socrata.com/docs/queries/
- NYC Local Law 251 资产清单 — https://data.cityofnewyork.us/City-Government/Local-Law-251-of-2017-Published-Data-Asset-Invento/5tqd-u88y

可视化：

- Vega-Lite embed 用法 — https://vega.github.io/vega-lite/usage/embed.html
- d3-scale-chromatic — https://d3js.org/d3-scale-chromatic
- MapLibre global-state 表达式示例 — https://maplibre.org/maplibre-gl-js/docs/examples/filter-layer-symbols-using-global-state/
- maplibre-gl-compare — https://github.com/maplibre/maplibre-gl-compare
- 双变量 choropleth 方法与配色 — https://www.joshuastevens.net/cartography/make-a-bivariate-choropleth-map/
- deck.gl H3HexagonLayer（备选） — https://deck.gl/docs/api-reference/geo-layers/h3-hexagon-layer

模型与运行时：

- Nemotron-3-Super-120B-A12B-NVFP4 模型卡 — https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4
- vLLM Nemotron-3-Super 博客与 recipe — https://vllm.ai/blog/2026-03-11-nemotron-3-super
- vLLM tool calling 文档 — https://docs.vllm.ai/en/latest/features/tool_calling/
- vLLM sleep mode — https://docs.vllm.ai/en/latest/features/sleep_mode/
- Qwen function calling 文档 — https://qwen.readthedocs.io/en/latest/framework/function_call.html
- GLM-4.5 技术报告 — https://arxiv.org/abs/2508.06471
- RAPIDS cuSpatial（已归档） — https://github.com/rapidsai/cuspatial
