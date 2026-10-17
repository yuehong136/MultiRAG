# Infinity dev6 版本与验收

Python 后端的 `infinity-sdk` 固定为 `0.7.0-dev6`，Compose 的可选 `infinity`
profile 使用 `infiniflow/infinity:v0.7.0-dev6`。升级时同时采用这两个版本；
不改变默认文档引擎、启用的 profile、连接配置、数据卷或数据库名。
`uv.lock` 在本地通过 `uv lock --upgrade-package infinity-sdk` 生成，仅改变
Infinity SDK 包及根依赖约束。azure-identity/pluginlib 的约束与锁定版本均不变。

本项对应 `12f80f170cc732b19f54a5ff4e75ce368694bcf8`，冻结上限为
`519e7d98a5651564d4e35d6648f006cba4baaf4f`；本仓无 Helm 部署入口。
保持现有 Python 存储接口、表/索引命名、元数据 JSON 与
[有限竞争重试合同](infinity-metadata-retries.md)。不启用 SDK 新增的语言设置，
不扩展检索或权限接口，也无需 Web 消费者改动。

## 隔离验收（2026-10-17）

源基线为 `9582bdb80f1a1be4505f4061d1abe71aa4cac63a`；独立工作树
`/Users/xldu/.codex/worktrees/infinity-dev6/multirag` 使用独立 Python 3.12.9 环境。
测试所用 SDK 为 `0.7.0.dev6`，服务启动日志标识
`v0.7.0-dev6-0-39c473403`，Docker 镜像摘要为
`sha256:fa64bbcff161015aee1835b9fbc523281e8d4a67a4783a6df40fbddee9edec14`。
自有容器只绑定 `127.0.0.1:53158`，无挂载卷，没有使用或重启共享服务。

- `uv lock --check --offline`：exit=0；运行环境、锁文件、项目约束与 Compose
  版本一致性测试通过。
- `make verify`：exit=0；6260 unit passed，Ruff 格式/检查、8 项 import 契约、
  async DB 门禁与 mypy（144 文件）通过。最终重跑同时保存源码哈希与退出码；
  较早的 6259 passed 日志不代替这次包含新增版本一致性测试的完整结果。
- `uv run --no-sync pytest tests/unit/test_infinity_dependency_contract.py tests/unit/test_infinity_metadata_retry.py tests/unit/test_tokenizer_chunk_order.py -q`：
  104 passed，exit=0。覆盖错误分类、非法重试参数、耗尽/中断/提前返回释放以及现有分块行为。
- `INFINITY_TEST_URI=127.0.0.1:53158 INTEGRATION_NO_TESTCONTAINERS=1 make integration-infinity`：
  22 passed、零 skip，exit=0。通过实际 SDK 验证标量过滤、向量/全文混合检索、
  分页、多表检索、更新删除、错误可见性；新增 metadata JSON CRUD 用例独立读回
  中文、列表、布尔值、零值与整体替换，6 次借用全部归还。
- 两组各 12 路并发创建/删除数据库内的表及索引：不同表组观察到 7 次竞争重试，
  同表组 11 次，每组借出与归还均为 48 次，活动租约为零。同表 DROP 的 4 次
  `4005` 仍抛出，不扩大 RocksDB 重试范围，也不将失败操作计作成功。
- scratch 数据库由 fixture 删除并读回；测试结束后另建 SDK 连接确认仅剩
  `default_db` 且其中表集为空。自有容器随后删除，并确认容器列表中已不存在。
- dev5/dev6 SDK 默认分词对照的四组英文、中文、数字/连字符与重音文本样例，
  粗细分词结果均一致；这不是所有语言和输入的等价保证。

## 证据与集成边界

本机证据索引：`/tmp/infinity-dev6-audit/`。`source-manifest.json` 绑定基线与
候选文件 SHA-256；`verify-final.json`/`verify-final.log` 保存完整 verify 结果；
`container.json`、`server.log`、`cleanup-readback.json` 保存服务身份与清理读回。
专项结果为工作树中的 `.test-results/20261017-225000-34061/{run.json,junit.xml}`，
原始命令输出为 `/tmp/infinity-dev6-integration.log` 与 `/tmp/infinity-dev6-focused.log`。

本项只交付隔离候选，组合回归与最终 HTTP smoke 由统一合入流程执行。
未部署生产、未验证生产持久卷原地升级/降级或多副本 HTTP 压测。生产升级前仍需
独立备份并验证恢复；本次新建实例验收不能证明旧卷可直接降级。
