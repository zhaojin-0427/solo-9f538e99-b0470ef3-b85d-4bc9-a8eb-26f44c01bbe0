# 单浮标潮位校时服务

浮标每次重启都会把毫秒计数归零，潮位分析却需要一条可信的 UTC 时间线。
本服务（FastAPI + SQLite）接收「开机段 + 段内计数」的观测与校时点，
把计数线性换算成 UTC 毫秒，并维护观测的可用状态供分析查询。

## 快速开始

```bash
docker compose up --build
# 服务监听 http://localhost:8000 ，交互式文档在 http://localhost:8000/docs
```

本地开发（Python 3.11+）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
pytest                 # 运行测试
```

数据默认存放在命名卷 `buoy-data` 的 `/data/buoy.db`（可用环境变量
`BUOY_DB` 覆盖）。SQLite 为单写者模型，容器以单进程运行。
旧版数据卷可直接挂入：启动时自动补建段内号观测表，既有全局号数据原样可查。

## 概念与规则

- **开机段（boot_segment）**：一次开机周期。计数只在段内单调，重启归零。
- **校时点（calibration point）**：`(开机段, 计数) -> UTC 毫秒`。
  同段同计数重复提交时新值替换旧值。
- **观测（observation）**：`(编号, 开机段, 计数, 潮位)`。编号有两种，
  同一批提交可混用，每条只填一个：
  - **全局号 `obs_id`**：全库唯一。同号重复提交覆盖原文并重算，
    可跨开机段更正（记录器换了段也用同号更正旧数据）。
  - **段内号 `local_obs_id`**：仅在开机段内唯一。只覆盖本段的同号观测；
    记录器重启后新段的同号读数**另存一条**（如重启前后的两条 42 号并存），
    也可与同名全局号并存、互不干扰。

### 校时换算

- 计数落在相邻两个校时点之间时线性换算并**向下取整**：
  `utc = t_lo + (c - c_lo) * (t_hi - t_lo) // (c_hi - c_lo)`。
- 恰好命中校时点时直接取该点 UTC；只有一个校时点时仅该计数可换算。
- 计数落在本段校时区间之外：保留原文，状态置 `pending_calibration`（待校时）。

### 观测状态机

| 状态 | 含义 | 是否供分析 |
|---|---|---|
| `ok` | 已校时且无冲突 | ✅ |
| `pending_calibration` | 计数在校时区间外，待校时 | ❌ |
| `conflict` | 不同开机段落在同一 UTC 毫秒 | ❌ |

- **补传/更正校时点**会重算受影响观测（区间扩大可让待校时观测转为可用）。
- **冲突**：同一 UTC 毫秒被 ≥2 个不同开机段的观测占用时，这些观测全部
  置为 `conflict` 不供分析；**两类编号一同判定**。更正校时点或覆盖观测
  解除冲突后自动恢复供数。同一开机段内多条观测共用同一毫秒不算冲突。
- **整批拒绝**：校时点合并后若使任一段内 UTC 随计数倒退，整个校时批次
  以 `409` 拒绝并回滚，原文、校时点与供数保持不变。

### 原子性

每批提交（原文 + 校时点 + 重算 + 冲突刷新）都在**单个 SQLite 事务**中
完成，并由进程级锁串行化读写：原文、校时依据、分析供数同次生效，
查询和（崩溃后的）重启都不会看到半次重算的中间态。

## API

均可在 `/docs` 中直接调用。

### `POST /observations` — 分批提交观测

每条只填全局号 `obs_id` 或段内号 `local_obs_id` 之一，同一批可混用：

```json
{
  "observations": [
    {"obs_id": "obs-1", "boot_segment": "boot-1", "counter": 1500, "tide_level": 2.34},
    {"local_obs_id": "42", "boot_segment": "boot-2", "counter": 60, "tide_level": 2.41}
  ]
}
```

### `POST /calibrations` — 分批提交校时点

```json
{
  "calibrations": [
    {"boot_segment": "boot-1", "counter": 1000, "utc_ms": 1700000000000},
    {"boot_segment": "boot-1", "counter": 2000, "utc_ms": 1700000001000}
  ]
}
```

两个提交接口的响应都会列出**本次提交中时刻或状态发生变化的观测**：
`changed` 为全局号回执（含新观测，以及被卷入/解除冲突的其他观测），
`segment_changed` 另列新增、时刻或状态变化的段内号观测
（带开机段、段内号、UTC 毫秒和状态）：

```json
{
  "changed_count": 1,
  "changed": [{"obs_id": "obs-1", "utc_ms": 1700000000500, "status": "ok"}],
  "segment_changed_count": 1,
  "segment_changed": [
    {"boot_segment": "boot-2", "local_obs_id": "42",
     "utc_ms": 1700000000060, "status": "ok"}
  ]
}
```

### `GET /tide-levels?start_ms=...&end_ms=...` — 按 UTC 范围查可用潮位

只返回 `status=ok` 的观测，区间含端点，按 UTC 毫秒升序。
默认仅返回全局号观测；加 `include_segment_ids=true` 时一并返回可用的
段内号观测（条目带 `boot_segment` 与 `local_obs_id`）。

### `GET /observations/{obs_id}` — 按全局号查状态和原因

返回原文、换算结果、状态、原因与校时依据（用于换算的上下界校时点）：

```json
{
  "obs_id": "obs-1", "boot_segment": "boot-1", "counter": 1500,
  "tide_level": 2.34, "utc_ms": 1700000000500, "status": "ok",
  "reason": null,
  "calibration": {
    "lo": {"counter": 1000, "utc_ms": 1700000000000},
    "hi": {"counter": 2000, "utc_ms": 1700000001000}
  }
}
```

### `GET /segments/{boot_segment}/observations/{local_obs_id}` — 按开机段和段内号查

返回段内号观测的原文与校时状态，字段与全局号详情一致
（`local_obs_id` 取代 `obs_id`）。

### 其他

- `GET /health` — 健康检查
- `GET /docs` — Swagger UI；`GET /openapi.json` — OpenAPI 模式

## 目录结构

```
app/
  main.py   # FastAPI 路由与请求/响应模型
  core.py   # 校时换算、批量写入、重算、冲突检测、变更快照
  db.py     # SQLite 连接、模式、事务与锁
tests/
  test_api.py
Dockerfile
docker-compose.yml
```
