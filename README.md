# 器官分配与转运协调系统

Python 标准库独立项目。系统按器官类型、血型、地域、医疗匹配、紧急程度和等待时间排序候选患者，并管理提出、接受、转运、交接、植入或撤回流程。提出分配时写明应答截止时间，接收医院须在期限内确认；期限过后再确认会收到失去机会提示，协调员查看协调台时系统按原排序把名额顺延给下一位仍符合条件且未拒绝过本次分配的患者，每次顺延保留双方患者、时间和原因。器官过期后所有继续流转操作都会被阻止，全部状态变化写入审计记录。

## 运行

```bash
python3 app.py --db organ_allocation.db
```

默认监听 `127.0.0.1:8203`，首页 `/`，健康检查 `/health`。

身份头：`X-User-Id`、`X-Role`。角色为 `viewer`、`hospital`、`coordinator`、`allocation_officer`、`auditor`；医院角色还需 `X-Hospital`。

## 主要接口

- `POST /api/donors`、`POST /api/candidates`：登记器官与候选患者。
- `GET /api/donors/{id}/ranking`：查看兼容候选排序。
- `POST /api/allocations`：提出唯一分配。可带 `respond_by`（ISO 时间）写明应答截止时间，默认 120 分钟窗口；显式传 `null` 表示旧方式（不设期限、不顺延）。截止时间必须晚于当前且不晚于器官可用窗口结束。
- `POST /api/allocations/{id}/accept`、`withdraw`：医院确认或撤回。期限过后确认返回 409 `opportunity_lost` 并立即顺延；待应答名额被撤回按拒绝处理并自动顺延，已接受后的撤回保持原流程。
- `POST /api/allocations/{id}/transit`、`delay`：冷链转运和延误上报。
- `POST /api/allocations/{id}/handoff`、`handoff-accept`：来源医院发起、接收医院确认。
- `POST /api/allocations/{id}/implant`：确认植入。
- `GET /api/allocations/{id}/audit`、`GET /api/state`：完整审计和权限视图。协调员/分配员打开协调台（`/api/state`）时自动扫描逾期名额并顺延；分配视图含 `respond_by`、`current_hospital`、`escalation_count`、后续候选 `queue` 和每轮留痕 `offers`（医院视图遮蔽无关患者信息，且不返回 `queue`；另含本院 `missed_offers`）。

## 限期应答与顺延

- 每次提出/顺延都在 `allocation_offers` 记录一轮：轮次、双方患者与医院、提出时间、应答时间、原因（`initial`、`accepted`、`response_timeout`、`rejected`、`rollover` 等）。
- 顺延时按原评分排序选择下一位"仍 active、willing、血型器官匹配"的患者，已拒绝或错过本次分配的患者不再参与；顺延给下一位后重新计时（默认窗口，截止时间不超过器官过期时间）。
- 所有候选都轮过后名额变为 `timed_out`，器官释放为 `available`，可重新提出分配；顺延、超时关闭、错过确认等均写入审计。
- 无 `respond_by` 的旧分配完全按原方式处理，不参与超时扫描。旧库启动时自动迁移（新增字段、应答留痕表、"仅有效分配唯一"索引）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

血型兼容与评分是演示规则，不包含 HLA 分型、器官大小、病程、儿科差异和真实移植网络规则。医院身份使用请求头模拟，SQLite 环境适合原型，不处理跨机构身份信任、远程患者隐私协议和真实冷链设备接入。
