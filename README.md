# 退役电芯成组放行 API

基于 FastAPI + SQLite 的退役电芯成组放行服务：录入待选电芯（最多 12 节），按串联成组
约束预选最优组合，确认时在单个 SQLite 事务中原子占用整组并保存不可改写的检测快照；
已确认组可登记为 2~4 组的可追溯交付批次，批次按当前隔离记录判定整批是否可交付；
交付批次可按批次号确认发运一次，发运时间与批次关联不可改写，发运后新增隔离会让
已发运批次显示为需追溯。

## 快速启动

### Docker（一条命令）

```bash
docker compose up --build
```

服务监听 **8000** 端口：<http://localhost:8000>（交互文档 <http://localhost:8000/docs>）。
SQLite 数据持久化在 `battery-data` 卷中。

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 运行测试

```bash
pip install pytest httpx
python -m pytest tests/ -q
```

## 业务规则

- **录入**：最多 12 节待选电芯，字段为 ID、化学体系、容量(Ah)、内阻(mΩ)、检测版本。
- **复测修订**：`PUT /cells/{id}/test` 更新容量/内阻并提升检测版本。确认前复测会使
  已预选候选过期；确认后复测只更新电芯档案，不改变已确认组的快照。复测**不能解除**
  已提交的隔离。
- **安全隔离**：`POST /cells/{id}/isolate` 提交隔离原因，记录一次**不可撤销**的隔离
  （无解除接口，数据库触发器禁止 UPDATE/DELETE）。电芯不存在返回 404；重复隔离返回
  首次隔离的原记录（200），不新增、不改写。预选自动排除隔离电芯；确认事务会重查
  隔离状态，命中即整组拒绝。已入组电芯被隔离后，该组在 `GET /groups` /
  `GET /groups/{group_id}` 中显示 `blocked: true` 及涉及的电芯 ID 与原因，原检测
  快照与组占用保留；未受影响的组 `blocked: false`，保持可放行。
- **预选**（只读，不占用）：从同一化学体系、未占用、未隔离、容量不低于最低单芯容量
  的电芯中选出 `series_count`（3~6）节，要求组内容量差、内阻差均不超过上限。目标：
  **使组内最小容量最大**；同值时按电芯 ID 排序序列的字典序取最小者。返回候选 ID、
  受限容量（= 组内最小容量）及各芯检测版本；无可行组合时返回原因。
- **确认**：在**一次 SQLite 事务**（`BEGIN IMMEDIATE`）中重查所有电芯的检测版本、
  未占用状态与隔离状态，全部一致才原子地占用整组、写入组记录与检测快照；任何一节
  版本已变化、已被别组占用或已被隔离，则**整组拒绝、回滚，不产生组也不留部分占用**。
- **快照不可改写**：`groups` / `group_cells` / `isolations` 表由触发器禁止
  UPDATE/DELETE，可按组编号 `GET /groups/{group_id}` 查询，后续检测修订与隔离
  不影响已确认组的快照内容。
- **交付批次**：`POST /batches` 提交 2~4 个**不重复**的已确认组号，登记为可追溯的
  交付批次。建批在**一次 SQLite 事务**（`BEGIN IMMEDIATE`）中重查全部成员组：仅接收
  **同一化学体系、当前未被隔离阻断且尚未归属其他批次**的组；有组不存在、已阻断或被
  另一批次先行占用，则**整批拒绝、回滚，不产生批次也不留部分归属**。批次成员关系
  固定（`batches` / `batch_groups` 由触发器禁止 UPDATE/DELETE），每组在全库最多归属
  一个批次（唯一约束兜底）。`GET /batches` / `GET /batches/{batch_id}` 返回固定成员
  关系、各组**确认时的检测快照**，并按**当前隔离记录**给出整批 `deliverable` 与
  受影响的组号、电芯 ID 和原因（`blocked_groups`）；后续隔离会阻断已建批次
  （`deliverable: false`），但不改写成员关系与原快照。建批与隔离走同一写路径，
  并发时以**提交先后**决定是拒绝建批（隔离先提交）还是让已建批次转为阻断
  （建批先提交）；批次查询在同一已提交快照内完成，不会拼出跨快照状态。
