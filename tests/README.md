# 测试执行与证据

通用交付门禁以 [AGENTS.md](../AGENTS.md#验证) 为准。单元与集成在独立进程运行，避免单元测试的资源假件污染真实服务；`make test-all` 顺序执行两者。模型质量回归使用独立命令和报告。

## 执行范围

| 命令 | 范围与依赖 |
| --- | --- |
| `make integration` | 已审核核心文件默认双进程；包括 SQL、真实 HTTP、存储、受控模型及恢复测试 |
| `make integration TESTS=tests/integration/test_async_engine.py` | 仅指定文件；也支持 `文件::用例`，在导入应用前按文件准备依赖 |
| `make integration-db` | 已确认仅需 PostgreSQL 的文件，不收集 HTTP、OCR、向量库测试 |
| `make integration INTEGRATION_WORKERS=0` | 串行调试；显式 pytest `-n 0` 也会覆盖默认并行数 |
| `make integration-system` | 数据库进程事务恢复，以及真实 worker 被终止后的消息重投、取消恢复 |
| `make integration-infinity` | Infinity 真库契约；可用 `INFINITY_TEST_URI` 明确选择实例 |
| `make integration-consumer` | 独立 Web 检索与导入日志客户端；需要已安装依赖的 `WEB_DATASET_CHECKOUT` |
| `make integration-all` | 核心、Infinity 和 Web 消费者全部执行 |
| `make eval` | 固定版本本地 OCR、中文向量模型、解析与真实 Milvus 检索回归 |
| `make eval-generation` | 显式真实模型回答、引用、拒答和工具选择评测 |

`PYTEST_ARGS` 可传 `-k`、`-x` 等 pytest 参数。文件集合定义在
[`integration_suites.py`](support/integration_suites.py)，新增文件默认纳入核心完整依赖，
经确认后才能加入数据库或并行分组。标签负责用例筛选，文件分组负责避免收集无关模块。

核心门禁显式排除独立 Infinity 文件和 `external_consumer` 用例，不将缺失环境的 skip
计作成功；CI 为 Infinity 单独运行必过 job，Web 消费者由对应专项命令证明。
涉及 Infinity 或 Web 契约的修改必须运行专项。
直接 `pytest tests/integration` 仍可用，但应用在收集期导入时可能访问服务；需要自动准备
临时端点时使用 Makefile 入口。混合收集时 `integration` 标签仅作用于集成目录。
质量评测不被普通 pytest 递归收集，使用以上显式入口运行。

## 可重复的产品验收

```sh
make acceptance      # 完整流程 + 真实 Chromium + 明暗主题截图
make acceptance-api  # 显式只跑 API，报告不宣称已检查页面
```

完整入口自动启动相邻 `../web` 的已安装 Vite 项目，使用独立端口；其他目录可用
`uv run --no-sync python scripts/run_product_acceptance.py --web-checkout /path/to/web`。
`--web-base-url http://127.0.0.1:5173` 复用已运行的前端，`--headed` 显示浏览器操作。
需要 dev 依赖和 Playwright Chromium（首次准备：`uv run --no-sync playwright install chromium`），
基础服务由既有集成框架探测/准备；缺失依赖或选中项失败使命令非零退出，不以 skip 计成功。

验收创建独立 PostgreSQL scratch 库、MinIO 桶、Milvus 集合、Redis 队列和测试身份。
直接运行当前 API 代码，不使用常驻服务的登录态；前端请求仅转发到本次 scratch API，
保留真实请求体、鉴权和业务响应，不伪造接口成功。结束时读回 SQL 行、对象、索引及队列清理结果。
临时 worker 配置只写权限受限的临时目录，用后删除；不修改共享配置。

当前锁定的行为：上传后独立读取名称/大小，生产 worker 解析后完成状态与非零分块，
分块保留合成文件内容，检索返回该文档与预期文本；配置保存后独立 GET，比对省略字段保留、
显式空数组清空及父子分块关闭。真实页面执行上传、开始解析、配置保存/重载和 Enter 提交检索。
检索还打开详情核对完整原文。知识库列表、文档列表、分块、检索、数据集设置、个人设置与
API 文档在明暗主题、1440×1000 和 1024×768 视口检查路由、脚本/接口错误、页面横向溢出
及键盘焦点，并截图。切页取消的过时请求另列记录，当前操作仍须返回真实成功响应并通过内容断言。

证据保存在每次独立 `.test-results/<运行标识>/product/`：`acceptance.json`、
`acceptance.md`、完整 PNG、带文件名/路由/视口/主题的 `contact-sheet-*.jpg`。
脚本逐项写入结果，前置失败会标记依赖项 blocked；截图保留失败状态，清理结束前整体状态仍为 running。
自动检查通过后，`visual_review` 仍为 `pending_human_review`，最终视觉判断由人完成；
本入口不使用像素基线自动批准设计。报告目录可用 `--report-dir` 指定，必须为空，避免混入旧证据。

首版用合成 TXT 与受控 768 维 embedding；解析、后台进程及存储/检索实现均为生产代码。
它验证流程契约，不证明真实模型的召回质量，也不覆盖 PDF/OCR、生产部署或任意文件格式。
真实模型质量仍用下文的 `make eval` / `make eval-generation`。产品验收为显式独立套件，
不替代 `make verify`、适用 `make integration` 或消费者专项门禁。

## 服务与隔离

入口先做协议就绪检查，再导入应用。PG、Redis、MinIO 探测包含认证；Milvus 查询版本；
Infinity 在有时限的子进程内完成真实 Thrift 握手和数据库列表读取。
已有实例只读探测、不会由测试框架停止；缺失的 PG、Redis、MinIO、Infinity 可用
Testcontainers 启动，镜像从 Compose 读取。Milvus 必须预先提供，当前不自动创建整套
Milvus 依赖。`INTEGRATION_NO_TESTCONTAINERS=1` 禁用自动启动。

临时端点通过进程内配置和 0600 外部覆盖文件同步给应用及子进程，不修改共享配置。
测试使用随机 scratch 库、桶、集合和 key。注册清理后再创建资源；初始化失败也执行清理。
普通业务测试应以独立连接读回提交结果，不通过事务回滚替代对真实 commit 的验证。
迁移测试需使用隔离 schema/数据库，不能并发修改同一个 schema。

共享 fixture 与辅助函数放在 [`support/`](support/)，普通 `test_*.py` 之间禁止互相导入。
上传、Agent、数据集、图片、解析与取消场景复用这些入口。关键 fixture 使用 ExitStack
独立回收 SQL、对象、向量、Redis、监听端口和线程；某项清理失败不会阻断其余回收，
失败仍会使验收变红。初始化故障测试覆盖 SQL 提交后失败、桶创建后丢失响应、
数据库创建后丢失响应，并通过独立连接检查没有遗留资源。

xdist 的每个进程使用独立 scratch 库和随机外部资源，进程内全局补丁不跨进程共享。
只有 `PARALLEL_TESTS` 显式登记的文件可并行；新文件、Infinity 和质量评测不自动获得许可。
默认仅对全部已审核的多文件选择启用两个进程和 worksteal 调度；单文件或包含未审核文件时
自动串行，显式要求未审核文件并行则失败。`PYTEST_ADDOPTS` / `PYTEST_ARGS` 中的 `-n`
优先于默认值，已有 `--dist` 选择保持不变。
`--integration-seed=20261004` 可复现乱序，同一文件内用例顺序也会改变，模块 fixture 保持连续。
两组互不依赖的解析集合可并行创建；`--fixture-jobs=1` 恢复原先串行创建，供性能对照。

普通 pytest 在用例执行阶段使用 pytest-socket，允许 loopback 与 Unix socket；集成入口还允许配置中的
服务主机，生成评测额外允许专用模型地址。未授权的 Python socket 连接在发出前失败。
收集期应用导入不受该插件完整保护，OCR 资产需要预先准备，避免触发遗留模型下载逻辑。
这是 Python 层的意外联网防护，不是操作系统网络沙箱；原生数据库驱动、浏览器及其他
子进程不由该补丁完整约束。真实 worker 测试的 Python 子进程显式安装相同保护。

## 失败与耗时证据

每次 Makefile 集成或质量运行创建独立 `.test-results/<运行标识>/`：

- `run.json`：套件、选中/未选择的文件、依赖服务、复用/临时来源、准备与总耗时、退出码、Python/平台/关键包版本。
- `execution-main.json` / `execution-gw*.json`：收集耗时、选中/排除用例、乱序种子、fixture 并发数和 setup/call/teardown 结果。
- `events-main.jsonl` / `events-gw*.jsonl`：逐用例开始和阶段完成记录；进程中途退出也能定位停顿。
- `junit.xml`：pytest 标准结果；控制台同时打印最慢 25 项。

`REQUIRE_SERVICES=1` 下，选中集成/质量用例及模块收集时的意外 skip 转为失败，正常 xfail
保持 pytest 语义。CI 保存报告和失败时服务日志 7 天。证据目录忽略入库，不写配置或凭据。
现有业务用例的专门资源快照继续保留。

性能对照必须使用相同用例、种子、服务和缓存状态，且不能同时运行两轮全量测试：

```sh
uv run --no-sync python scripts/run_integration.py --suite core --workers 0 --report-dir .test-results/serial -- -q --integration-seed=20261004 --fixture-jobs=1
uv run --no-sync python scripts/run_integration.py --suite core --report-dir .test-results/parallel -- -q -n 2 --integration-seed=20261004 --fixture-jobs=2
uv run --no-sync python scripts/compare_integration_runs.py .test-results/serial .test-results/parallel
```

比较器拒绝失败、skip、缺少阶段、重复执行、选中但未执行的用例，以及用例集合/环境指纹/
已知服务版本不同的结果。当前直接探测 PG 和 Milvus 版本，其余服务版本未知时记为 null；
因此仍需操作者确认服务实例、缓存和后台负载一致。阶段耗时之和在并行运行中会重叠，
单次对照只能说明该机器上的一次观察，不能承诺每次固定加速比例。

## 恢复覆盖边界

`integration-system` 使用独立子进程，数据库案例验证已提交事务保留、未提交事务回滚。
worker 案例运行生产 `handle_task()`、真实 Redis pending 消息、解析器和 SQL/MinIO/Milvus，
仅控制 embedding 输出与终止检查点。它验证：

- 结果已落库、XACK 前进程被终止：重启后清空 pending，SQL 计数、操作日志、原始对象及向量内容不重复改变。
- 解析结束后进程被终止、随后通过真实 API 取消：重启消费后仍保持取消状态，不能继续写入索引。

这些证据覆盖指定的两个崩溃窗口，不代表任意跨存储中间状态都获得 exactly-once 保证，也不等同于部署恢复验收。

## 本地质量与显式生成评测

先安装可选依赖组，再显式准备资产；质量评测在导入解析器前校验缓存，缺失即失败：

```sh
uv sync --group dev --group eval --frozen
make eval-assets
make eval
```

OCR 与中文 BGE-small 向量模型都按不可变 revision 和 SHA256 校验；下载失败不替换缓存。
OCR 使用 `core/res/deepdoc`，向量模型使用已忽略的 `.cache/evals/`。缓存命中时不访问网络。
CI 在独立准备步骤下载、校验并缓存，然后以单独的 `make eval` 步骤运行质量门禁。

[`evals/corpus/v1/`](evals/corpus/v1/) 是版本化的合成中文回归语料，含采购审批、数据保留、
套餐表格、支持时限和固定收据图片。10 个正相关检索样本走真实解析、本地 embedding、
Milvus 和生产 Dealer；另外检查表格事实与真实 OCR 的编号、数量和日期。
报告保存语料版本/哈希、模型 revision、依赖版本、Recall@3、MRR、延迟及本地模型调用成本 0。
本地 CPU/基础设施成本不包含在该数值中。这套小语料尚未经过生产领域人工校准，不代表业务质量上限。

生成评测按需配置本机环境变量，再运行 `make eval-generation`：

- `MULTIRAG_EVAL_BASE_URL`：专用 OpenAI 兼容服务地址。
- `MULTIRAG_EVAL_API_KEY`：专用评测密钥，仅放环境变量或本机秘密管理器。
- `MULTIRAG_EVAL_MODEL`：评测候选模型名。

缺少配置时命令明确失败，`quality-generation.json` 记录阻塞原因和执行 0 项，不 skip。
配置完整时，13 个样本通过生产聊天/工具适配器检验回答事实、引用覆盖与来源、无答案拒答、
工具名称及参数；工具服务使用版本化的只读合成工单。报告记录模型、提示词版本、token、
延迟及完整断言。费用在没有账单证据时保持 null，不凭 token 总数猜测。
事实检查以预期短语/独立数字匹配为主，并非语义裁判；下一步引入真实业务集时，应先人工审定
预期、校准阈值再升级评分，不能用普通集成绿色替代真实生成质量证据。
