# Infinity dev7 版本与验收

Python SDK 固定为 `0.7.0-dev7`，Compose 的可选 `infinity` profile 使用
`infiniflow/infinity:v0.7.0-dev7`。两者配套升级，锁文件仅变更 Infinity SDK
及根依赖约束，其他包记录保持不变。本仓没有 Helm 部署入口。

本次对齐 `87516edadfba44f4bfb54633cfdc7552c5de8538`，冻结参考上限为
`519e7d98a5651564d4e35d6648f006cba4baaf4f`，不跟进该范围内更晚的正式版本。
dev6/dev7 SDK 的 24 个 Python 源文件中，仅握手处的版本注释不同，协议仍为 36。
保持现有存储接口、表／索引命名、元数据 JSON、检索错误语义和
[有限竞争重试合同](infinity-metadata-retries.md)，无需变更 Web 消费者。

## 隔离验收（2026-10-23）

后端基线为 `ed42a4c7f8059cb6e633f07f9d546351d340a91f`，使用独立 worktree
及按锁文件安装的 Python 3.12.9 环境。真实服务为原生 Linux arm64 镜像，
摘要 `sha256:5e02cf091fd5df6fd431faa8f8b55574d44435eafe79b34119f9dff44342e969`，
启动标识为 `v0.7.0-dev7-0-ce1793118`。

自有容器绑定 `127.0.0.1:55522`，使用独占持久卷；私有测试配置将 buffer 限为
512MB、memindex 限为 128MB，未修改仓库配置或共享服务。以下为本轮实际证据：

- `uv sync --group dev --frozen --python 3.12`、`uv lock --check --offline`
  通过；现有 SDK／lock／Compose／运行环境一致性测试复用本轮 dev7 环境。
- mypy：147 个文件通过；import-linter：8 项契约通过。
- 定向单测：11 个文件、165 passed、零 skip，覆盖 SDK 版本一致性、过滤／身份、
  错误分类、重试耗尽、连接归还、文档可见性和分块顺序。运行于离线模式；API
  收集所需的三个 OCR 文件从本机缓存按哈希复制，仅作为导入前置资源，验后删除。
- `REQUIRE_SERVICES=1 INTEGRATION_NO_TESTCONTAINERS=1 INFINITY_TEST_URI=127.0.0.1:55522`
  的 Infinity 专项：24 passed、零 skip。覆盖标量／全文／向量／混合检索、跨表分页、
  身份保留、更新删除、失败可见性和元数据 JSON 独立读回。
- 两组各 12 路并发 DDL：分别观察到 7／11 次目标竞争重试，每组借出／归还
  均为 48 次，活动租约为零。同表 DROP 的 2 次 `4005` 仍保持失败。
- dev6/dev7 六组默认分词样例的粗细结果一致，包含中英文、数字、重音、空串和
  字段名；实际 `core.nlp.rag_tokenizer` 的普通引擎结果一致，Infinity 模式仍原样
  传递文本。样例比较不代表所有输入的等价保证。
- 24 个 scratch 数据库和表／索引创建标识从操作开始写入外部资源日志。
  fixture 清理后，独立 SDK 连接确认只剩空的 `default_db`；容器及独占卷已删除。

额外 `uv pip check` 返回三项既有冲突：XGBoost 的平台产物不匹配，以及 Moodle
对 attrs／cattrs 的旧版本约束。原 main 环境只读检查也复现这三项，相关锁记录
未变；本项未调整这些无关依赖，红色检查结果保留在证据目录。
首次单测在 OCR 自动下载阶段中断，随后离线收集确认缺少本地模型；两个 exit=2
记录均保留。补齐上述三个缓存文件后，原 11 文件测试集合通过，没有跳过用例。

## 证据与边界

本机证据目录为
`/Users/xldu/.local/state/multirag/validation/20261023-batch-after-bbb0798c/E/`。
`input-manifest.json` 绑定基线、冻结范围及执行输入哈希；`reports/*.json` 保存
命令、退出码和原始日志哈希；`reports/integration/` 保存 run、JUnit 和事件报告。
`container-identity.json`、`server.log`、`scratch-resources.jsonl` 和清理读回记录
绑定服务身份与资源生命周期。

本项只验证新建隔离实例与现有本地存储合同。生产持久卷原地升级／降级、
Linux amd64 运行、多副本负载、生产切换及远程 `ci-required` 尚未验证。
最终组合检查由集成流程执行；历史 [dev6 验收](infinity-dev6.md) 不代替本轮证据。