- **发运**：`POST /batches/{batch_id}/ship` 按批次号确认发运一次。发运在
  **一次 SQLite 事务**（`BEGIN IMMEDIATE`）中重查：批次存在（否则 `404`）、
  尚未发运（否则 `409` 重复发运明确拒绝）、全部成员当前未被隔离（否则 `409`
  拒绝发运）；任一条件不满足即**拒绝、回滚，不留发运记录**。成功后写入**不可改写**
  的发运时间与批次关联（`shipments` 表由触发器禁止 UPDATE/DELETE，每批次最多一条
  发运记录），返回批次号、成员组号和各组原检测快照。发运与隔离走同一写路径，
  并发时以**提交先后**决定：隔离先提交则拒绝发运；发运先提交则记录保留，此后隔离
  让已发运批次在 `GET /batches` / `GET /batches/{batch_id}` 中显示
  `trace_required: true`（需追溯）及受影响的组号、电芯 ID 和原因
  （`blocked_groups`）。批次列表与按号查询同时呈现历史发运事实（`shipped` /
  `shipped_at`）与当前隔离状态，单次响应来自同一已提交快照；未发运批次
  （`shipped: false`、`shipped_at: null`）继续按原规则查询。
- **并发可见性**：每个请求使用各自线程的独立 SQLite 连接（WAL 模式），查询只能
  看到**已提交的完整快照**。确认/隔离事务提交前，其他请求看不到其中间状态；提交
  成功的瞬间整组（组记录 + 快照 + 占用）或隔离记录一次性可见，回滚则毫无痕迹。
  **单次查询内各电芯的占用、组编号与隔离标志取自同一已提交快照**：`GET /cells`、
  `GET /cells/{id}`（以及录入、复测返回电芯档案的响应）都在一个只读事务内完成、
  或以一条 `LEFT JOIN` 同时读取 `cells` 与 `isolations`，因此在"读请求先读到
  占用信息 → 另一请求确认含该电芯的组并提交、再隔离该电芯并提交 → 读请求随后才读
  隔离信息"这种交错下，只可能返回**确认前**（`occupied=false, group_id=null,
  isolated=false`）或**确认/隔离后**（`occupied=true, group_id=<组号>,
  isolated=true`）的完整状态，绝不会拼出 `occupied=false` 同时 `isolated=true`
  这种不存在的混合状态；未提交修改始终不可见。隔离与确认同时发生时，以**提交先后**
  决定是拒绝候选（隔离先提交）还是阻断新组（确认先提交），查询不会显示半成品。
  并发查询不会阻塞、也不会干扰写事务。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/cells` | 录入电芯（最多 12 节） |
| GET | `/cells` / `/cells/{id}` | 查询电芯（含 `isolated` 隔离标记） |
| PUT | `/cells/{id}/test` | 复测修订（更新容量/内阻/检测版本，不解除隔离） |
| POST | `/cells/{id}/isolate` | 安全隔离（不可撤销；重复提交返回原记录） |
| GET | `/isolations` | 隔离记录列表（受影响范围追溯） |
| POST | `/preselect` | 预选成组（只读，排除已隔离电芯） |
| POST | `/groups/confirm` | 确认候选（单事务重查版本/占用/隔离，原子占用 + 快照） |
| GET | `/groups` / `/groups/{group_id}` | 查询已确认组：检测快照 + 当前阻断状态 |
| POST | `/batches` | 登记交付批次（2~4 个不重复组号；单事务重查，独占归属） |
| GET | `/batches` / `/batches/{batch_id}` | 查询批次：固定成员 + 各组检测快照 + 整批可交付状态 + 发运事实 |
| POST | `/batches/{batch_id}/ship` | 按批次号确认发运（一次；单事务重查存在/未发运/未隔离） |

## 调用示例（curl）

### 1. 录入电芯

```bash
for i in 1 2 3 4 5 6; do
  curl -s -X POST http://localhost:8000/cells \
    -H 'Content-Type: application/json' \
    -d "{\"cell_id\":\"C00$i\",\"chemistry\":\"LFP\",\"capacity\":$((101-i)).0,\"resistance\":2.$i,\"test_version\":\"T1\"}"
  echo
done
```

