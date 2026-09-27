# 教材选用回避审查

纯 Python 服务端项目：教材选用评审的申请—退回—再次提交—批准全流程，提供版本化状态、幂等命令、回避审查、SQLite 持久化审计和 JSON API 边界。

## 设计要点

- **连续关系**：退回后重新提交沿用同一 `case_id`，版本号与提交轮次连续递增；历次审查意见作为不可变事件永久保留，重新提交不开新案、不丢历史。
- **权限与回避**：批准/退回仅审批角色可执行；申请人本人及与候选教材供应方存在申报关联的人员一律回避，无权者不得代替审批者作决定。
- **幂等与并发**：变更命令必须携带 `idempotency_key` 与 `expected_version`。重复点击返回首次结果；两名审批者同时处理时只有第一个命令生效，其余收到 409 及当前状态说明——同一版本只形成一个可解释的结果。
- **审计**：每次状态变化追加一条事件（操作者、前后状态、原因、轮次、时间），`GET /cases/{id}/history` 可完整还原每次变化及其原因。

## 状态机

`draft →(submit) reviewing →(return) returned →(resubmit) reviewing →(approve) approved`；`draft/returned →(cancel) cancelled`。

## API

- `POST /cases` `{id, applicant, supplier, title, idempotency_key}`
- `POST /cases/{id}/submit|resubmit|return|approve|cancel` `{actor, expected_version, idempotency_key, reason?, note?}`（`return` 必须填 `reason` 审查意见）
- `GET /cases`、`GET /cases/{id}`、`GET /cases/{id}/history`
- `POST /relationships` `{person, supplier}` 申报关联、`GET /relationships`

错误码：400 参数或状态不合法，403 无权限/应回避，404 不存在，409 版本冲突。

测试命令：python3 -m unittest discover -s tests -v

编译命令：python3 -m compileall -q service_09261_013 tests
