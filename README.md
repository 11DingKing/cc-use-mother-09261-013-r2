# 教材选用回避审查

教材选用评审服务：申请人与候选教材供应方存在关联时要求回避；申请、退回、再次提交、
批准全程保留在同一案件上；每次状态变化都有可审计的原因记录。

## 状态机

```
draft → reviewing → returned → reviewing → … → approved
```

- `submit`：申请人提交（draft→reviewing）或退回后重新提交（returned→reviewing，
  round 递增）。重新提交沿用同一案件 id，历史审查意见保留在事件日志中，不会丢失。
- `return`：审批者退回（reviewing→returned），必须填写审查意见。
- `approve`：审批者批准（reviewing→approved）。

## 回避与权限

- 只有登记的审批者（`approvers`）能作出退回/批准决定；
- 申请人不得审查自己的申请，也不得由他人代为提交申请；
- 与案件供应方存在关联（`associations` 登记的 (人员, 供应方)）的审批者必须回避；
- 申请人创建案件时申报是否与供应方关联（`applicant_has_association`），
  申报后案件标记 `recusal_required` 并写入审计日志。

## 幂等与并发

- 命令可携带 `idempotency_key`：重复提交返回首次结果，不产生重复事件；
  同一键被不同命令复用返回 409。
- 状态变更命令必须携带 `expected_version`：两人同时处理同一申请时只有一人成功，
  另一人收到 409 及当前状态说明，全程只形成一个可解释的结果。

## 审计

`GET /cases/{id}/history` 返回该案件的全部事件（动作、操作人、前后状态、原因、
时间、版本），可还原每次状态变化及其原因。SQLite 持久化案件快照、事件日志与
幂等键，重启后状态与历史连续。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | /cases | 创建申请（id, applicant, textbook, supplier, applicant_has_association?, idempotency_key?） |
| POST | /cases/{id}/submit | 提交/重新提交（actor, expected_version, reason?, idempotency_key?） |
| POST | /cases/{id}/return | 退回（actor, expected_version, reason 必填, idempotency_key?） |
| POST | /cases/{id}/approve | 批准（actor, expected_version, reason?, idempotency_key?） |
| GET | /cases | 案件列表 |
| GET | /cases/{id} | 单个案件 |
| GET | /cases/{id}/history | 审计事件链 |

错误码：404 不存在 / 403 无权限或需回避 / 409 状态或版本冲突、幂等键复用 /
422 参数或校验失败。

测试命令：python3 -m unittest discover -s tests -v

编译命令：python3 -m compileall -q service_09261_013 tests