### 2. 预选（4 节串联，最低单芯容量 90Ah，容量差 ≤5Ah，内阻差 ≤0.5mΩ）

```bash
curl -s -X POST http://localhost:8000/preselect \
  -H 'Content-Type: application/json' \
  -d '{"series_count":4,"min_capacity":90.0,"max_capacity_diff":5.0,"max_resistance_diff":0.5,"chemistry":"LFP"}'
```

响应（候选 ID、受限容量、各芯检测版本）：

```json
{
  "feasible": true,
  "chemistry": "LFP",
  "series_count": 4,
  "limited_capacity": 97.0,
  "cell_ids": ["C001", "C002", "C003", "C004"],
  "cells": [
    {"cell_id": "C001", "capacity": 100.0, "resistance": 2.1, "test_version": "T1"},
    {"cell_id": "C002", "capacity": 99.0,  "resistance": 2.2, "test_version": "T1"},
    {"cell_id": "C003", "capacity": 98.0,  "resistance": 2.3, "test_version": "T1"},
    {"cell_id": "C004", "capacity": 97.0,  "resistance": 2.4, "test_version": "T1"}
  ]
}
```

无可行组合时：

```json
{"feasible": false, "reason": "同一化学体系下容量达标的未占用电芯不足 4 节（体系 LFP 最多 2 节）"}
```

### 3. 确认候选（带上预选返回的检测版本）

```bash
curl -s -X POST http://localhost:8000/groups/confirm \
  -H 'Content-Type: application/json' \
  -d '{"cells":[
        {"cell_id":"C001","test_version":"T1"},
        {"cell_id":"C002","test_version":"T1"},
        {"cell_id":"C003","test_version":"T1"},
        {"cell_id":"C004","test_version":"T1"}]}'
```

成功返回 `201` 及组编号、受限容量与快照内容。

### 4. 按组编号查询检测快照

```bash
curl -s http://localhost:8000/groups/1
```

响应包含不可改写的检测快照与当前阻断状态（未受影响时 `blocked` 为 `false`、
`blocked_cells` 为空，表示该组保持可放行）：

```json
{
  "group_id": 1,
  "chemistry": "LFP",
  "series_count": 4,
  "limited_capacity": 97.0,
  "created_at": "2026-10-06T01:23:45.678901+00:00",
  "blocked": false,
  "blocked_cells": [],
  "cells": [
    {"cell_id": "C001", "capacity": 100.0, "resistance": 2.1, "test_version": "T1"}
  ]
}
```

### 5. 安全隔离（不可撤销）

```bash
# 提交电芯 ID 和隔离原因 -> 201 记录一次不可撤销的隔离
curl -s -X POST http://localhost:8000/cells/C005/isolate \
  -H 'Content-Type: application/json' \
  -d '{"reason":"抽检发现漏液"}'
# {"cell_id":"C005","reason":"抽检发现漏液","isolated_at":"2026-10-06T02:00:00.000000+00:00"}

# 电芯不存在 -> 404
curl -s -X POST http://localhost:8000/cells/NOPE/isolate \
  -H 'Content-Type: application/json' -d '{"reason":"x"}'
# {"detail":"电芯 NOPE 不存在"}

# 重复隔离 -> 200 返回首次隔离的原记录（原因与时间戳不变，不新增记录）
curl -s -X POST http://localhost:8000/cells/C005/isolate \
  -H 'Content-Type: application/json' -d '{"reason":"另一个原因"}'

# 隔离记录列表（受影响范围追溯）
curl -s http://localhost:8000/isolations
```

隔离的影响：

- **预选**：`POST /preselect` 自动排除已隔离电芯；
- **确认**：`POST /groups/confirm` 在确认事务中重查隔离状态，候选中任何一节
  已被隔离即整组拒绝（`409`），不产生组也不留部分占用；
- **已入组电芯被隔离**：`GET /groups` 与 `GET /groups/{group_id}` 中该组显示
  已阻断及涉及的电芯 ID 与原因，原检测快照与组占用保留：

