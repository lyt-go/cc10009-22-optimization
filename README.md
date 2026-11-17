# 分布式网页抓取调度平台

面向分布式网页抓取任务调度、节点协同、请求分配、反爬策略编排、数据校验入库与抓取质量大盘的采集工程管理平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
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
| 采集节点 | `worker` | 工作节点 | 节点编号、主机地址、并发槽位数 |
| 请求分配 | `dispatch` | 分配策略 | 策略编号、目标域名、分配算法 |
| 抓取作业 | `crawljob` | 作业定义 | 作业编号、目标站点、起始URL |
| URL过滤 | `urlfilter` | 过滤规则 | 规则编号、包含模式、排除模式 |
| DNS解析 | `dnsresolver` | 解析配置 | 配置编号、域名模式、DNS服务器 |
| 内容提取 | `contentextract` | 提取器 | 提取器编号、内容类型、正文选择器 |
| 存储策略 | `storagestrategy` | 存储方案 | 方案编号、存储类型、连接参数 |
| 渲染任务 | `rendertask` | 渲染作业 | 作业编号、目标URL、浏览器版本 |
| 采集质量 | `qualityreport` | 质量报告 | 报告编号、作业编号、总URL数 |
| 带宽控制 | `bandwidth` | 带宽策略 | 策略编号、节点标识、下载带宽上限 |
| 重试队列 | `retryqueue` | 重试条目 | 条目编号、失败URL、失败原因码 |
| 翻页管理 | `pagemarker` | 翻页规则 | 规则编号、目标站点、翻页类型 |
| 增量标记 | `incrementmark` | 增量水位 | 水位编号、目标源、增量字段名 |
| 配置同步 | `configsync` | 同步任务 | 任务编号、源节点、目标节点 |
| 采集API | `scrapeapi` | API定义 | API编号、API名称、请求路径 |
| 数据抽样 | `datasample` | 抽样方案 | 方案编号、数据源、抽样方式 |
| 采集告警 | `alertpolicy` | 告警策略 | 策略编号、监控指标、触发条件 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。
- 静态路由（如 `/export`）必须声明在 `/{entry_id}` 之前，否则会被参数路由抢先匹配。

## 增量质量重算与导出复核

采集质量不再每次全量重算，数据流如下（跟着数据流查找）：

```text
POST /api/qualityreport/ingest        采集质量回执 → 作业结果(crawlresult)
POST /api/qualityreport/{id}/recalc   冻结快照(qualitysnapshot) → 增量重算
                                      → 交付对象(qualitydelivery：质量结论 + 报告导出结果数据集)
GET  /api/qualityreport/{id}/deliveries[/{version}]   逐项复核
GET  /api/contentextract/versions     结构化提取版本数据（可复用字段覆盖结果）
GET  /api/contentextract/manifest     正文解析侧清单（结果摘要 + 回写的报告导出结果）
```

- 只读取受影响作业结果与结构化提取版本数据；未变化作业不重新扫描，复用缓存字段，
  且复用记录可回到原始 URL 与提取版本、不另起分支。
- 质量重算与导出共用同一冻结快照，结果写成一份交付对象；快照水位之后写入的数据不进导出。
- 已确认报告保持不可变，增量重算只生成新版本；回执编号与写入序号保证重复/迟到结果不倒退版本。
- 报告导出按操作键幂等，重复操作只生效一次；并发回执不会多出第二份结果。
- 同一作业出现两个版本依据（新结果 vs 旧提取版本缓存）时判定为冲突：导出结果对应两份记录，
  冲突记录优先级为高、排在复核前列。
- 指标为零或字段缺失时仍形成可解释结论（写入「解释」字段，字段覆盖率缺失按 0 计入并标注）。
- 测试：`cd backend && .venv/bin/python tests/test_incremental_quality.py`

领域：数据工程
子方向：数据采集、爬虫与RPA
