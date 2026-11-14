# 数仓治理与运维平台

面向数据仓库团队的治理与运维一体化平台。覆盖表生命周期管理、存储成本优化、数据热度分析、任务依赖编排、SLA告警、资源弹性调度与数仓健康度评估的完整治理链路。

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
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 表生命周期管理 | `lifecycle` | 生命周期策略 | 策略编号、表名称、创建日期 |
| 存储成本优化 | `cost` | 成本分析报告 | 报告编号、分析范围、存储总量 |
| 数据热度分析 | `heatmap` | 热度记录 | 记录编号、表名称、查询次数 |
| 任务依赖编排 | `dag` | 依赖图节点 | 节点编号、节点名称、上游节点 |
| SLA告警管理 | `sla` | SLA规则 | 规则编号、监控对象、产出时间 |
| 资源弹性调度 | `resource` | 资源池配置 | 配置编号、资源类型、最小配额 |
| 数仓健康度评估 | `score` | 健康度报告 | 报告编号、评估范围、存储健康分 |
| 数据漂移检测 | `drift` | 漂移记录 | 记录编号、表名称、漂移字段 |
| 数据资产盘点 | `catalog` | 资产记录 | 资产编号、表名称、资产分类 |
| 数据保留策略 | `retention` | 保留规则 | 规则编号、数据集范围、保留期限 |
| 数据去重管理 | `dedup` | 去重任务 | 任务编号、目标表名、去重字段 |
| 数据分层规划 | `tier` | 分层定义 | 分层编号、分层名称、层级别名 |
| 基线监控管理 | `baseline` | 基线配置 | 基线编号、基线名称、承诺时间 |
| 数据回收站 | `recycle` | 回收记录 | 记录编号、表名称、删除时间 |
| 变更审计追踪 | `audit` | 变更记录 | 记录编号、变更对象、变更类型 |
| 查询性能分析 | `benchmark` | 性能报告 | 报告编号、查询语句、执行耗时 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 变更审计事件链

`audit`、`catalog`、`retention`、`lifecycle` 四个模块的写入统一经过
`backend/app/services/audit_ledger.py` 的事件账本，别的模块只读投影：

- 版本键为 `业务对象编号|变更对象`，乱序事件按 `事件序号` 还原版本链；
- 幂等键重放不追加副本，同槽位冲突顺延下一版本，复核状态单调前进；
- 每条事件保留来源模块、业务对象主键、父事件序号/指纹、幂等键与摘要、
  本地序列号、落库批次戳、校验码和稳定排序键；
- 当前状态进生命周期结果清单，历史链保留完整版本，归档事件仍参与读取。

写入调用链、版本流程图与归并窗口设计见 [`docs/audit-event-flow.md`](docs/audit-event-flow.md)。

```bash
# 后端行为测试（乱序还原、重放幂等、并发争抢、归档一致性）
cd backend && .venv/bin/python -m unittest discover -s tests -v
```

领域：数据工程
子方向：数仓与数据湖