```json
{
  "group_id": 1,
  "blocked": true,
  "blocked_cells": [
    {"cell_id": "C002", "reason": "抽检发现漏液", "isolated_at": "2026-10-06T02:00:00.000000+00:00"}
  ],
  "cells": ["...原检测快照保持不变..."]
}
```

- **复测不能解除隔离**：`PUT /cells/{id}/test` 只更新检测档案，隔离记录保持不变；
  隔离没有解除接口，数据库触发器禁止对隔离记录的 UPDATE/DELETE。

### 6. 候选过期场景（确认前复测 → 整组拒绝）

```bash
# 预选后、确认前对 C002 复测，检测版本变为 T2
curl -s -X PUT http://localhost:8000/cells/C002/test \
  -H 'Content-Type: application/json' \
  -d '{"capacity":99.0,"resistance":2.2,"test_version":"T2"}'

# 再用旧版本 T1 确认 → 409 整组拒绝，无任何部分占用
curl -s -X POST http://localhost:8000/groups/confirm \
  -H 'Content-Type: application/json' \
  -d '{"cells":[
        {"cell_id":"C001","test_version":"T1"},
        {"cell_id":"C002","test_version":"T1"},
        {"cell_id":"C003","test_version":"T1"},
        {"cell_id":"C004","test_version":"T1"}]}'
# {"detail":"检测版本已变化（确认前发生复测），候选过期：[{'C002': {'expected': 'T1', 'actual': 'T2'}}]"}
```

已被别组抢先占用的电芯再次确认时同样返回 `409` 并整组拒绝。确认后对电芯的复测
修订不会改变 `GET /groups/{group_id}` 返回的快照内容。

### 7. 并发查询（确认事务与读取互不干扰）

确认事务提交前，其他请求看不到它的任何中间状态；提交成功的瞬间，整组
（组记录、检测快照、电芯占用）对所有请求一次性可见：

```bash
# 后台发起确认，同时并发轮询组列表、按编号查询与电芯占用
curl -s -X POST http://localhost:8000/groups/confirm \
  -H 'Content-Type: application/json' \
  -d '{"cells":[
        {"cell_id":"C001","test_version":"T1"},
        {"cell_id":"C002","test_version":"T1"},
        {"cell_id":"C003","test_version":"T1"},
        {"cell_id":"C004","test_version":"T1"}]}' &

for i in 1 2 3 4 5; do
  curl -s http://localhost:8000/groups;    echo  # 提交前不含新组，提交后整组出现
  curl -s http://localhost:8000/groups/1;  echo  # 提交前 404，提交后 200 且快照完整
  curl -s http://localhost:8000/cells;     echo  # 提交前 C001~C004 仍未占用，提交后整组占用
done
wait
```

观测要点：

- `/groups` 与 `/groups/{id}` 只会看到**已提交的完整快照**：确认未提交时查不到
  该组；若确认失败回滚，该组始终不可见。
- `/cells` 与 `/preselect` 在确认提交前看不到临时占用；提交成功后整组占用
  一次性可见。
- 隔离与确认并发时，以**提交先后**决定结果：隔离先提交则确认整组拒绝（无组、
  无占用）；确认先提交则新组产生并随即显示为已阻断。任何时刻查询都不会看到
  半成品状态。
- 读取与确认并发执行互不影响：读取永远拿到一致的已提交状态，确认事务照常
  原子提交或整组回滚。

### 8. 同一快照口径：确认与隔离交错时不拼出不存在的状态

考虑这个交错：一次 `GET /cells`（或 `GET /cells/C001`）先读到 C001 的占用信息，
此时另一请求**确认**含 C001 的组并提交、随后又**隔离** C001 并提交，读请求最后
才去读隔离信息。由于占用、组编号与隔离标志在**同一个只读事务（同一已提交快照）**
内读取，返回只可能是确认前或确认/隔离后的**完整状态**，不会出现
`occupied=false` 却 `isolated=true` 的矛盾记录：

