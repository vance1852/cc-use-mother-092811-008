# 核验零碳运输走廊减排结果协同基础服务

本项目提供综合交通运输业务共享的服务端基础能力：在运营机构、交通节点、操作者和
结构化参考资料登记之上，内置**零碳运输走廊碳核算与独立核验**子系统。所有能力仅依赖
Python 标准库和 SQLite，提供角色权限、请求幂等、SQLite 事务与哈希串联审计。

## 目录

- `src/transport_coordination/`
  - 基础能力：`service.py`（主体/场所/资料）、`storage.py`（事务与建表）、
    `audit.py`（哈希审计链）、`clock.py`（可替换时钟）、`api.py`（HTTP 边界）；
  - 碳核算：`carbon_models.py`（不可变数据对象）、`carbon_engine.py`（纯函数核算
    引擎，无 IO）、`carbon_service.py`（配置版本化、批次冻结、核验发布、重述吊销）、
    `carbon_api.py`（碳核算路由）；
  - 离线验收：`acceptance.py`（基础能力）、`carbon_acceptance.py`（完整碳核算链）。
- `tests/`：基础规则、引擎口径、服务事务边界、HTTP 路由和端到端验收测试。

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
PYTHONPATH=src python3 -m transport_coordination.carbon_acceptance
```

碳核算验收覆盖：行程结束后补传载荷与充电来源 → 首次冻结进入补证、补证后重新冻结 →
站点/车队重复申报冲突 → reviewer 独立核验（不得是冻结人）→ auditor 发布（不得是
核验人）→ 因子修订产生重述版本 → 凭证撤回产生吊销版本 → 三个历史版本均按原输入
复算且哈希一致 → 走廊目标只汇总每个批次族最新发布版本。成功时输出 `status` 为
`ok` 的 JSON 并以退出码 `0` 结束。

## 核算口径（corridor-carbon-v1）

- **范围**：行程时间落在批次周期内、路段属于冻结时的边界版本。
- **缺证不零排**：载荷缺失/冲突/超过证据有效期、补能记录缺失、充电来源缺失/冲突/
  过期的行程整条排除并生成补证单，绝不按零排放处理；核验存在未处理补证单时不能通过。
- **基准排放（柴油重卡）**：基准油耗 × 里程 × 柴油因子 × 载荷系数；载荷系数
  `0.5 + 0.5 × min(实际载荷/额定载荷, 1)`。
- **实际排放（电动重卡）**：车型电耗 × 里程；被有效绿电凭证匹配的电量用可再生
  电力因子，**其余电量一律用区域电网因子**（有充电无凭证不等于零排放）。
- **减排量**：逐行程 `max(基准排放 − 实际排放, 0)` 求和，解释接口按行程列明每一吨
  减排来自哪个行程、哪张凭证、用了哪些因子版本。

## 版本化与可复算

- 路段边界、车辆与能源类型、排放因子、基准方案、证据有效期、走廊目标均按版本保存，
  旧版本永不删改。
- 批次冻结时把行程、载荷、补能、凭证声明和所引用配置**的确切版本与数值**拍成
  canonical JSON 快照（`snapshot_hash`），由纯函数引擎算出结果（`result_hash`）。
  `POST /carbon/batches/{id}/recompute` 用原输入重算并逐字节比对。
- 迟到数据、因子修订、凭证撤回**只能创建新版本**：`restated`（重述）或
  `revoked`（吊销），旧版本原样保留；重述被独立核验驳回时，自动恢复此前发布版本的
  效力。走廊目标只汇总每个批次族（batch_id）最新发布版本，不会重复计算。

## 凭证防重复计算

- 每个补能事件至多有一个 `accepted` 申报；车队与站点对同一事件的第二次申报落为
  `rejected_conflict` 并产生补证单。
- 冻结时在 IMMEDIATE 事务内原子校验：批次内有效需求 + 其他批次族已占用电量
  ≤ 凭证总量。批次内超订、跨批次超用均直接冻结失败，不留占用。
- 占用生命周期：`held`（冻结待核验）→ `committed`（发布锁定）；驳回 `released`，
  被新版本替代 `superseded`。凭证撤回不删除历史，只能触发吊销重述。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database service.sqlite3 \
    --host 127.0.0.1 --port 8080
```

健康检查 `GET /health`（含审计链校验）。写入接口通过 `X-Actor-Id` 标识操作者；
幂等写接口需带 `request_id`。主要碳核算路由：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/carbon/corridors` | 登记走廊（admin） |
| POST | `/carbon/corridors/{id}/boundary-versions` | 发布路段边界版本 |
| POST | `/carbon/corridors/{id}/target-versions` | 发布减排目标口径 |
| POST | `/carbon/vehicle-types` | 发布车型/能源类型版本 |
| POST | `/carbon/factors` | 发布排放因子版本（含适用区间） |
| POST | `/carbon/baselines` | 发布基准方案版本 |
| POST | `/carbon/evidence-policies` | 发布证据有效期策略 |
| POST | `/carbon/trips`、`/carbon/trips/{id}/payload` | 行程登记、载荷补传 |
| POST | `/carbon/energy-events`、`.../{id}/source` | 补能登记、来源补报 |
| POST | `/carbon/certificates`、`.../{id}/withdrawal` | 凭证登记、撤回 |
| POST | `/carbon/claims`、`/carbon/claims/{id}/void` | 凭证申报、裁定撤销 |
| POST | `/carbon/batches` | 创建核算批次 |
| POST | `/carbon/batches/{id}/freeze` | 冻结输入并立即核算 |
| POST | `/carbon/batches/{id}/review` | reviewer 独立核验 |
| POST | `/carbon/batches/{id}/publish` | auditor 发布 |
| POST | `/carbon/batches/{id}/restate` | 创建重述/吊销新版本 |
| POST | `/carbon/batches/{id}/recompute` | 按原快照复算并比对哈希 |
| GET | `/carbon/batches/{id}/explanation` | 逐行程解释减排来源 |
| GET | `/carbon/batches/{id}/versions` | 批次版本链 |
| GET | `/carbon/corridors/{id}/progress` | 目标完成口径 |
| GET | `/carbon/evidence` | 补证单查询 |
| POST | `/carbon/evidence/adjudicate` | 核验员裁定补证单 |

服务重启后 SQLite 中的配置版本、运营数据、批次快照和审计历史继续保留。
