# 母婴照护交接

本项目面向月子中心护理团队，关联母亲与婴儿的照护记录、重要提醒和跨班次责任确认。服务使用 Python（FastAPI + SQLite），保留实际发生时间、补录时间及交接更正过程。

## 解决的问题

夜班接班时，喂养量写在纸卡上、用药提醒留在群聊里，交接双方都无法确认夜间哪些观察已完成。本服务把母亲与婴儿分别建档，让喂养、睡眠、排泄、用药协助、情绪观察和护理备注按**实际发生时间**进入同一照护时间线，并以交接单机制保证：未确认事项逐项交接、重要提醒被明确接收前不消失、已签署的交接只能更正不能篡改。

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --port 8000   # 交互式文档：http://localhost:8000/docs
.venv/bin/python -m pytest                   # 运行测试（32 个用例）
```

## 核心规则

| 规则 | 实现 |
| --- | --- |
| 母婴分别建档，共用一条照护时间线 | `care_events` 以 `subject_type`（mother/baby）+ `subject_id` 归属，按 `occurred_at` 排序 |
| 只能提交自己负责时段的事实 | 事件的 `occurred_at` 必须落在操作人某个班次内，否则 403 |
| 迟到补录保留双时间 | `occurred_at`（发生）与 `recorded_at`（录入）都落库，超阈值标记 `late_entry` |
| 重复提交不产生第二条记录 | `client_request_id` 唯一约束；重复提交返回原记录（200），同键不同内容返回 409 |
| 接班人逐项确认 | 交接事项（未接收的重要提醒 + 未关闭的持续关注）在创建交接单时快照，逐项 confirm 后才能 sign |
| 重要提醒不自动消失 | 未被 `ack` 或被交接确认接收的提醒，自动结转到下一班次的交接清单 |
| 已签署交接只能更正 | 签署后确认/再签署/重建交接均 409，只能追加 `corrections` 说明差异，原记录不变 |
| 组长总览 | `GET /handovers/{id}/lead-view`：未确认事项、异常变化、责任人、迟到补录、更正经过 |
| 家属端可见性 | 仅 `shareable=1` 且经护理组长 `center-review` 确认的内容，且不暴露内部字段 |

并发安全：确认、签署等多步写操作在 `BEGIN IMMEDIATE` 事务内以条件更新（`UPDATE ... WHERE status='pending'`）判定成败；幂等性由唯一约束保证。SQLite 开启 WAL 与 busy_timeout。

## API 概览

所有写操作通过请求头 `X-Staff-Id` 标识操作人。

### 建档
- `POST /staff` `{name, role: nurse|lead_nurse}` — 护理人员
- `POST /mothers` / `POST /babies` — 母亲、婴儿档案（婴儿关联母亲）
- `POST /shifts` `{staff_id, start_at, end_at, label}` — 排班（可跨午夜，绝对时间）

### 照护事件
- `POST /events` — 录入事件。字段：`client_request_id`（幂等键）、`subject_type/subject_id`、`category`（feeding/sleep/excretion/medication/mood/note）、`occurred_at`、`payload`（JSON 明细），标志位 `is_important`、`requires_followup`、`abnormal`、`shareable`
- `POST /events/{id}/ack` — 明确接收重要提醒（幂等）
- `POST /events/{id}/resolve` — 关闭持续关注事项
- `POST /events/{id}/center-review` — 中心确认（仅护理组长），可同时调整 `shareable`
- `GET /timeline?subject_type=&subject_id=[&start=&end=]` — 员工时间线（按发生时间排序）
- `GET /family/timeline?subject_type=&subject_id=` — 家属端（仅已确认且允许共享）

### 交接
- `POST /handovers` `{shift_id, incoming_staff_id}` — 交班人为本班次创建交接单，自动生成事项清单
- `GET /handovers/{id}` — 交接单与事项明细
- `POST /handovers/{id}/items/{item_id}/confirm` — 接班人逐项确认（重复确认 409）
- `POST /handovers/{id}/sign` — 全部确认后签署
- `POST /handovers/{id}/corrections` — 已签署交接的更正说明（可关联具体事件）
- `GET /handovers/{id}/lead-view` — 护理组长总览（仅组长）

## 项目结构

```
app/
  db.py        # SQLite 连接、schema、BEGIN IMMEDIATE 事务助手
  timeutil.py  # UTC 时间规范化（字符串比较即时间比较）
  models.py    # 请求模型
  services.py  # 全部业务规则
  main.py      # FastAPI 路由与依赖注入
tests/
  test_profiles_and_timeline.py   # 建档与统一时间线
  test_shift_scope.py             # 只能提交本人时段的事实
  test_idempotency.py             # 重复提交不产生第二条记录
  test_handover_cross_midnight.py # 跨午夜交接全流程
  test_important_reminders.py     # 重要提醒接收与结转、关注事项关闭
  test_offline_backfill.py        # 离线补录：双时间、去重、排序、结转
  test_concurrency.py             # 并发确认/签署/重复提交
  test_corrections.py             # 已签署交接只能更正
  test_family_view.py             # 家属端可见性与角色权限
```

## 设计说明

- **时间表示**：所有时间以 UTC ISO-8601 字符串存储，字典序即时间序，跨午夜班次用绝对时间自然表达，无需特殊处理。
- **交接事项快照**：创建交接单时收集「未接收的重要提醒」与「未关闭的持续关注」（含历史遗留），此后新补录的未接收提醒自动进入下一班次的清单，保证不漏。
- **确认即接收**：接班人确认「重要提醒」事项时同步写入接收记录（`acknowledgements`），一条提醒只要被任何人明确接收，就不再出现在后续交接中。
- **不可篡改**：交接单一班次一张（唯一约束），签署后状态机锁死，差异只能以更正事件追加，更正记录含责任人与时间，组长总览完整呈现。