```bash
# 终端 A：持续轮询电芯列表 / 单芯档案
( while true; do
    curl -s http://localhost:8000/cells        | python3 -m json.tool
    curl -s http://localhost:8000/cells/C001   | python3 -m json.tool
  done ) &
POLL=$!

# 终端 B：先确认整组，提交后再隔离 C001（两次独立提交）
curl -s -X POST http://localhost:8000/groups/confirm \
  -H 'Content-Type: application/json' \
  -d '{"cells":[
        {"cell_id":"C001","test_version":"T1"},
        {"cell_id":"C002","test_version":"T1"},
        {"cell_id":"C003","test_version":"T1"},
        {"cell_id":"C004","test_version":"T1"}]}'
curl -s -X POST http://localhost:8000/cells/C001/isolate \
  -H 'Content-Type: application/json' -d '{"reason":"抽检发现漏液"}'

kill $POLL
```

任何一次返回里，C001 只会是下面两种**自洽**状态之一：

```jsonc
// 确认前快照
{"cell_id":"C001","occupied":false,"group_id":null,"isolated":false}
// 确认 + 隔离提交后的快照
{"cell_id":"C001","occupied":true,"group_id":1,"isolated":true}
```

绝不会出现 `{"occupied":false,"group_id":null,"isolated":true}` 这种跨快照拼出的
不存在状态。录入（`POST /cells`）与复测（`PUT /cells/{id}/test`）返回电芯档案时
遵守同一口径；`/preselect` 也在同一快照内按已提交的占用与隔离状态筛选。需要严格
复现该交错时，测试用 SQLite 的 `set_progress_handler` 在"读到占用之后、读隔离
之前"可控挂起读线程，再依次提交确认与隔离（见 `tests/test_api.py` 中
`*_reads_one_committed_snapshot`）。

### 9. 登记交付批次（2~4 个已确认组）

```bash
# 已有组 1、2（均为 LFP、当前未阻断、未归属其他批次）
curl -s -X POST http://localhost:8000/batches \
  -H 'Content-Type: application/json' \
  -d '{"group_ids":[1,2]}'
```

成功返回 `201`：批次号、固定成员关系、各组确认时的检测快照与整批可交付状态：

```json
{
  "batch_id": 1,
  "chemistry": "LFP",
  "created_at": "2026-10-06T03:00:00.000000+00:00",
  "group_ids": [1, 2],
  "deliverable": true,
  "blocked_groups": [],
  "groups": [
    {"group_id": 1, "blocked": false, "blocked_cells": [],
     "cells": [{"cell_id": "C001", "capacity": 100.0, "resistance": 2.1, "test_version": "T1"}]},
    {"group_id": 2, "blocked": false, "blocked_cells": [], "cells": ["...确认时快照..."]}
  ]
}
```

整批拒绝的情形（`409`，不产生批次也不留部分归属）：组号不存在、组当前被隔离
阻断、组已归属其他批次、成员组化学体系不一致；组号重复或数量不在 2~4 返回 `422`。

### 10. 查询批次与隔离阻断

```bash
curl -s http://localhost:8000/batches/1
```

建批后若成员组内电芯被隔离，整批转为不可交付，并给出受影响的组号、电芯 ID 与
原因；成员关系与各组原检测快照保持不变：

```bash
curl -s -X POST http://localhost:8000/cells/C005/isolate \
  -H 'Content-Type: application/json' -d '{"reason":"巡检发现鼓包"}'
curl -s http://localhost:8000/batches/1
```

```json
{
  "batch_id": 1,
  "group_ids": [1, 2],
  "deliverable": false,
  "blocked_groups": [
    {"group_id": 2, "cell_id": "C005", "reason": "巡检发现鼓包",
     "isolated_at": "2026-10-06T03:30:00.000000+00:00"}
  ],
  "groups": ["...成员关系与各组确认时快照保持不变..."]
}
```

建批与隔离并发时同样以**提交先后**决定结果：隔离先提交则建批整批拒绝（`409`，
无批次、无部分归属）；建批先提交则批次产生并随即显示 `deliverable: false`。
任何一次批次查询（`GET /batches` / `GET /batches/{batch_id}`）都在同一已提交
快照内完成，`deliverable`、`blocked_groups` 与各组的 `blocked` 标记必然自洽。

### 11. 确认发运（按批次号，一次）

