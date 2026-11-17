# 数据分析实验与数据应用服务平台

面向指标口径管理、分析实验设计、OLAP查询、数据看板构建与数据API发布的数据应用平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
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

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 指标定义 | `metric_def` | 指标 | 指标编号、指标名称、计算公式 |
| 维度管理 | `dimension` | 分析维度 | 维度编号、维度名称、维度类型 |
| 数据集管理 | `dataset` | 分析数据集 | 数据集编号、数据集名称、来源类型 |
| 分析实验 | `experiment` | 实验方案 | 方案编号、实验名称、分析数据集 |
| OLAP查询 | `query_builder` | 查询模板 | 模板编号、模板名称、查询语句 |
| 数据看板 | `dashboard` | 看板配置 | 看板编号、看板名称、包含图表 |
| 图表配置 | `chart` | 图表 | 图表编号、图表名称、图表类型 |
| 数据API发布 | `data_service` | 数据服务 | 服务编号、服务名称、输出数据集 |
| 智能告警 | `alert` | 告警规则 | 规则编号、告警名称、监控指标 |
| 分析用户组 | `user_group` | 用户组 | 组编号、组名称、组描述 |
| 数据快照 | `snapshot` | 快照记录 | 快照编号、数据集名称、快照时间 |
| 任务调度 | `scheduler` | 调度任务 | 任务编号、任务名称、调度类型 |
| 缓存管理 | `cache` | 缓存配置 | 缓存编号、缓存目标、缓存策略 |
| 分析管道 | `pipeline` | 分析节点 | 节点编号、输入数据集、分析算子 |
| 分析模型 | `model` | 模型配置 | 模型编号、模型名称、模型类型 |
| 数据集市 | `data_mart` | 集市表 | 表编号、表名称、主题域 |
| 数据导出 | `export` | 导出任务 | 任务编号、导出对象、导出格式 |
| 数据审计 | `audit` | 审计记录 | 记录编号、操作类型、操作人 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据分析与数据应用

## 指标口径发布版本包

指标页（`/metric_def`）在原有指标定义之上补了一层“口径发布”能力：

- **提交公式**：`POST /api/metric_def/{id}/formula`。已发布指标会保留原公式作为基准，
  调整后的公式落到同一指标的新草稿，草稿/已定义/已发布记录可以同时存在。
- **发起版本校验**：`POST /api/metric_def/{id}/validate`。按指标、统计粒度和时间窗口解析
  计算公式，在**同一个发布边界**内生成公式快照、基准快照和差异明细；按业务日期重算与
  按统计粒度整组重算会落到不同的差异明细范围。
- **发布版本包**：`POST /api/metric_def/releases/{id}/actions`，动作包含发布、驳回、废弃。
  发布新版本会把同一指标旧的已发布版本包标为已废弃，唯一有效结果不会并存。
- **差异核对**：`GET /api/metric_def/releases/{id}/diff`。接口只读取版本包快照与主版本逐项核对，
  不会在查看差异时重新读取仍在变化的指标草稿；`/export` 可导出差异明细 CSV。
- **来源追溯**：`GET /api/metric_def/releases/{id}/sources` 从版本包回到指标草稿与历史发布记录。
- **图表取值**：`GET /api/chart/{id}/metric`（或 `GET /api/metric_def/{id}/chart`）。图表只读取
  一份完整的已发布版本包，口径版本与指标值来自同一个包，不会读到新口径配旧基准的错配值。

阈值规则默认是差异率 ±10%：版本包生成时逐条判断，超出范围的记录带统一口径说明，
`超阈值条数` 会体现在列表、差异接口和图表快照中。并发提交使用同一把发布锁串行化，
相同请求指纹只保留一个版本包，同一发布边界上的竞争公式按冲突边界拦下。

后端规则测试：

```bash
cd backend
.venv/bin/python -m unittest discover -s tests -v
```

