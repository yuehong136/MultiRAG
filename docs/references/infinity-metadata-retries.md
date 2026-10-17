# Infinity 元数据竞争重试

Infinity 文档存储与 memory 存储共用 `InfinityConnectionBase`。元数据 DDL 在
`ConflictType.Ignore` 下才接入有限重试：数据库创建、普通/文档元数据表创建、
向量/全文/二级索引创建、表删除，以及启动迁移中的数据库与索引创建。
当前生产路径没有单独删除数据库、删除索引的调用，不新增这些接口。

## 错误与失败合同

- SDK `infinity-sdk==0.7.0-dev5` 的 `InfinityException` 使用
  `error_code` / `error_msg`；同时兼容异常双参数、嵌套 `(code, message)` 元组。
- `9003` 是通用 `kRocksDBError`，还须有 `Resource busy` 才重试。
  无结构化码的旧异常必须同时含 RocksDB 和 Resource busy；已知其他错误码
  不降级成文本匹配。损坏、IO、超时、参数错误和 `4005` 事务冲突立即失败。
  参见 [Infinity 对应版本错误码](https://github.com/infiniflow/infinity/blob/v0.7.0-dev5/src/common/status.cppm)。
- SDK 的 `drop_table(..., Ignore)` 可能返回非零错误响应而不抛异常，重试层
  先将其转换成 `InfinityException`，避免误报删除成功。
- 重试耗尽仍抛原异常。`create_doc_meta_idx` 保持本地布尔接口，失败返回
  `False`，其中任一二级索引失败也不再报告 `True`。
- INSERT、UPDATE、行 DELETE、读取和 `add_columns` 不接入重试；启动迁移
  不作为整体重放。每个借出的连接由所有者通过 `finally` 恰好归还一次；
  启动探活与迁移各自借还，刷新池时先归还旧池连接再销毁旧池。

## 参数

参数在模块加载时读取；变更环境变量后需要重启进程。

| 环境变量 | 默认 | 有效范围 |
|---|---:|---:|
| `INFINITY_META_RETRY_MAX` | 5 次尝试（含首次） | 整数 1..10 |
| `INFINITY_META_RETRY_BASE_DELAY_MS` | 50 ms | 整数 0..1000 |

空值沿用默认，非整数及越界值记录告警后回退默认，不导致导入失败。
内部函数显式传入非法值则在首次操作前抛 `ValueError`，包括布尔数、浮点数和无穷值。
1 次尝试表示禁用重试，0 ms 表示不等待但仍受次数约束。

每个 DDL 调用失败后使用指数退避与 1..1.5 倍随机抖动，单次等待最多 1.5 秒，最后一次
失败不再等待。默认累计等待最多 1.125 秒，最大有效配置累计等待最多 13.5 秒。
这只限制等待预算，不限制 SDK 网络调用本身的耗时。

## 适配与验收

对应提交 `c0fc8b32f2edadca8dc490dcbc37e2c71dbc33d1`，上游 checkout
`/Users/xldu/project/ragflow`，冻结上限
`519e7d98a5651564d4e35d6648f006cba4baaf4f`。确认指定提交在冻结范围内，
后续该重试实现未有语义修复；冻结范围内的 Python 退役不适用于本仓活跃 Python
后端。本地补齐结构化错误识别、非竞争 RocksDB 错误拒绝、返回错误码处理、
参数上下界、元数据二级索引与启动迁移覆盖，以及异常释放，保留现有命名与映射。

2026-10-05 验收：

- [专项单测](../../tests/unit/test_infinity_metadata_retry.py)：99 passed。
  覆盖每个 DDL 阶段竞争恢复/耗尽/立即失败、缺失或非法映射、借连接失败、
  中断退出、启动健康检查失败、迁移不重放和刷新池的释放顺序。
  合并既有 Infinity 单测与文档可用性测试的回归为 136 passed。
- `make integration-infinity`：10 passed；使用 Testcontainers 创建的真实
  `infiniflow/infinity:v0.7.0-dev5`，未改共享配置或业务库。
  [并发测试](../../tests/integration/test_infinity_metadata_contention.py) 两组各 12 路，
  分别操作不同表和相同表；独立 SDK 连接读回数据库、表以及目标向量、全文、
  二级索引后再删除。
  最终轮次两组分别捕获 5、11 次 RocksDB 竞争重试，各借还 48 次，未遗留连接；
  scratch 数据库与容器均清理。运行证据在本机
  `.test-results/20261005-140910-33054/`。
- 首轮同表并发 DROP 真实触发 `4005` 并立即抛出。专项测试明确检查该非目标
  错误可见且仍释放连接，不将它计为成功操作，不扩大 RocksDB 重试范围。
  最终通过轮次同表 DROP 有 2 次 `4005`，均保持失败且释放连接，独立读回
  最终表集为空；不同表的并发操作均成功。这不保证同表任意并发都成功。
- 最新 `make -k verify` exit=0：5623 unit passed，格式、Ruff、8 项 import
  契约、async DB 门禁与 mypy（138 文件）均通过。此前其他任务文件的格式/
  pytest 保留参数名阻塞已由其修改解除，本任务没有修改那些文件。
- 核心集成未通过。首次 `make integration` 记录 392 passed、1 failed 后
  主动中断；中断期间 runner 还报告 `KeyError`，该轮不视为完成全套检查。
  随后 `make integration PYTEST_ARGS='-q -x'` 复现同一范围外失败：45 passed、
  1 failed、2 deselected，pytest/runner exit=1（make exit=2）。
  `test_agent_execution_origin.py::test_origin_migration_preserves_material_and_refuses_destructive_downgrade`
  硬编码期望迁移头 `b0d2e4f6a8c0`，当前实际为 `d6f8a0b2c4e6`；本任务未改
  迁移或该断言。复现证据为 `.test-results/20261005-142631-53461/`。
- 生产部署、多副本 HTTP 压测和其他 Infinity 版本
  不在本次真实环境验收内。`make smoke` 检查现有服务，不能代表本补丁已部署。

### 2026-10-17 复核

实现已由独立提交 `feac2aedeef412e99533cdaa940ed3f0c1c47389` 入库，当前
`9582bdb80f1a1be4505f4061d1abe71aa4cac63a` 已包含该提交。本次重新核对指定
上游完整 diff、冻结范围内后续演进、安装的 dev5 SDK 异常与返回值，以及文档/
memory 存储的共用调用链；上述重试合同仍成立，无需重复移植或修改生产代码。
当前没有独立删除数据库、索引的生产调用，未新增无消费者接口。

- 专项单测与 Infinity 回归：150 passed，exit=0。命令为
  `uv run --no-sync pytest tests/unit/test_infinity_metadata_retry.py tests/unit/test_infinity_available_filter.py tests/unit/test_infinity_empty_results.py tests/unit/test_infinity_condition_escaping.py tests/unit/test_infinity_missing_table_update.py tests/unit/test_infinity_search_failures.py tests/unit/test_infinity_row_id.py tests/unit/test_document_availability_infinity.py -q`。
  覆盖 SDK/旧异常形态、参数非法值与上下界、非竞争立即失败、耗尽、提前返回、
  中断与每次借用恰好归还；完整日志为 `/tmp/infinity-retry-20261017-unit.log`。
- `make integration-infinity`：21 passed，零 skip，exit=0。运行器创建自有
  `infiniflow/infinity:v0.7.0-dev5` 容器，两组各 12 路并发在独占 scratch
  数据库中创建/删除，并使用独立 SDK 连接读回数据库、表和索引。
  不同表组捕获 4 次目标竞争重试，同表组捕获 11 次；每组借出/归还均为 48 次，
  无活动连接遗留。同表删除的 3 次 `4005` 保持异常，不计作操作成功。
  scratch 数据库清理读回通过，运行结束后确认自有容器已移除。
  证据为 `.test-results/20261017-193506-84129/{run.json,junit.xml}`；完整日志为
  `/tmp/infinity-retry-20261017-integration.log`。
- 本次仅更新验收文档，未修改代码、依赖或共享配置，未重跑全量 `make verify`
  与核心 `make integration`。上节全量门禁及其失败是历史结果，不作为本次结果。
  真实验收范围是单个 dev5 实例的 SDK 并发；生产部署、多副本 HTTP 压测及
  其他 Infinity 版本仍未验收。