```bash
# 批次 1 存在、尚未发运且全部成员当前未被隔离 -> 201 发运成功
curl -s -X POST http://localhost:8000/batches/1/ship
```

成功返回 `201`：批次号、不可改写的发运时间、成员组号和各组原检测快照：

```json
{
  "batch_id": 1,
  "shipped_at": "2026-10-06T04:00:00.000000+00:00",
  "group_ids": [1, 2],
  "groups": [
    {"group_id": 1, "blocked": false, "blocked_cells": [],
     "cells": [{"cell_id": "C001", "capacity": 100.0, "resistance": 2.1, "test_version": "T1"}]},
    {"group_id": 2, "blocked": false, "blocked_cells": [], "cells": ["...确认时快照..."]}
  ]
}
```

拒绝发运的情形（不留发运记录）：

```bash
# 批次不存在 -> 404
curl -s -X POST http://localhost:8000/batches/999/ship
# {"detail":"批次 999 不存在"}

# 重复发运 -> 409 明确拒绝，首次发运记录不改写
curl -s -X POST http://localhost:8000/batches/1/ship
# {"detail":"批次 1 已发运（发运时间 2026-10-06T04:00:00.000000+00:00），不可重复发运"}

# 有成员组当前被隔离阻断 -> 409（建批后、发运前隔离了成员电芯时）
curl -s -X POST http://localhost:8000/cells/C005/isolate \
  -H 'Content-Type: application/json' -d '{"reason":"巡检发现鼓包"}'
curl -s -X POST http://localhost:8000/batches/1/ship
# {"detail":"批次成员当前被安全隔离阻断，拒绝发运：[{2: 'C005'}]"}
```

发运后批次查询同时呈现历史发运事实与当前隔离状态（未发运批次为
`"shipped": false, "shipped_at": null, "trace_required": false`，按原规则查询）：

```bash
curl -s http://localhost:8000/batches/1
```

```json
{
  "batch_id": 1,
  "group_ids": [1, 2],
  "deliverable": true,
  "blocked_groups": [],
  "shipped": true,
  "shipped_at": "2026-10-06T04:00:00.000000+00:00",
  "trace_required": false,
  "groups": ["...成员关系与各组确认时快照保持不变..."]
}
```

### 12. 发运后隔离：已发运批次转为需追溯

发运先提交、隔离后到时，发运记录保留；此后的隔离让已发运批次显示需追溯及
受影响的组号、电芯 ID 和原因：

```bash
# 批次 1 已发运，此时隔离其成员电芯 C001
curl -s -X POST http://localhost:8000/cells/C001/isolate \
  -H 'Content-Type: application/json' -d '{"reason":"发运后抽检发现漏液"}'
curl -s http://localhost:8000/batches/1
```

```json
{
  "batch_id": 1,
  "group_ids": [1, 2],
  "deliverable": false,
  "blocked_groups": [
    {"group_id": 1, "cell_id": "C001", "reason": "发运后抽检发现漏液",
     "isolated_at": "2026-10-06T05:00:00.000000+00:00"}
  ],
  "shipped": true,
  "shipped_at": "2026-10-06T04:00:00.000000+00:00",
  "trace_required": true,
  "groups": ["...成员关系与各组确认时快照保持不变..."]
}
```

发运与隔离并发时以**提交先后**决定结果：隔离先提交则发运被拒绝（`409`，不留
发运记录）；发运先提交则记录保留，批次随后显示 `trace_required: true`。任何一次
批次查询都在同一已提交快照内完成，`shipped` / `shipped_at` / `trace_required` /
`deliverable` / `blocked_groups` 必然自洽，不会拼出跨快照状态。

## 项目结构

```
app/
  main.py       # FastAPI 路由：录入 / 复测 / 隔离 / 预选 / 确认 / 快照与阻断查询 / 批次登记、发运与查询
  db.py         # 每线程 SQLite 连接（WAL）、schema、不可改写触发器、写锁
  selection.py  # 成组预选算法（穷举组合，最多 C(12,6)=924 种）
  schemas.py    # Pydantic 请求模型（串联节数 3~6、隔离原因、批次 2~4 组等校验）
tests/test_api.py
Dockerfile / docker-compose.yml
```
