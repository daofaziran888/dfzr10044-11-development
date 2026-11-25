# 湖表工坊 · 湖表分区演化与小文件归并平台

面向湖表目录登记、表结构演进、分区规划与演化、小文件归并、快照与时间旅行、入湖水位推进、压实调度、统计画像、隔离区处置、消费视图发布与存储分层的湖表治理平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/views/QualityVersioningView.vue  结构版本化质量校验工作台
│   ├── src/views/QuarantineDetailView.vue   隔离详情页（复检/回炉/版本核对）
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/routers/quality_versioning.py  结构版本化校验/复检/回炉接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/quality_versioning.py 按结构版本校验转换引擎
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8001/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5174/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8001`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 湖表目录 | `lake_catalog` | 湖表条目 | 表编号、表名全称、所属库域 |
| 结构演进 | `table_schema` | 结构版本 | 版本编号、所属湖表、列清单 |
| 分区规划 | `partition_plan` | 分区方案 | 方案编号、所属湖表、分区键 |
| 分区演化 | `partition_evolve` | 演化记录 | 演化编号、所属湖表、原分区键 |
| 小文件归并 | `small_file_merge` | 归并任务 | 归并编号、所属湖表、目标分区 |
| 快照管理 | `snapshot_mgr` | 湖表快照 | 快照编号、所属湖表、快照标识 |
| 时间旅行 | `time_travel` | 回溯查询 | 查询编号、所属湖表、目标快照 |
| 入湖水位 | `ingest_watermark` | 水位记录 | 水位编号、所属湖表、水位字段 |
| 入湖批次 | `ingest_batch` | 入湖批次 | 批次编号、所属湖表、来源系统 |
| 写入作业 | `write_job` | 写入作业 | 作业编号、所属湖表、写入模式 |
| 压实作业 | `compact_job` | 压实作业 | 压实编号、所属湖表、触发条件 |
| 过期策略 | `expire_policy` | 过期策略 | 策略编号、所属湖表、保留快照数 |
| 统计画像 | `stats_profile` | 统计画像 | 画像编号、所属湖表、统计粒度 |
| 列统计 | `column_stat` | 列统计 | 统计编号、所属湖表、列名称 |
| 入湖质量规则 | `quality_rule` | 质量规则 | 规则编号、所属湖表、校验列 |
| 隔离区 | `quarantine_zone` | 隔离记录 | 隔离编号、所属湖表、来源批次 |
| 重建补数 | `rebuild_backfill` | 补数任务 | 补数编号、所属湖表、补数窗口 |
| 存储分层 | `storage_tier` | 分层规则 | 分层编号、所属湖表、热层判定 |
| 保留窗口 | `retention_window` | 保留窗口 | 窗口编号、所属湖表、保留粒度 |
| 消费视图 | `publish_view` | 消费视图 | 视图编号、所属湖表、视图定义 |
| 资源额度计量 | `quota_meter` | 额度读数 | 读数编号、所属湖表、额度类型 |
| 湖表审计 | `lake_audit` | 审计条目 | 条目编号、所属湖表、操作类型 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 结构版本化质量校验（按结构版本校验/转换/复检/回炉）

入湖批次固定采集当时读到的**结构版本**，质量规则按版本发布不可变**规则快照**（阈值随版本冻结）。
实现位于 `app/services/quality_versioning.py`、`app/lake_store.py`，接口前缀
`/api/quality-versioning`，前端入口为导航中的「版本化质量校验」与隔离详情页。

核心保证：

- **批次保留结构版本与原输入**：`BatchRecord.schema_version_id` 固定采集版本，
  转换只读不改原输入；V1 的 `amount`（元）经版本链换算为 V2 的 `amount_cents`（分，×100）。
- **在途校验阻塞冲突发布**：评估会话处于 `validating` 时，同湖表的新结构版本生效、
  新规则版本发布都会冲突（HTTP 409）；确认（`confirmed`）后才放行。
- **同一套完整结构与规则复算**：通过与隔离明细都按评估时冻结的版本计算，
  70 元（=7000 分）不会因读错量纲被隔离，130 元（=13000 分）不会因换字段无故通过。
- **隔离区可追溯**：行级隔离记录保存初次判定的结构/规则版本、原输入与历次
  `initial/recheck/retry` 计算轨迹；复检默认沿用隔离记录固定版本。
- **跨湖表同名版本拒绝**：结构版本号全局唯一，复检/转换引用其他湖表的同名版本直接报错。
- **回炉幂等**：通过原回炉接口（`/quarantine/{id}/retry`）提交更正输入，走同一评估链路；
  落湖台账按 `(湖表, 行主键)` 去重，同一笔有效数据只出现一次，重复重投被拒绝。
- **回执可追溯**：回炉回执同时给出原批次、原输入参与的初次评估与更正输入参与的回炉评估。

主要接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/quality-versioning/schemas` | 登记结构版本（列、量纲、跨版本转换映射） |
| POST | `/api/quality-versioning/schemas/activate` | 生效结构版本（在途冲突返回 409） |
| POST | `/api/quality-versioning/rule-versions` | 发布不可变规则版本（绑定结构版本） |
| POST | `/api/quality-versioning/batches` | 按结构版本接收入湖批次 |
| POST | `/api/quality-versioning/batches/convert` | 批次沿版本链转换（只读） |
| POST | `/api/quality-versioning/evaluations` | 启动按结构版本的在途校验 |
| POST | `/api/quality-versioning/evaluations/{id}/confirm` | 确认评估：通过入台账、违规入隔离 |
| GET | `/api/quality-versioning/quarantine/{id}` | 隔离详情（初次版本/原输入/计算轨迹） |
| POST | `/api/quality-versioning/quarantine/{id}/recheck` | 按同一套结构规则复检（不落湖） |
| POST | `/api/quality-versioning/quarantine/{id}/retry` | 原回炉接口重试更正输入（幂等落湖） |
| GET | `/api/quality-versioning/ledger` | 有效数据幂等落湖台账 |

领域：数据工程
子方向：数仓与数据湖
