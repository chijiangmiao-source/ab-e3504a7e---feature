# 深空地面站回绕时标审计服务

汇集多个设备的回绕时标（模 `M` 计数器读数），判断带因果窗口的记录能否落在
同一条真实时间线上。**相同计数值不视为同一时刻**——每个事件的绝对时刻为
`absolute = counter + M × wrap`，回绕次数 `wrap` 为非负整数，由约束精确推出。

## 语义模型

- **锚点**：绝对 tick 已知的节点（`anchor.absolute`），其回绕次数固定。
- **事件**：至多 12 个，各给出一个模 `M` 计数值 `counter ∈ [0, M)`。
- **约束**：带标识的闭区间 `source --[lo, hi]--> target`，含义为
  `lo ≤ absolute(target) − absolute(source) ≤ hi`。
- 全部事件必须经约束（无向）连通锚点，否则请求被拒绝（400）。

约束两边减去计数残差后得到回绕次数上的整数差分约束：

```
k_t − k_s ∈ [ ⌈(lo − (c_t − c_s)) / M⌉ ,  ⌊(hi − (c_t − c_s)) / M⌋ ]
```

用最短路/负环机制求解，判定三态：

| 状态 | 含义 | 结论内容 |
|---|---|---|
| `unique` | 唯一时间线 | 每个事件的 `wrap` 与 `absolute` |
| `ambiguous` | 多解 | 按事件标识顺序的**前两条规范时间线**（字典序最小两条）及**首个不稳定先后关系**（标识序上第一对先后顺序不被所有解共享的节点，含两条时间线中各自的关系与可达区间） |
| `unsatisfiable` | 无解 | **可复算的冲突约束链**：负环上的约束标识、每步整数界、权重总和 `< 0` |

参考例：`M=100, A=95, B=3, A→B=[8,8]` 唯一展开为 `B = 3 + 100×1 = 103`。

## 计数修复（只允许改读数）

审计员读到 `unsatisfiable` 记录后，可凭**修复标识**提交统一读数校正上限
`K`（整数，`0 ≤ K ≤ ⌊M/2⌋`）。系统冻结来源输入与 `K`，**绝不放宽因果窗口**，
只允许把每个事件的读数按整数 tick `d ∈ [-K, K]` 修正、再按模数归一
（`corrected = (counter + d) mod M`），随后原始回绕整数差分约束必须原样成立。

校正向量与回绕差分约束**联合搜索**（不枚举全部向量、不贪心逐条改读数、
不用浮点）：

1. MRV 可行性搜索先给出一个仅作上界的现任解；
2. 再按事件标识序、tick 从 `-K` 到 `K` 做完备深度优先枚举；
3. 每个节点为未决事件维护 tick 域：固定某 tick、其余事件按当前域松弛后，
   若出现空 wrap 区间或负冲突环，就把该 tick 从域中剔除，并迭代到不动点；
4. 存活域驱动目标下界（域中已无 0 的事件必改；最小存活 `|tick|` 之和为
   绝对 tick 下界），无法击败现任解的子树直接剪除；
5. 每个被剔除的 tick 都附带可复算的证据（空区间或负环及权重）。

裁决严格按序：**先最少化被改动事件数，再最少化校正绝对值之和，
最后按事件标识序取字典序最小的校正向量**。成功时返回规范校正、修正后的
时间线（含每个事件的归一读数、wrap、绝对时刻）以及逐约束差值
（残差差、校正 tick 差、wrap 差、绝对差与原始窗口的复核）；预算内无解时
返回 `beyond_budget` 与完整的剪枝分支证据树。

修复幂等语义：

- 相同 `fix_id` + 相同来源审计 + 相同 `K` 重传 → `200`，返回**原修复编号**，
  `replayed: true`；
- 相同 `fix_id` 改投其他来源审计或改换 `K` → `409`，不新增记录，
  **来源审计永不被改写**；
