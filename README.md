# 非遗课程交流预约协调

本项目用于建设面向业务人员的纯服务端系统。代码按领域模型、应用服务、持久化与接口边界组织；时间、标识和外部输入应通过可替换端口接入，以便稳定复现状态变化。运行数据与本地配置不得写入源码目录。

## 架构

```
service_09252_008/
├── domain/            # 领域模型层
│   ├── models.py      #   课程包、导师、工坊资源、材料批次、接待窗口、预约、发运单、损耗、结算、事件
│   ├── disputes.py    #   预约争议案件：案件头、双方陈述、证据、处理决定、主体与角色
│   ├── rules.py       #   纯规则：前置培训、容量、安全等级、互斥资源、材料分配、运输周期
│   └── errors.py      #   领域错误（接口边界据此映射 HTTP 状态码）
├── application/       # 应用服务层
│   ├── ports.py       #   可替换端口：Clock / IdGenerator（测试注入手动时钟与序列 ID）
│   ├── catalog_service.py  # 目录登记与校验
│   ├── booking_service.py  # 预约状态机：申请/报价/锁定/改期/发运/到货/签到/结算/取消/恢复
│   └── dispute_service.py  # 争议案件：登记/双方陈述/证据/处理决定，查询按权限裁剪
├── persistence/       # 持久化层
│   ├── store.py       #   预约存储端口 + 内存实现（快照回滚）
│   ├── sqlite_store.py     # 预约 SQLite 实现（BEGIN IMMEDIATE，重启可恢复）
│   ├── case_store.py       # 争议案件存储端口 + 内存实现（案件/陈述/证据/决定拆表）
│   └── sqlite_case_store.py# 争议案件 SQLite 实现（证据与决定独立建表）
└── interfaces/
    └── http_api.py    # 接口边界：HTTP/JSON API（仅标准库）
```

## 领域规则要点

- **预约方案**：申请时校验前置培训（导师资格有效期须覆盖课程结束）、场地容量、
  材料安全等级（批次等级 ≤ 场地与窗口允许上限）、互斥资源（同资源或同互斥组时段不可重叠）、
  跨境运输周期（`now + lead_time ≤ slot_start`），并生成材料分配计划。
- **状态机**：`REQUESTED → QUOTED → LOCKED → SHIPPED → CHECKED_IN → SETTLED`，
  另有 `WAITLISTED / CANCELLED / EXPIRED`。窗口满或互斥被占时进入候补。
- **锁定**：在单事务内复查互斥并扣减库存，带 TTL；幂等键防止重复占位。
- **发运后不可移动**：`SHIPPED` 及之后的状态拒绝改期；取消时已发运材料记损耗
  （`cancel_after_shipment`），未发运预占回补库存，并按申请先后释放候补。
- **到货**：支持部分到货与在途损耗；发运单未关闭或到货不足时禁止签到。
- **结算**：按实际出勤折算消耗；国内余料退回库存，跨境余料记损耗
  （`non_returnable_leftover`），课中损坏记 `damaged_in_use`。
- **超时恢复**：过期锁定释放库存并晋级候补，过期报价退回待报价；
  服务启动时与 `POST /admin/recover` 均可触发。
- **时间**：内部一律 UTC；输入接受任意 ISO-8601 偏移（拒绝朴素时间）。

### 预约争议案件

客服可把升级投诉登记为“预约争议案件”（独立 `disputes.db`）：

- **关联原预约**：立案必须指向已存在的预约，默认以申请院校与导师为双方，可显式覆盖。
- **双方陈述与证据**：申请方/被申请方各自提交；当事方只能提交本方材料，
  内部角色（客服/处理人/管理员）可代登记。证据带内容指纹（SHA-256）用于留存。
- **处理决定**：仅处理人/管理员可作出，一案一决定，落定即关闭案件。
- **关闭后管控**：关闭后案件仍允许查看，但**不允许追加未经授权的证据**；
  仅处理人凭显式 `authorized` 标记可补交，补交证据带 `authorized=true`。
- **按权限裁剪（Python 层）**：内部角色可见全部陈述与证据；当事方仅可见
  本方陈述、本方证据与处理决定；无关主体拒绝查看（HTTP 403）。
- 操作主体经请求头 `X-Actor-Id` 与逗号分隔的 `X-Actor-Roles` 传入。

## 运行

```bash
python3 -m service_09252_008 --host 127.0.0.1 --port 8080
# 运行数据目录：--data-dir 或环境变量 SERVICE_09252_008_DATA_DIR
# （默认 ~/.local/state/service_09252_008，绝不写入源码目录）
```

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/packages` `/mentors` `/resources` `/material-batches` `/reception-windows` | 目录登记 |
| POST | `/bookings` | 申请（需幂等键） |
| POST | `/bookings/{id}/quote` | 报价 |
| POST | `/bookings/{id}/lock` | 锁定（需幂等键，可带 `ttl_seconds`） |
| POST | `/bookings/{id}/reschedule` | 改期（发运后拒绝） |
| POST | `/bookings/{id}/ship` | 发运（需幂等键） |
| POST | `/shipments/{id}/arrivals` | 到货（支持部分到货） |
| POST | `/shipments/{id}/losses` | 在途损耗登记 |
| POST | `/bookings/{id}/checkin` | 签到 |
| POST | `/bookings/{id}/settle` | 结算（`actual_attendance`、可选 `damaged`） |
| POST | `/bookings/{id}/cancel` | 取消（释放候补、按规则记损耗） |
| POST | `/admin/recover` | 恢复超时任务 |
| POST | `/disputes` | 客服登记预约争议案件（需支持角色） |
| GET  | `/disputes` `/disputes/{id}` | 案件列表/详情（按主体权限裁剪，列表可 `?booking_id=` 过滤） |
| POST | `/disputes/{id}/statements` | 双方陈述 |
| POST | `/disputes/{id}/evidence` | 证据（关闭后须处理人 `authorized`） |
| POST | `/disputes/{id}/decision` | 处理决定（落定即关闭） |
| GET  | `/bookings/{id}` `/health` | 查询 |

幂等键经请求头 `Idempotency-Key` 或载荷字段 `idempotency_key` 传入；
同键重放返回首次结果（`idempotent_replay: true`），同键不同载荷返回 409。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：主流程端到端、前置培训/容量/安全/互斥/运输周期规则、跨时区、
幂等重放、并发锁定（内存与 SQLite 双后端）、重启后超时恢复、
部分到货与在途损耗、取消释放候补与损耗记录、HTTP 接口边界、
预约争议案件（登记/双方陈述/权限裁剪/决定关闭/关闭后补证管控，内存与 SQLite 双后端）。

## 编译检查

```bash
python3 -m compileall -q service_09252_008 tests
```

扩展模块覆盖证据、审批、权限、留存、对账与恢复等业务边界。
