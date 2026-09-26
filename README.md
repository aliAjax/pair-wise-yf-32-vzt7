# 器官分配与转运协调系统

Python 标准库独立项目。系统按器官类型、血型、地域、医疗匹配、紧急程度和等待时间排序候选患者，并管理提出、接受、转运、交接、植入或撤回流程。器官过期后所有继续流转操作都会被阻止，全部状态变化写入审计记录。

## 运行

```bash
python3 app.py --db organ_allocation.db
```

默认监听 `127.0.0.1:8203`，首页 `/`，健康检查 `/health`。

身份头：`X-User-Id`、`X-Role`。角色为 `viewer`、`hospital`、`coordinator`、`allocation_officer`、`auditor`；医院角色还需 `X-Hospital`。

## 主要接口

- `POST /api/donors`、`POST /api/candidates`：登记器官与候选患者。
- `GET /api/donors/{id}/ranking`：查看兼容候选排序。
- `POST /api/allocations`：提出唯一分配，可携带 `response_deadline`（ISO 8601 应答截止时间，须晚于当前时间且不晚于器官可用结束时间）。
- `POST /api/allocations/{id}/accept`、`withdraw`：医院确认或撤回；超过应答截止时间确认会收到 `offer_expired`（失去本次机会）。
- `POST /api/allocations/{id}/transit`、`delay`：冷链转运和延误上报。
- `POST /api/allocations/{id}/handoff`、`handoff-accept`：来源医院发起、接收医院确认。
- `POST /api/allocations/{id}/implant`：确认植入。
- `GET /api/allocations/{id}/audit`、`GET /api/state`：完整审计和权限视图。

## 限期应答与顺延

- 提出分配时写明 `response_deadline`，接收医院需在期限内确认；未带截止时间的旧分配按原方式处理，不受期限限制。
- 协调员或分配员查看 `GET /api/state`（协调台）时，系统把已逾期仍未确认的分配按原候选排序顺延给下一位仍符合条件（在册、愿意、血型兼容）且未拒绝过本次分配的患者，并按原应答时长生成新的截止时间。
- 每次顺延写入 `allocation_defers`，保留双方患者、双方医院、时间和原因，同时记录审计事件 `allocation_deferred`；没有可顺延的候选时分配关闭（`allocation_lapsed`），器官回到可分配状态。
- 分配详情和协调台展示 `current_hospital`（当前医院）与 `deferral_count`（顺延次数），分配详情还包含完整 `defers` 顺延历史。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

血型兼容与评分是演示规则，不包含 HLA 分型、器官大小、病程、儿科差异和真实移植网络规则。医院身份使用请求头模拟，SQLite 环境适合原型，不处理跨机构身份信任、远程患者隐私协议和真实冷链设备接入。