- 只有 `unsatisfiable` 的来源接受修复（其余状态 `409`）；
- 非法请求（`K` 越界、字段非法等）→ `400`，不占用 `fix_id`。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 健康检查 |
| `POST` | `/audits` | 创建审计（对 `request_id` 幂等） |
| `GET` | `/audits` | 列出审计编号与数量 |
| `GET` | `/audits/{id}` | 读取冻结的输入、结论与证据 |
| `POST` | `/audits/{id}/repairs` | 对无解审计提交计数修复（对 `fix_id` 幂等） |
| `GET` | `/audits/{id}/repairs` | 列出某条审计下的修复编号 |
| `GET` | `/repairs/{rid}` | 读取冻结的修复记录与搜索证据 |

### 创建示例

```bash
curl -X POST localhost:${HOST_PORT:-8080}/audits -d '{
  "request_id": "req-1",
  "modulus": 100,
  "anchor": {"id": "A", "absolute": 95},
  "events": [{"id": "B", "counter": 3}],
  "constraints": [{"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8}]
}'
```

返回 `201` 与完整记录（`audit_id`、`status`、`conclusion`、`evidence`）。

### 修复示例

```bash
# 1) 读取一条 unsatisfiable 审计（audit_id 为 AUD-000003）
# 2) 提交修复标识与统一读数校正上限 K
curl -X POST localhost:${HOST_PORT:-8080}/audits/AUD-000003/repairs -d '{
  "fix_id": "fix-7",
  "K": 1
}'
```

成功返回 `201`（重传返回 `200`，`replayed: true`），含
`canonical_corrections`、`repaired_timeline`、`constraint_deltas`、
`objective` 与 `search`（剪枝统计与可复算分支证据）；预算内无解时
`status` 为 `beyond_budget`。

### 幂等语义

- 相同 `request_id` + 相同载荷（事件/约束顺序无关）→ `200`，返回**原审计编号**，
  `replayed: true`；
- 相同 `request_id` + 改换任一事件或约束 → `409`，**不新增记录**；
- 载荷非法 → `400`（`problems` 列出全部问题），不占用 `request_id`。

## 运行

### 本地

```bash
PORT=8080 python3 -m app.server
```

### Docker Compose

```bash
HOST_PORT=9090 docker compose up --build app   # 健康检查走可配置宿主机端口
curl localhost:9090/health
```

## 一次性验收服务 verify

`verify` 是执行后即退出的一次性服务，依次执行：

1. **构建检查**：全部源码编译；
2. **代码测试**：求解器/修复搜索/存储/API 单元测试；
3. **API/HTTP 冒烟**：参考展开（B=103）、歧义双时间线与首个不稳定先后关系、
   双向矛盾链（权重可复算且 `< 0`）、幂等记录（重放/冲突/不新增），以及
   计数修复：跨回绕修正、全局最优并列裁决、改动数优先于绝对 tick 和、
   预算内无解的分支证据、修复幂等与守卫、旧审计语义回归。

```bash
docker compose up --build --exit-code-from verify --abort-on-container-exit verify
echo $?   # 0 = 验收通过，非 0 = 失败
```

本地等价运行：

```bash
PORT=18080 python3 -m app.server &
APP_URL=http://127.0.0.1:18080 python3 -m verify.verify
```

## 结构

```
app/solver.py        回绕展开与三态判定（纯函数，无第三方依赖）
app/repair.py        计数修复联合搜索（域过滤 + 冲突链下界 + 分支限界）
app/store.py         幂等审计/修复存储（request_id、fix_id 指纹、冻结记录）
app/server.py        HTTP API（stdlib，PORT 环境变量配置端口）
app/healthcheck.py   容器健康检查
tests/               单元测试
verify/verify.py     一次性验收（构建检查 + 代码测试 + HTTP 冒烟）
Dockerfile           单一镜像，app 与 verify 共用
compose.yaml         app（健康检查、可配置宿主机端口）+ verify（一次性）
```
