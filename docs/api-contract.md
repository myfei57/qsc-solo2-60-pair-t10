# 控制台接口契约

本契约是所有控制台接口的唯一入口规则。处理顺序固定，不允许在 handler 内自行解析 JSON、自行决定 HTTP 状态码或拼接错误文案。

## 1. 请求处理时序

```text
1. 解析传输层：method/path/header/JSON
2. 路由匹配
3. 按 EndpointContract 完整校验入参（收集全部违规，不进入业务）
4. 检查关键操作的 Idempotency-Key
5. 建立请求上下文（X-Request-Id、Idempotency-Key）
6. 开启 UnitOfWork 快照
7. 执行业务
8. 成功：提交并缓存幂等响应；失败：回滚、写拒绝/失败审计、返回统一错误信封
```

校验阶段禁止调用任何业务组件、Store、外部设备或审计记录之外的副作用。只有进入业务阶段后才允许修改状态。

## 2. 统一响应

成功响应保持各接口原有业务 JSON；所有响应带：

* `X-Request-Id`：未由客户端提供时服务端生成。
* 幂等重试命中时额外带 `Idempotent-Replay: true`。

错误响应统一为：

```json
{
  "error": {
    "code": "COMMON-0008",
    "message": "request validation failed",
    "request_id": "7f4...",
    "retryable": false,
    "stage": "validate",
    "details": [
      {
        "field": "flow",
        "code": "COMMON-0008",
        "message": "flow must be >= 0",
        "rejected_value": -1
      }
    ]
  }
}
```

`stage` 固定为：`transport`、`validate`、`idempotency`、`business`。

## 3. 错误码分段（已定死）

错误码是**全局唯一字符串码**，同时数字段按模块固定预留。任何码只能新增，不能改号、不能复用。

| 段 | 含义 | HTTP 语义 |
|---|---|---|
| `COMMON-00xx` | JSON、字段缺失、类型、越界、未知字段、路由 | 400/404/405 |
| `COMMON-03xx` | 幂等键缺失、冲突、执行中 | 400/409 |
| `*-1xxx` | 模块入参校验保留段 | 400 |
| `*-2xxx` | 模块业务前置条件，例如资源不存在、库存不足、状态冲突 | 404/409 |
| `COMMON-4xxx` | 业务执行中失败、部分失败、恢复失败 | 500 |
| `COMMON-5xxx` | 未预期服务端错误 | 500 |

当前通用校验码：

| code | 含义 |
|---|---|
| `COMMON-0001` | JSON 非法 |
| `COMMON-0002` | body 不是 JSON object |
| `COMMON-0004` | 未知字段 |
| `COMMON-0005` | 必填字段缺失 |
| `COMMON-0006` | 字段为 null |
| `COMMON-0007` | 类型错误 |
| `COMMON-0008` | 值域/长度越界 |
| `COMMON-0009` | 值不允许（校验聚合错误及枚举/空串等） |
| `COMMON-0201` | 路由不存在，404 |
| `COMMON-0202` | 方法不允许，405 |

模块业务码示例：

| code | HTTP | 含义 |
|---|---:|---|
| `FILTER-2001` | 404 | 滤池不存在 |
| `FILTER-2002` | 404 | zone 当前没有对应滤池 |
| `FILTER-2003` | 409 | zone 已被其他滤池占用 |
| `INVENTORY-2001` | 409 | 库存不足 |
| `COMMON-4001` | 500 | 部分失败且已恢复，可重试 |
| `COMMON-4002` | 500 | 部分失败且恢复未完成，不自动重试 |
| `COMMON-5001` | 500 | 未预期错误 |

同一种条件只能映射一个 code/status；handler 不再自行捕获后改报 500。

## 4. 三类参数错误的区分

* 缺字段：`COMMON-0005`，`details[].field` 指出字段。
* 类型错误：`COMMON-0007`，严格按 JSON 类型判断。数字字符串不会隐式转数字；`true/false` 不等于 0/1。
* 越界：`COMMON-0008`，包括 `< minimum`、`> maximum`、字符串/数组长度越界。
* null：`COMMON-0006`。
* 未知字段：`COMMON-0004`。

一次请求会返回所有可独立发现的错误；顶层 code 按“越界 > 类型 > null > 缺失 > 未知字段”的优先级选择，具体每个字段仍在 `details` 中给出自己的码。

## 5. 缺字段策略：按接口重要程度定死

每个接口在 `contracts.py` 中声明三档之一：

1. `CRITICAL`：关键字段缺失或为 null，整个请求拒绝，不使用默认值。用于加药、库存消耗/接收、校准、水位、全流程 `/cycle`、恢复/重放等。
2. `STANDARD`：契约中标为 `required` 的字段缺失则拒绝；只有字段显式声明 default 时才按默认值继续。
3. `TOLERANT`：可选字段缺失或 null 时使用文档化安全默认值。例如 `/simulate` 的 `ticks=5`。

GET 接口不接受 JSON object body；POST 契约只允许声明过的字段。是否拒绝整个请求由上述 importance 决定，不由 handler 临时判断。`GET /catalog` 会输出每个接口的 importance、字段类型、默认值、上下界和是否强制幂等键。

## 6. 幂等

关键状态变更（契约中的 `idempotency_required=true`）强制要求 `Idempotency-Key`，包括：加药/投药、流量校准、水位、滤池新增/改号、反洗开始/入队/恢复/重放、配额累加、`/cycle`、库存接收/消耗、重置类操作等。完整机器可读清单以 `/catalog` 和 `contracts.py` 为准。

规则：

* 同一个 key + method + path + 参数指纹：首次执行业务；后续重试返回首次的状态码和响应体，不重复执行业务。
* 同 key 改参数或改操作：`COMMON-0302`，409。
* 首次操作仍在进行：`COMMON-0303`，409，`retryable=true`。
* 业务异常且 UnitOfWork 已回滚：删除 in-progress 标记，客户端可用同 key 重试。
* 已完成的响应持久化在 `console:idempotency:<key>`。

## 7. 部分失败与恢复

业务执行统一包在 `UnitOfWork` 中：

* 进入业务前保存 Store 与内存 Bank 快照。
* 业务中途发生非预期异常时恢复 Store 和 Bank，保证此前成功的步骤不留下半状态。
* 回滚成功返回 `COMMON-4001`，`retryable=true`，响应和请求审计使用同一个 `request_id`。
* 回滚也失败返回 `COMMON-4002`，必须人工介入，不允许自动重试。
* 业务前置条件（资源不存在、库存不足）仍在任何必要状态变化前抛出，分别返回 404/409，不伪装成 500。

化学加药审计属于业务状态：当业务事务回滚时一并恢复，因此审计只记录真正提交成功的加药动作。

## 8. 审计与运行记录对账

有两条不同职责的记录：

* 化学加药审计：`/audit`，记录真正成功提交的 coagulant/chlorine 加药，条目包含 `request_id` 与 `idempotency_key`。
* 控制台请求审计：`/requests`，记录每次接受和拒绝，字段包括：
  `request_id`、`idempotency_key`、`method`、`path`、`status`、`code`、`stage`、`message`、`details`、`time`。

查询示例：

```text
GET /requests?request_id=<X-Request-Id>
GET /requests?code=COMMON-0007
```

值班排障时按响应头/响应体中的 `X-Request-Id` 可在 `/requests` 找到拒绝原因；若是成功业务动作，可继续用同一个 ID 在 `/audit` 对上化学加药记录。`/metrics` 中 `by_error_code` 统计同一错误码，保证告警、请求审计、错误响应三者口径一致。
