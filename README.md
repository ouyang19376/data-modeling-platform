# 数据建模中心（Data Modeling Center）MVP

![License](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009485.svg)
![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.x-d71f18.svg)
![Tests](https://img.shields.io/badge/tests-183%20passed-brightgreen.svg)

通用型数据开发管理平台的第一个纵向闭环：**数据建模中心**。
覆盖业务调研（主题域/业务需求）→ 模型设计（字段/表级约束/索引/关系）→ 规范校验 → 评审发布 → 版本差异 → DDL 预览 全链路，并提供模型关系图与变更影响分析（候选级）。

技术栈：FastAPI + SQLAlchemy 2.x + Alembic + SQLite（默认）/ PostgreSQL（生产目标），前端为原生 HTML/CSS/JS 单页应用，API 驱动。

当前版本 **V1.2.0**：在 V1.0 真实认证（API Key）与项目级三角色授权之上，陆续交付 V1.1 工作台图表与布局改版、V1.2 两级 RBAC 用户权限体系（平台 admin + 项目 owner/editor/viewer、集中式能力矩阵、八条防护规则）与 Neo4j 级交互图谱画布。功能全貌见[核心业务闭环](#核心业务闭环)与[界面预览](#界面预览)。

## 界面预览

以下截图来自本地完整演示环境（电商数仓演示数据集，114 个模型，见[电商数仓演示数据集](#电商数仓演示数据集)）：

<table>
<tr>
<td align="center"><img src="docs/screenshots/01-dashboard.png" alt="工作台总览" width="100%"><br><b>工作台总览</b>：统计卡片 + 业务域分布 / 数仓分层 / 模型状态三图表</td>
<td align="center"><img src="docs/screenshots/02-editor-fields.png" alt="模型编辑器" width="100%"><br><b>模型编辑器</b>：字段设计器（类型/主键/唯一/可空/业务说明）</td>
</tr>
<tr>
<td align="center"><img src="docs/screenshots/03-graph-canvas.png" alt="模型关系图" width="100%"><br><b>模型关系图</b>：Neo4j 级画布（力导向/分层布局、缩放拖拽、四视图切换）</td>
<td align="center"><img src="docs/screenshots/04-requirements.png" alt="业务需求" width="100%"><br><b>业务需求</b>：主题域卡片 + 关键词/业务域/优先级/状态组合筛选</td>
</tr>
<tr>
<td align="center"><img src="docs/screenshots/05-path-result.png" alt="最短路径与血缘面板" width="100%"><br><b>最短路径与血缘</b>：BFS 路径结果分页 + 快照对比 / 外部血缘导入</td>
<td align="center"><img src="docs/screenshots/06-users-rbac.png" alt="用户与权限" width="100%"><br><b>用户与权限（V1.2 RBAC）</b>：身份能力 / 用户管理 / 项目成员 / API Key 四卡</td>
</tr>
</table>

## 快速开始

```bash
# 1. 安装依赖（Python 3.10+）
pip install -r requirements.txt

# 2. 配置：复制模板生成本地配置（可选，所有项均有默认值）
cp config.example.yaml config.yaml

# 3. 启动（默认 local 环境：自动建库迁移 + 空库注入最小演示数据）
uvicorn backend.app.main:app --host 127.0.0.1 --port 8000

# 4. 打开浏览器
#    前端工作台：  http://127.0.0.1:8000/
#    API 文档：     http://127.0.0.1:8000/docs
```

> 所有配置项集中在 `config.yaml`（模板 `config.example.yaml`，每项含注释），
> 也可用环境变量覆盖（优先级：环境变量 > config.yaml > 默认值），详见[配置](#配置)。

首次启动会在 `data/platform.db` 自动执行 Alembic 迁移（revision `7f0c38e854e5`），
并注入演示数据：项目 `PRJ_RETAIL`（零售分析）、主题域、需求 `REQ_CONTRACT_001`、
已发布模型 `dwd_contract`（V1）与草稿模型 `dim_customer`（含两者间 `joins_to` 关系），可直接体验完整功能。

> 当前开发库已替换为规模更大的**电商数仓演示数据集**（项目 `PRJ_ECOM`，114 个模型），
> 详见下文[电商数仓演示数据集](#电商数仓演示数据集)。需要最小演示集时删除 `data/platform.db`
> 后按上述命令重启即可恢复 `PRJ_RETAIL`。

## 认证模式（V1.0）

`AUTH_MODE` 双模式，启动门禁校验：

- `demo`（默认）：`X-Actor` 演示身份，所有授权检查短路，行为与 V0.9.2 完全一致；
  启动时幂等播种演示管理员（`u:demo.admin`）与每个既有项目的 owner 成员
- `api_key`：`Authorization: Bearer dmc_<key_id>_<secret>`；服务端只存
  `SHA-256(secret)` 摘要（`hmac.compare_digest` 常量时间比较），secret 仅在创建响应返回一次；
  进程内密钥缓存 TTL 60s，吊销/禁用即时失效。首次启用需一次性引导：
  `POST /api/auth/bootstrap`（携带 `BOOTSTRAP_TOKEN`）创建首个平台管理员并返回其密钥

项目级授权三角色：`viewer`（读）< `editor`（写）< `owner`（成员管理）；
平台 admin（`users.is_admin`）旁路全部项目检查。未认证 401、非成员 403
PROJECT_ACCESS_DENIED、角色不足 403 PROJECT_ROLE_INSUFFICIENT。
平台级用户管理与 RBAC 防护规则详见下文「用户与权限（V1.2 RBAC）」。

## 用户与权限（V1.2 RBAC）

两级正交的 RBAC 体系（设计文档 `docs/v1.2/数据建模中心_V1.2_RBAC权限体系设计.md`）：

- **平台级**（管"人"）：唯一角色 admin（`users.is_admin`），能力为
  `user.manage` / `api_key.provision` / `metrics.view`；admin 旁路全部项目检查
- **项目级**（管"事"）：`viewer`（读）< `editor`（写）< `owner`（成员管理）三角色，
  能力随角色递增（`model.read` < `model.write` < `member.manage`）
- **集中式能力矩阵**：`PLATFORM_CAPABILITIES` / `PROJECT_CAPABILITIES` 为单一事实源，
  后端 `/api/auth/me` 的 `capabilities` 字段与前端按权限显隐共用同一份定义
- **八条防护规则（P1-P8）**：P1 最后 admin 保护（409 `ADMIN_LAST_ADMIN`，先于其他
  判定）、P2 禁自降级（`ADMIN_SELF_DEMOTE`）、P3 禁自禁用（`ADMIN_SELF_DISABLE`）、
  P4 成员 upsert 降唯一 owner 保护（`MEMBER_LAST_OWNER`，与 DELETE 对齐）、
  P5 禁用主体不可加成员/发密钥、P6 状态/角色变更即时 invalidate 密钥缓存
  （修复降级 admin 60s 越权窗口）、P7 管理端点 require_admin（403 `ADMIN_REQUIRED`）、
  P8 secret 仅返回一次
- **用户管理 API**（平台 admin 专属）：`GET /api/users`（status/q 过滤）、
  `POST /api/users`（重复 user_key 409 `USER_KEY_EXISTS`，写 user.created 事件+审计）、
  `PUT /api/users/{user_id}`（display_name/is_admin/status 局部更新；未知用户 404，
  空 body 422 `USER_UPDATE_EMPTY`）
- **前端「用户与权限」视图**：四卡布局——当前身份（平台能力徽标 + 项目角色表）、
  用户管理（新建/编辑/禁用/启用，自操作与最后 admin 按钮置灰并提示原因）、
  项目成员（加人/改角色/移除）、API Key（代建/secret 一次性展示+复制/吊销）；
  顶栏身份芯片点击进入该视图；Bearer 令牌登录态（localStorage `dmc_token`，
  令牌失效自动清除并提示重登）

## 配置

运行配置集中在**一个配置文件**中，环境变量可覆盖（适合容器 / CI / 临时覆盖）：

**优先级：环境变量 > `config.yaml` > 内置默认值**

```bash
cp config.example.yaml config.yaml   # 复制模板到仓库根目录，按需修改
```

- 服务默认读取**仓库根目录**的 `config.yaml`（与进程工作目录无关，从 `backend/` 目录内启动也能找到）；
  也可用 `APP_CONFIG_FILE=/path/to/config.yaml` 指定其他路径
- `config.yaml` 已列入 `.gitignore`：本地与服务器配置不会误提交，随仓库分发的只有模板
  `config.example.yaml`（每项配置均含注释与对应环境变量说明）
- 未知配置键启动时直接报错（fail-fast），允许的键以 `config.example.yaml` 为准

| 配置键（config.yaml） | 默认 | 对应环境变量 | 说明 |
| --- | --- | --- | --- |
| `app_env` | `local` | `APP_ENV` | 仅允许 local/dev/development/test；production/staging 仅 `auth_mode=api_key` 放行 |
| `database_url` | `sqlite:///./data/platform.db` | `DATABASE_URL` | 数据库连接串；生产建议 `postgresql+psycopg://user:pass@host:5432/dmc` |
| `auto_migrate` | `true` | `AUTO_MIGRATE` | 启动时自动 `alembic upgrade head` |
| `seed_demo_data` | `true` | `SEED_DEMO_DATA` | 空库时注入最小演示数据（PRJ_RETAIL） |
| `allow_demo_actor_header` | `true` | `ALLOW_DEMO_ACTOR_HEADER` | 是否允许 `X-Actor` 头指定演示身份（仅演示用） |
| `adopt_legacy_schema` | `false` | `ADOPT_LEGACY_SCHEMA` | 一次性接管未版本化的既有库（需显式开启） |
| `log_level` | `INFO` | `LOG_LEVEL` | 日志级别 |
| `data_dir` | `./data` | `DATA_DIR` | 数据目录 |
| `auth_mode` | `demo` | `AUTH_MODE` | `demo`（演示身份，授权短路）/ `api_key`（Bearer 令牌 + 项目授权） |
| `bootstrap_token` | 空 | `BOOTSTRAP_TOKEN` | api_key 模式一次性引导令牌（为空时禁止引导；生产必须设为高强度随机值并视为机密） |
| `full_snapshot_deactivate_ratio_threshold` | `0.5` | `FULL_SNAPSHOT_DEACTIVATE_RATIO_THRESHOLD` | full_snapshot 计划停用比例阈值，超阈值必须 `confirm_deactivation=true` |
| `dbt_manifest_max_bytes` | `8388608` | `DBT_MANIFEST_MAX_BYTES` | dbt manifest 连接器序列化后体积上限（8MiB） |
| `dbt_manifest_max_edges` | `1000` | `DBT_MANIFEST_MAX_EDGES` | dbt manifest 连接器推导边数上限 |
| `auth_cache_ttl_seconds` | `60` | `AUTH_CACHE_TTL_SECONDS` | API Key 认证缓存 TTL（吊销/禁用即时失效不依赖 TTL） |

服务器部署示例（PostgreSQL + api_key 模式）：

```yaml
# config.yaml（节选）
app_env: production
database_url: postgresql+psycopg://dmc:****@db.internal:5432/dmc
auth_mode: api_key
bootstrap_token: "<高强度随机值>"
allow_demo_actor_header: false
seed_demo_data: false
```

## 运行测试

```bash
python -m pytest tests/ -v
```

183 个集成测试覆盖：主链路、乐观并发 409、状态守卫、跨项目边界、
版本 diff、三引擎 DDL、request_id 传播、迁移烟测（upgrade/downgrade/upgrade）、
V0.7 字段设计器（唯一约束/字段 ID 稳定性/默认值白名单/类型参数规则/非草稿 409）、
V0.8 表级约束与索引（CRUD、PK/CHECK/FK 规则、DDL 三引擎约束输出）、
模型关系图（节点/边/字段映射）、变更影响分析（无基线/字段级传播/模型级范围/字段收窄/基线边合并）、
快照差异新类别与 legacy 快照兼容、
V0.9 图谱快照（发布同事务写入/快照列表与详情/不可变性/omitted 边计数）、
图谱四视图（current/published_latest/snapshot/lineage 与 404/422 守卫）、
快照 diff（节点/边增删与属性变更）、最短路径（downstream/upstream/any、NO_PATH、
MAX_DEPTH_REACHED 截断、depth 越界）、外部血缘导入（首次 201/幂等重放 200/409 冲突/
整批原子拒绝与失败批次计数重试/扫描模式规则/scope 隔离与跨 scope 复用/full_snapshot
局部停用/字段与 metadata 限流/幂等重激活）、外部边运维（过滤分页头/停用幂等/
import-runs 过滤/health 分组/stale-candidates 阈值/preview 只读）、
V0.9.2 降级保护（跨 scope 重复数据时 downgrade 中止且 revision 保持 head）、
V1.0 认证与 API Key（Bearer 令牌解析/SHA-256 哈希/secret 一次性返回/吊销即时失效/
BOOTSTRAP_TOKEN 一次性引导）、V1.0 项目成员授权矩阵（viewer/editor/owner 全端点
授权/admin 旁路/USER_NOT_FOUND 与主体代建/最后 owner 保护/denied 审计）、
V1.0 dbt manifest 连接器（parent_map 推导依赖边/元数据白名单/幂等重放/skip_unmatched/
规模上限/scope 隔离）、V1.0 评审上下文（血缘覆盖/graph diff/影响摘要随 submit-review
入响应与事件）、V1.0 停用确认门槛（阈值上下两侧/空扫描/preview 预告/增量不受影响/
failed 重试保留计数/阈值可配置）、V1.0 可观测（modeling_* 指标族计数/summary 增量/
health SQL 聚合与旧实现对拍/api_key 模式 /metrics admin-only 与拒绝计数）、
V1.2 用户管理与 RBAC（/api/auth/me capabilities 两级能力矩阵/用户 CRUD 与筛选/
最后 admin 与自降级自禁用防护/禁用与降级即时失效密钥缓存/成员 upsert 唯一 owner
保护/双 admin 互降后项目旁路失效）。

### 浏览器 E2E（playwright）

先按[快速开始](#快速开始)启动服务，再运行（脚本跨平台，优先使用系统
Chrome / Chromium；无系统浏览器时先 `npx playwright install chromium`
使用内置 Chromium，也可用 `CHROME_PATH` 指定浏览器路径）：

```bash
node scripts/users_rbac_e2e.js        # 用户与权限（V1.2 RBAC）22 项断言
node scripts/walkthrough_all_views.js # 全页面走查 14 项断言
node scripts/ui_revamp_e2e.js         # 工作台图表与 UI 改版 19 项断言
node scripts/neo4j_graph_e2e.js       # Neo4j 画布图谱交互 37 项断言
```

四套共 **92 项断言**，覆盖 RBAC 全流程、页面走查、图表与筛选、图谱画布交互
（缩放/拖拽/聚焦/布局切换/视图切换/路径高亮），均要求控制台零错误；
截图输出到 `.playwright-cli-e2e/`（可用 `E2E_SHOT_DIR` 覆盖）。

## 电商数仓演示数据集

当前开发库预置了一套**电商行业数仓分层**演示数据（项目 `PRJ_ECOM`），
按数据仓库分层设计，覆盖平台全部功能演示场景（尤其是图谱/血缘/快照等大规模场景）：

| 维度 | 规模与覆盖 |
| --- | --- |
| 模型 | **114 个**，五层分布：ODS 28 / DWD 32 / DIM 14 / DWS 22 / ADS 18 |
| 字段 | 801 个 |
| 模型关系 | **159 条**；类型覆盖 depends_on 27 / derived_from 41 / joins_to 84 / references 7；基数覆盖 one_to_one 33 / one_to_many 8 / many_to_one 116 / many_to_many 1 / unspecified 1 |
| 表级约束 | **72 条**：主键 38 / 唯一 11 / 外键 14 / 检查 9 |
| 索引 | **25 个**：btree 21 / hash 3 / gin 1（三型覆盖，用于演示引擎矩阵差异） |
| 状态分布 | published 75 / approved 17 / in_review 13 / draft 9，全状态覆盖；另含 1 个「变更中」草稿（`dwd_ticket_detail`，演示影响分析）与 2 个校验阻断的问题草稿 |
| 需求案例 | 26 条业务需求，覆盖订单、支付、营销、流量、客服、风控等主题域 |
| 图谱资产 | 82 个发布快照、79 条外部血缘边（dbt/外部系统导入）、6 个导入批次（5 成功 + 1 失败，演示重试） |

再生成方法（脚本幂等，可反复执行；当前库已由该脚本生成并通过验收）：

```bash
# 1. 备份并清空当前库
cp data/platform.db data/platform.db.bak-ecom

# 2. 以干净配置重启（SEED_DEMO_DATA=false 表示不注入默认 PRJ_RETAIL 演示数据）
SEED_DEMO_DATA=false python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000

# 3. 生成电商演示数据（约 70s，839 次 API 调用，规格见 scripts/seed_ecommerce_demo.py）
python scripts/seed_ecommerce_demo.py

# 4. API 层验收（29 项断言：图谱四视图 / 最短路径 / 快照 diff / 版本 diff /
#    血缘 health / 影响分析 / 问题草稿 / 三引擎 DDL）
python scripts/verify_ecommerce_demo.py
```

浏览器 E2E（图谱交互 37 项断言，需后端已在 `127.0.0.1:8000` 运行，
脚本会自动解析系统 Chrome / Chromium，详见上文[浏览器 E2E](#浏览器-e2eplaywright)）：

```bash
node scripts/neo4j_graph_e2e.js
```

## 核心业务闭环

```
draft ──submit-review(校验无阻断错误)──▶ in_review ──approve──▶ approved ──publish──▶ published
  ▲                                        │                                              │
  └────────────────reject(必须填意见)────────┘                     start-change ────────────┘
```

- **乐观并发**：更新模型必须带 `expected_revision`，不匹配返回 `409 REVISION_CONFLICT`
- **发布原子性**：版本快照 + 模型状态 + 活动事件 + 审计日志同一事务提交
- **版本语义**：`edit_revision`（草稿修订号）与 `published_version_no`（正式版本号）分离，发布快照不可变
- **规范校验**：22 条规则两级分级（16 条 error 阻断 / 6 条 warning），规则 ID 如
  `MODEL_NAME_REQUIRED`、`DUPLICATE_FIELD_CODE`、`VARCHAR_LENGTH_REQUIRED`、`UNSAFE_DEFAULT_VALUE`、
  `PK_UNIQUE_REDUNDANT`、`PRIMARY_KEY_RECOMMENDED`；V0.8 新增 `TABLE_PK_FIELD_PK_CONFLICT`、
  `CONSTRAINT_FIELD_NOT_FOUND`、`CHECK_EXPRESSION_INVALID`、`INDEX_FIELD_NOT_FOUND`、
  `INDEX_ENGINE_UNSUPPORTED`
- **字段设计器（V0.7）**：快速列表 + 详细配置面板双层交互；14 列 TSV 批量导入（兼容 V0.6 六列）；
  类型参数联动（VARCHAR 建议 255、DECIMAL 建议 18,2）；主键/可空互斥；`is_unique` 单列唯一约束
  （PG/MySQL 输出 `UNIQUE (column)`，Hive 输出逻辑唯一注释）；字段按 `field_code` upsert 保持 ID 稳定；
  默认值白名单（服务端 + DDL 生成双重复检）
- **表级约束与索引（V0.8）**：四种约束类型（primary_key/unique/check/foreign_key）与三类索引
  （btree/hash/gin）设计器；表级主键要求 ≥2 字段且与字段级主键互斥、参与字段自动置非空；
  CHECK 受限语法白名单；外键目标同项目、字段一一对应、ON DELETE 白名单（SET NULL 要求源字段可空）；
  引擎能力矩阵前置拒绝（MySQL 仅 btree、PG 拒绝唯一 GIN），DDL 生成层二次防线；
  仅草稿可编辑（非草稿返回 409 MODEL_NOT_EDITABLE）
- **DDL 预览**：逻辑类型 → PostgreSQL / MySQL / Hive 三引擎映射；**只生成不执行**（execute_on_target=false）；
  PG 输出命名 `CONSTRAINT` 子句 + `CREATE [UNIQUE] INDEX ... USING` 语句，MySQL 反引号 + ENGINE=InnoDB，
  Hive 约束/索引仅逻辑注释并计入 unsupported_features
- **模型关系图与影响分析（V0.8）**：`GET /api/projects/{id}/model-graph` 输出节点（分层/引擎/状态/版本）
  与有向边（关系类型 + 字段映射）；`GET /api/models/{id}/impact` 以当前草稿对比最近发布快照，
  沿 source→target 有向边传播（字段级变更按边字段映射配对传播；模型属性/约束/关系变化升级为模型级
  范围并要求人工确认）；无发布基线时按全模型范围处理；基线边合并（发布后删除关系仍沿历史快照边
  标记 includes_baseline_edge）；结果为**候选**分析，方向语义需与团队规范核对，不能替代真实血缘
- **图谱快照与多视图（V0.9）**：模型发布在同一事务内写入项目图谱快照（`project_graph_snapshots`），
  节点取各模型正式版本快照、边只包含两端均已发布的出向关系，目标未发布的边计入
  `omitted_edge_count`；`graph_hash` 为规范化 JSON 的 sha256。启动时为"有已发布模型但无快照"
  的项目幂等补建 `migration_baseline`。图谱视图四态：`current`（草稿实时）、`published_latest`
  （最新快照，无则 404）、`snapshot`（指定快照号，缺失 422）、`lineage`（设计边 + active 外部边）；
  `diff` 按模型 id/edge_key 对齐输出增删与属性变更；`path` 以 BFS 求边数最少路径（downstream
  正向 / upstream 反向标记 traversal_direction=reverse / any 双向，深度上限处仍有未探索邻居时
  truncated + MAX_DEPTH_REACHED，完整探索无路径为 NO_PATH，max_depth 默认 20 上限 50）
- **外部血缘导入（V0.9/9.1/9.2）**：`external_lineage_edges`（V0.9.2 身份键
  project_id + source_system + scope_key + source_ref，支持 DAG/job group 等扫描 scope 内复用
  局部边 ID；active 软停用）与 `lineage_import_runs`（批次身份键同含 scope_key）。导入三种
  扫描模式：incremental（≥1 边、complete_scan=false）、full_snapshot（complete_scan=true
  且必须非 default scope，空批次只停用本 scope 未出现的旧 active 边）、preview（只读估算）。
  幂等语义：同身份同 payload 重放返回 200 replayed=true，不同 payload 返回 409
  LINEAGE_RUN_ID_REUSED；校验失败（扫描模式规则 → 模型解析 → 自环 → 字段存在与配对 →
  metadata ≤50 键/8000 字节 → 批内 source_ref 去重）整批原子拒绝并落 status=failed 批次
  （attempt/failure 计数保留，可同 payload 重试）。运维端点：import-runs（状态/来源过滤）、
  health（按 source_system + scope 分组：active/inactive、最近成功/失败扫描、连续失败、stale
  候选）、stale-candidates（阈值 1-525600 分钟，只读不自动停用）
- **full_snapshot 停用确认（V1.0 M5）**：计划停用比例
  `planned / max(1, active_total)` 超过 `FULL_SNAPSHOT_DEACTIVATE_RATIO_THRESHOLD`
  （默认 0.5）且未带 `confirm_deactivation=true` 时返回 422
  `LINEAGE_DEACTIVATION_CONFIRM_REQUIRED`（details 含 planned_deactivated/
  active_existing/ratio/threshold/scope_key），并落 failed 批次（attempt/failure 计数）；
  空完整扫描 ratio=1.0 天然落入门槛；preview 提前返回 `planned_deactivate_ratio` 与
  `confirmation_required`，连接器按「preview → 确认 → 提交」两步式适配；
  `lineage_import_runs.deactivation_confirmed` 记录确认标志（import-runs 响应可见）
- **dbt manifest 连接器（V1.0 M3）**：`POST /api/projects/{id}/lineage/import/dbt-manifest`
  以 manifest 的 `parent_map` 推导模型级依赖边（source_ref =
  `dbt:{P.unique_id}=>{N.unique_id}`，manifest 内稳定，支持 upsert 与幂等重放），
  边元数据白名单 6 键；强制 `scan_mode=incremental`、`complete_scan=false`；
  结构非法/超 8MiB/超 1000 边/模型未匹配返回 422（DBT_MANIFEST_INVALID/TOO_LARGE/
  NODES_UNMATCHED），`skip_unmatched=true` 可跳过未匹配节点并计数；连接器信息落
  `run.result_json.connector` 与响应体
- **发布评审上下文（V1.0 M4）**：`GET /api/models/{id}/review-context` 一次返回
  当前校验、变更摘要（对比最近发布快照）、下游影响（候选级）、血缘覆盖
  （出向设计关系无 active 外部边佐证记 uncovered，edge_key =
  `design:{source}:{target}:{type}`）与最近两快照 graph diff；`submit-review` 响应与
  review.submitted 事件 detail 随附同结构 4 字段摘要
- **可观测生产化（V1.0 M6）**：`/metrics` 新增 modeling_* 指标族
  （auth_denied_total{reason}、model_published_total{layer}、
  graph_snapshot_created_total{snapshot_type}、graph_snapshots_current、
  graph_snapshot_nodes/edges、lineage_import_runs_total{status}、
  lineage_import_records_total{result}、lineage_import_conflicts_total、
  lineage_edges_active/inactive；标签不用高基数字段）；
  `/api/observability/summary` 新增 app_version/head_revision/auth/lineage 摘要；
  lineage health 改 SQL 聚合（GROUP BY source_system, scope_key, active，
  批次取最近 200 条，响应契约不变）
- **模型关系图画布（V1.2）**：前端以 neovis.js v2.1.0（Neo4j 官方图谱可视化组件，
  `frontend/static/vendor/neovis.js`）替换原 SVG 图谱。画布交互：拖动平移、滚轮/按钮缩放、
  节点拖动、hover tooltip、单击出详情卡、双击聚焦；详情卡关系列表点击跳转上游/下游模型、
  「聚焦邻居」适配到节点+全部邻居、「打开编辑器」跨视图跳转模型编辑器；边详情可「聚焦两端」。
  力导向 / LR 分层布局切换；四视图（current/published_latest/snapshot/lineage）与最短路径
  查询高亮（聚焦路径节点+边、其余淡化）复用同一画布实例。**适配器架构**：neovis 的
  `dataFunction` 不直连数据库，而是把 `model-graph`（及快照/lineage 视图）JSON 响应转换为
  Neo4j Record 协议（`{keys, _fields}`；Node 带 `labels`、Relationship 带 `type`、identity 用
  `{low, high}` Integer 形状）喂给组件；快照视图边无库 id，由 `edge_key` 经 FNV-1a 哈希派生
  稳定 vis id（`EDGE_HASH_BASE` 起 + 碰撞顺延）。**未来接真 Neo4j**：仅需把 dataFunction
  换成真实 Cypher 查询（记录协议不变），交互/样式/视图逻辑零改动。**最短路径查询**
  独立全宽横排（起终点/方向/depth 一栏排布），结果按路径节点步进分页展示：默认每页 10 条，
  每页数量在结果筛选栏调整（10/20/50/全部），未查询时结果区不占空间；查询同步在图谱高亮
  路径并可一键清除。快照历史与对比、外部血缘导入两列并列展示于路径查询下方。浏览器 E2E
  （`scripts/neo4j_graph_e2e.js`，playwright 直驱 + 系统 Chrome）37 项断言覆盖以上全部交互
  与控制台零错误；已知 vis `fit` 的 scale 上限为 1.0（`moveTo` 不受限），聚焦类断言以
  「目标落入画布视区」为准。
- **工作台图表与明细筛选（V1.1 UI 改版）**：工作台总览在统计卡片之下新增三张原生 SVG
  图表（无外部依赖，颜色与图谱引擎共用同一套分层/状态配色）——各业务域模型分布横向
  柱状图（按数量降序，含"未归属"）、数仓分层占比环形图（中心总数 + 图例计数/百分比）、
  模型状态构成堆叠条形图；柱状图带生长动画与原生 tooltip。业务需求明细新增组合筛选：
  关键词（标题/需求编码）+ 业务域 + 优先级 + 状态四维过滤，实时结果计数（"筛选出 M / N 条"）、
  一键清空与无结果空态。全局布局现代化：内容区限宽 1560px 居中、卡片大圆角 + 分层阴影、
  统计卡渐变底色与悬停浮起、按钮/输入框焦点环、表格行悬停过渡、顶栏显示当前项目上下文、
  自定义滚动条与选区配色；窄屏（≤860px）侧栏自动收为图标模式。
- **用户与权限（V1.2 RBAC）**：两级正交权限体系——平台级唯一 admin 角色
  （`user.manage`/`api_key.provision`/`metrics.view`，旁路全部项目检查）与项目级
  viewer/editor/owner 三角色（`model.read` < `model.write` < `member.manage`）；
  集中式能力矩阵（`PLATFORM_CAPABILITIES`/`PROJECT_CAPABILITIES`）同时驱动后端
  `/api/auth/me` 的 capabilities 与前端按权限显隐；八条防护规则覆盖锁死（最后 admin、
  自降级/自禁用、成员唯一 owner）与越权窗口（禁用/降级即时失效密钥缓存）；
  「用户与权限」视图四卡（当前身份/用户管理/项目成员/API Key）+ 顶栏身份芯片 +
  Bearer 令牌登录态（失效自动清除重登）

## API 一览（68 个端点：67 个 /api + /metrics）

- 健康：`/api/health/live`、`/api/health/ready`、`/api/health`
- 项目/主题域/需求：CRUD + 需求-模型关联
- 模型：CRUD（乐观并发）、`/validate`、`/submit-review`、`/approve`、`/reject`、`/publish`、`/start-change`
- 表级约束：`/api/models/{id}/constraints` CRUD（`/approve` 前需 in_review 状态）
- 索引：`/api/models/{id}/indexes` CRUD
- 关系：CRUD（自引用/跨项目/字段对校验）
- 关系图/影响：`GET /api/projects/{id}/model-graph`、`GET /api/models/{id}/impact?field_codes=&max_nodes=`
- 图谱快照（V0.9）：`GET /api/projects/{id}/model-graph/snapshots`、`.../snapshots/{snapshot_no}`、
  `GET /api/projects/{id}/model-graph/diff?from_snapshot_no=&to_snapshot_no=`、
  `GET /api/projects/{id}/model-graph/path?source_model_id=&target_model_id=&direction=&view=&snapshot_no=&max_depth=`
- 外部血缘（V0.9）：`POST /api/projects/{id}/lineage/import`（幂等 upsert）、
  `POST /api/projects/{id}/lineage/preview`（只读预检）、
  `GET /api/projects/{id}/lineage/edges`（分页头 X-Total-Count/X-Limit/X-Offset）、
  `POST /api/projects/{id}/lineage/edges/{edge_id}/deactivate`、
  `GET /api/projects/{id}/lineage/import-runs`、`GET /api/projects/{id}/lineage/health`、
  `GET /api/projects/{id}/lineage/stale-candidates`
- 认证与授权（V1.0）：`POST /api/auth/bootstrap`（一次性引导）、`GET /api/auth/me`、
  `GET/POST /api/auth/api-keys`（secret 仅创建返回一次）、
  `POST /api/auth/api-keys/{key_id}/revoke`
- 项目成员（V1.0）：`GET/POST /api/projects/{id}/members`、
  `DELETE /api/projects/{id}/members/{user_id}`（owner 专属；最后 owner 409 保护）
- 用户管理（V1.2）：`GET /api/users`（status/q 过滤）、`POST /api/users`、
  `PUT /api/users/{user_id}`（display_name/is_admin/status；平台 admin 专属，
  最后 admin/自降级/自禁用 409 防护）
- dbt 连接器（V1.0）：`POST /api/projects/{id}/lineage/import/dbt-manifest`
- 评审上下文（V1.0）：`GET /api/models/{id}/review-context`
- 版本：`/versions`、`/versions/diff`（含 constraints/indexes/relationships 差异类别）
- DDL：`POST /api/models/{id}/ddl`
- 看板/事件：`/api/dashboard/{pid}`、`/api/projects/{pid}/events`
- 观测：`/api/observability/summary`、`/metrics`（Prometheus 文本；api_key 模式需 admin）

错误体统一为 `{"error": {"code", "message", "details"}, "request_id"}`，
`X-Request-ID` 贯穿响应头、错误体、日志、事件与审计。

## 目录结构

```
config.example.yaml  应用配置模板（复制为 config.yaml 使用；config.yaml 不进版本库）
.github/             GitHub Actions CI（pytest，Python 3.11–3.13 矩阵）
.gitignore           版本库忽略规则（本地库/备份库/构建产物/开发机残留）
LICENSE              MIT 许可证
backend/app/       FastAPI 应用（config/db/models/schemas/validation/ddl/auth/seed/main）
migrations/        Alembic 迁移（0001 初始 6 表 + 0002 V0.6 增量 4 表 1 列 + V0.7 is_unique
                   + V0.8 model_constraints/model_indexes + V0.9 三表
                   + V0.9.1 scope 治理列 + V0.9.2 scope 身份键
                   + V1.0 users/api_keys/project_members + deactivation_confirmed）
frontend/static/   原生 HTML/CSS/JS 单页工作台（含 Neo4j 画布图谱（neovis.js）、
                   约束/索引设计器、影响分析、图谱视图切换/快照浏览器/
                   路径查询/血缘导入面板、工作台 SVG 图表与需求明细筛选、
                   用户与权限管理（令牌登录/身份芯片/四卡视图））
scripts/           浏览器 E2E（e2e_launch.js 跨平台启动 helper、
                   neo4j_graph_e2e.js 图谱画布 37 项、
                   users_rbac_e2e.js 用户与权限 22 项、
                   walkthrough_all_views.js 全页面走查 14 项、
                   ui_revamp_e2e.js UI 改版 19 项；playwright 直驱系统 Chrome）、
                   演示数据种子（seed_graph_demo.py、seed_ecommerce_demo.py
                   电商数仓 114 模型 + check_seed_spec.py 规格静态校验 +
                   verify_ecommerce_demo.py API 层 29 项验收）、
                   发布打包（package_release.py：python scripts/package_release.py v1.2
                   → data-modeling-center-v1.2.zip，含源码/文档/演示库）
tests/             pytest 集成测试（183 个）
docs/              需求与技术设计文档、API 契约（openapi.json 当前版本 /
                   API 接口契约.json 早期契约）、迁移脚本、ERD、界面截图
                   （screenshots/；v1.0/、v1.1/、v1.2/ 为各版本设计资料）
```

## 已知限制（V1.0 边界）

- `AUTH_MODE=demo` 仅适合本地演示（`X-Actor` 身份可伪造）；生产须 `api_key` 模式，
  且当前仅覆盖本服务自身密钥，未接 OAuth/LDAP 等外部身份源
- RBAC 仅内置「平台 admin + 项目三角色」，不支持自定义角色；账号锁死的恢复路径为
  停服务直接改库（无逃生舱 API），见 V1.2 设计文档「明确不做的事」
- 模型关系图画布（neovis.js）当前经 dataFunction 适配器消费 REST 图谱接口，
  未直连真实 Neo4j 数据库；切换点仅为 dataFunction（记录协议不变），交互与样式无需改动
- DDL 只预览不执行；血缘图谱目前覆盖外部边导入、dbt manifest 参考连接器与查询，
  ETL 开发、数据质量监控为后续横向扩展方向
- 变更影响分析为候选级：方向语义来自手工配置的关系，评审人员必须人工核对，
  未建模的外部依赖可能导致漏报
- 血缘 health/stale-candidates 为 SQL 聚合与只读候选，无自动告警与通知渠道；
  full_snapshot 停用有比例门槛，但仍需连接器先 preview 确认最近一次完整扫描成功
- `/metrics` 为进程内计数，重启归零，不可用于多实例聚合；graph_snapshot_nodes/edges
  为抓取时实时求和，大规模项目应改预计算
