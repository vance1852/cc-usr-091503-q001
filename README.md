# 母婴照护交接

本项目面向月子中心护理团队，关联母亲与婴儿的照护记录、重要提醒和跨班次责任确认。服务使用 Python（FastAPI + SQLite），保留实际发生时间、补录时间及交接更正过程。

## 需求与设计对应

| 需求 | 实现 |
| --- | --- |
| 母亲与婴儿分别建档 | `mother` / `baby` 两张档案表，婴儿关联母亲；`POST /mothers`、`POST /babies` |
| 六类事项进入同一时间线 | `care_event` 统一表：`feeding / sleep / excretion / medication / mood / note`，`GET /timeline` 按 `occurred_at`（实际发生时间）排序 |
| 交班人只能提交自己负责时段的事实 | 事件落库前校验 `occurred_at` 必须落在记录人本人的班次窗口内（`shift` 表），否则 403；未来时间拒绝 |
| 接班人逐项确认未完成事项 | 交接单提交时自动快照「需跟进事件 + 未签收重要提醒」为 `handover_item`，接班人逐项 `confirm`，全部确认后才能 `sign` |
| 重要提醒被明确接收前不消失 | `is_important` 事件在 `GET /reminders/pending` 中常驻，直到 `acknowledge` 或接班人确认对应提醒事项（`acknowledgement` 表） |
| 迟到补录保留双时间 | `occurred_at`（发生时间，客户端给）与 `recorded_at`（录入时间，服务端写）分列存储；间隔超过 10 分钟自动标记 `is_backfill` |
| 重复提交不生成第二条记录 | 客户端幂等键 `idempotency_key` 唯一约束；重试返回原记录（200），同键不同内容返回 409 |
| 已签署交接只能通过更正说明差异 | 签署后该班次时段锁定，直接写事件返回 409；差异通过 `POST /events/{id}/corrections`（amend/void）或 `POST /handovers/{id}/corrections`（add 漏录）记录，原始事件保留 |
| 组长视图 | `GET /handovers/{id}/leader-summary`：未确认事项、异常变化（`is_abnormal`）、责任人（记录人/确认人/更正人）、更正经过 |
| 家属端 | `GET /family/timeline` 只返回组长审核确认（`center_confirmed`）且允许共享（`shareable_with_family`）的内容，不含工作人员信息；被更正事件展示生效内容 |

## 运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload        # 数据库默认 ./care.db，可用 CARE_DB 覆盖
```

交互文档：`http://127.0.0.1:8000/docs`。所有写操作与员工视图需请求头 `X-Staff-Id`（演示用身份约定，生产环境应替换为正式认证）；组长接口额外要求 `role=leader`。

## 测试

```bash
.venv/bin/python -m pytest -q
```

覆盖：统一时间线排序与班次时段约束、幂等去重、交接全流程与提醒签收、签署后更正、**跨午夜交接**（`test_cross_midnight.py`）、**并发确认/并发重复提交/并发签署**（`test_concurrency.py`，多线程 + 多客户端）、**离线补录与已签署时段补登**（`test_offline_backfill.py`）、家属端可见性。

## 典型流程

```bash
H_OUT='X-Staff-Id: 1'; H_IN='X-Staff-Id: 2'; H_LEAD='X-Staff-Id: 3'
# 1. 建档：员工、母亲、婴儿
curl -X POST localhost:8000/staff  -d '{"name":"李护士"}'               -H 'Content-Type: application/json'
curl -X POST localhost:8000/mothers -d '{"name":"王女士","room":"301"}' -H 'Content-Type: application/json'
curl -X POST localhost:8000/babies -d '{"mother_id":1,"name":"小宝"}'   -H 'Content-Type: application/json'
# 2. 开班并记录事件（occurred_at 为实际发生时间，支持离线补录）
curl -X POST localhost:8000/shifts -H "$H_OUT" -d '{"staff_id":1}' -H 'Content-Type: application/json'
curl -X POST localhost:8000/events -H "$H_OUT" -H 'Content-Type: application/json' -d '{
  "idempotency_key":"feed-2300","subject_type":"baby","subject_id":1,
  "category":"feeding","occurred_at":"2026-09-19T23:00:00+08:00",
  "payload":{"amount_ml":80,"note":"亲喂+瓶补"},"needs_followup":true}'
# 3. 交接：建单 → 提交（快照未完成事项）→ 接班人逐项确认 → 签署
curl -X POST localhost:8000/handovers -H "$H_OUT" -d '{"shift_id":1,"incoming_staff_id":2}' -H 'Content-Type: application/json'
curl -X POST localhost:8000/handovers/1/submit -H "$H_OUT"
curl -X POST localhost:8000/handovers/1/items/1/confirm -H "$H_IN"
curl -X POST localhost:8000/handovers/1/sign    -H "$H_IN"
# 4. 签署后发现差异：只能更正
curl -X POST localhost:8000/events/1/corrections -H "$H_OUT" -H 'Content-Type: application/json' -d '{
  "action":"amend","reason":"奶量誊写错误","new_payload":{"amount_ml":60,"note":"亲喂+瓶补"}}'
# 5. 组长视图与家属端
curl localhost:8000/handovers/1/leader-summary -H "$H_LEAD"
curl -X POST localhost:8000/events/1/family-review -H "$H_LEAD" -d '{"shareable":true}' -H 'Content-Type: application/json'
curl 'localhost:8000/family/timeline?subject_type=baby&subject_id=1'
```

## 并发与一致性设计

- SQLite WAL + `busy_timeout`，每请求独立连接；写操作统一 `BEGIN IMMEDIATE` 事务串行化。
- 确认事项用条件更新 `UPDATE ... WHERE status='pending'`，并发下只有一方影响行数非零，另一方收到 409——不会重复确认。
- 幂等键唯一约束在数据库层兜底，断网重试、批量重发、并发提交都只会留下一条记录。
- 事件只增不改：作废/修改全部走 `correction` 更正事件，原始内容与更正经过同时可查。

## 目录结构

```
app/
  main.py      # FastAPI 路由与依赖注入（X-Staff-Id 鉴权、组长校验）
  services.py  # 业务规则：时段校验、交接快照、确认/签署、更正、家属可见性
  db.py        # SQLite schema、连接、事务
  schemas.py   # 请求模型
  utils.py     # UTC 时间工具、事件类别中文名
tests/         # pytest：时间线/交接/跨午夜/并发/离线补录/家属端
```
