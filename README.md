# 核验零碳运输走廊减排结果协同基础服务

本项目提供综合交通运输业务共享的服务端基础能力，负责运营机构、交通节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域服务可以在这些稳定边界上扩展自己的状态、规则和接口。

`corridor_carbon` 在该基础上实现**零碳运输走廊碳核算与核验**：

- 路段边界、车辆与能源类型、排放因子、基准方案、证据有效期政策、走廊目标**全部按版本只追加保存**，新版本生效时旧版本标记为 `superseded` 但永不删除；
- 核算批次冻结时把行程、载货量、补能记录、车队/站点双侧电力声明、主数据**精确版本指针**与凭证登记内容一并快照，形成 `manifest_hash` 与 `result_hash`，任何历史报告都可按原输入复算；
- 载货量迟到、补能记录缺失、双侧申报量不一致、凭证撤回或超期等情形一律进入**补证清单**，行程标记为 `pending_evidence`，实际排放为 `null`、减排记 0，**绝不按零排放计入**；
- 绿色电力凭证先登记后占用：车队侧声明与站点侧记录按凭证逐笔匹配，只有双侧一致才产生唯一占用；冻结时预留容量、发布时结清、重述/吊销时释放，`BEGIN IMMEDIATE` 事务内完成容量校验，防止跨批次族重复计算；
- 批次状态机 `frozen → verified → published`，核验人角色必须为 `reviewer` 且不能是批次冻结人，复算哈希一致且无未解决阻断性发现才能发布；
- 迟到数据、因子修订、凭证撤回只生成**重述（restatement）或吊销（revocation）版本**，旧报告与旧输入原样保留；吊销后可用更正重述重新发布；
- 提供每吨减排到有效行程的解释接口、走廊目标完成口径（`published`/`verified` 两种口径）、历史发布版本清单与凭证撤回完整性预警。

## 目录

- `src/transport_coordination/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `src/corridor_carbon/`：碳核算版本化主数据、证据规则、凭证占用、批次冻结/核验/发布/重述/吊销、HTTP 路由和离线验收；
- `tests/`：基础规则、事务边界、接口路由、碳核算规则/服务/接口和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m transport_coordination.acceptance
```

验收命令会在临时 SQLite 数据库中登记运营机构、操作者、交通节点和参考资料，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database transport_coordination.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。

## 碳核算 HTTP 服务

```bash
PYTHONPATH=src python3 -m corridor_carbon.api --database corridor_carbon.sqlite3 --host 127.0.0.1 --port 8090
```

同一进程同库提供基础建档接口（`/organizations`、`/actors` 等）与碳核算接口：

| 类别 | 接口 |
| --- | --- |
| 版本化主数据 | `POST /segments` `/energy-types` `/emission-factors` `/baselines` `/evidence-policies` `/targets`；`GET /master-versions/{type}/{id}` |
| 绿证 | `POST /certificates`、`POST /certificates/{id}/withdraw` |
| 批次 | `POST /batches/freeze`、`POST /batches/restate`、`GET /batches`、`GET /batches/{id}` |
| 核验发布 | `POST /batches/{id}/verify` `/publish` `/revoke`，`POST /batches/{id}/findings` |
| 复算与解释 | `GET /batches/{id}/recompute` `/explain` `/deficits` `/findings` |
| 走廊口径 | `GET /corridors/{id}/progress`、`GET /corridors/{id}/reports` |

所有写接口都要求 `request_id`（幂等键）。`explain` 返回每个有效行程的基准排放、实际排放、减排量与占比、绿证匹配电量及占用状态；`recompute` 用批次冻结的原始输入与精确主数据版本重新计算并比对 `result_hash`。

## 碳核算离线验收

```bash
PYTHONPATH=src python3 -m corridor_carbon.acceptance
```

验收脚本演示完整业务故事：三条行程初版冻结（载货量迟到、车队与站点申报冲突两条进入补证，只有证据完整行程计入 0.108 tCO₂）→ 独立核验拒绝自审 → 发布 → 迟到数据/冲突更正后重述为 0.276 tCO₂ → 绿证撤回触发吊销 → 以有效凭证更正重述重新发布，同时验证历史初版报告仍按原输入复算、哈希不变、主数据版本全部保留。
