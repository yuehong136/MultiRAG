# 测试执行与证据

通用交付门禁以 [AGENTS.md](../AGENTS.md#验证) 为准。单元与集成在独立进程运行，避免单元测试的资源假件污染真实服务；`make test-all` 顺序执行两者。

## 执行范围

| 命令 | 范围与依赖 |
| --- | --- |
| `make integration` | 核心集成；包括 SQL、真实 HTTP、存储、受控模型及恢复测试 |
| `make integration TESTS=tests/integration/test_async_engine.py` | 仅指定文件；也支持 `文件::用例`，在导入应用前按文件准备依赖 |
| `make integration-db` | 已确认仅需 PostgreSQL 的文件，不收集 HTTP、OCR、向量库测试 |
| `make integration-db PYTEST_ARGS='-q -n 2'` | 两个进程，各自使用 scratch 库，按文件调度；不默认扩大并行数 |
| `make integration-system` | 独立进程被终止后，提交保留、未提交事务回滚和服务恢复 |
| `make integration-infinity` | Infinity 真库契约；可用 `INFINITY_TEST_URI` 明确选择实例 |
| `make integration-consumer` | 独立 Web 客户端；需要已安装依赖的 `WEB_DATASET_CHECKOUT` |
| `make integration-all` | 核心、Infinity 和 Web 消费者全部执行 |

`PYTEST_ARGS` 可传 `-k`、`-x` 等 pytest 参数。文件集合定义在
[`integration_suites.py`](support/integration_suites.py)，新增文件默认纳入核心完整依赖，
经确认后才能加入数据库分组。标签负责用例筛选，文件分组负责避免收集无关模块。

核心门禁显式排除独立 Infinity 文件和 `external_consumer` 用例，不将缺失环境的 skip
计作成功；CI 为 Infinity 单独运行必过 job，Web 消费者由对应专项命令证明。
涉及 Infinity 或 Web 契约的修改必须运行专项。
直接 `pytest tests/integration` 仍可用，但应用在收集期导入时可能访问服务；需要自动准备
临时端点时使用 Makefile 入口。混合收集时 `integration` 标签仅作用于集成目录。

## 服务与隔离

入口先做协议就绪检查，再导入应用。PG、Redis、MinIO 探测包含认证；Milvus 查询版本；
Infinity 在有时限的子进程内完成真实 Thrift 握手和数据库列表读取。
已有实例只读探测、不会由测试框架停止；缺失的 PG、Redis、MinIO、Infinity 可用
Testcontainers 启动，镜像从 Compose 读取。Milvus 必须预先提供，当前不自动创建整套
Milvus 依赖。`INTEGRATION_NO_TESTCONTAINERS=1` 禁用自动启动。

临时端点通过进程内配置和 0600 外部覆盖文件同步给应用及子进程，不修改共享配置。
测试使用随机 scratch 库、桶、集合和 key。创建后立即注册清理；初始化失败也执行清理。
普通业务测试应以独立连接读回提交结果，不通过事务回滚替代对真实 commit 的验证。
迁移测试需使用隔离 schema/数据库，不能并发修改同一个 schema。

`runtime_upload_api` 和对象读回已集中到 [`support/runtime_upload.py`](support/runtime_upload.py)。
其清理采用 ExitStack，某项清理失败不会阻断其余资源回收，失败仍会使验收变红。
数据库套件允许 xdist；其余套件涉及共享全局状态与跨存储补偿，目前拒绝并行运行。

## 失败与耗时证据

每次 Makefile 集成运行创建独立 `.test-results/<运行标识>/`：

- `run.json`：套件、选中/未选择的文件范围、所需服务、复用/临时来源、准备耗时和最终退出码。
- `execution-main.json` / `execution-gw*.json`：模块收集结果与耗时、选中/排除用例及 setup/call/teardown 结果。
- `events-main.jsonl` / `events-gw*.jsonl`：逐用例开始和阶段完成记录；即使进程中途退出，已写入的进度仍可定位停顿。
- `junit.xml`：pytest 标准结果，供 CI 与分析工具读取；控制台同时打印最慢 25 项。

`REQUIRE_SERVICES=1` 下，选中集成用例及模块收集时的意外 skip 转为失败，正常 xfail 保持 pytest 语义。
CI 保存报告和失败时的服务日志 7 天。证据目录忽略入库，不在报告中写配置或凭据。
现有业务用例的专门资源快照继续保留。

比较性能时使用相同文件、参数、服务版本和缓存状态；同时记录准备、收集、执行与清理，
不能把缩小执行范围当作同一套件的加速。先比较串行与两个进程，再根据瓶颈决定是否增加。
模型导入所需的 OCR 文件由 `scripts/provision_test_assets.py` 按不可变 revision 下载，
校验 SHA256，缓存命中不访问网络，失败下载不覆盖原文件。

## 后续覆盖边界

现有测试以真实存储和受控模型输出验证工程契约。独立进程恢复目前验证数据库服务的事务
边界，并不代表完整 worker 崩溃、消息重投或部署恢复已验收。
真实模型质量评估应另建版本化的业务样本集，记录模型/提示词/语料版本、检索命中、引用、
工具轨迹、延迟与成本；人工核定预期结果后再校准阈值。普通集成绿色不能替代这些质量证据。
