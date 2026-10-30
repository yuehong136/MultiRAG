# Extractor 与模型默认值回填定向收尾

2026-10-30，在 `d26ae1db` 上修复 after298 交付中另列的两处遗留失败。
本地提交，不 push、不部署；远程最终 `ci-required` 待运行。

## 合同与修复

- 上游 `8f90740d2ec741a1a427fe5c51d92231f72ef3d7` 通过 Canvas globals 传递
  `chat_template_kwargs`。当前 LLM 在所有生成轮次读取该状态，并保留严格对象校验和深拷贝。
  旧失败中的 `_ExtractorCanvas` 是 `core/flow/utils/extractor_utils.py` 的直调适配器，
  不是测试假件；此前将其归因为 fake canvas 不准确。适配器现提供每实例独立的空 globals，
  表示没有请求级覆盖，不绕过 LLM 校验或撤回模板参数支持。仓库业务调用搜索未发现该导出
  helper 的其他活动调用，现有直接调用合同仍通过真实 Extractor/LLM 生成链验证。
- 上游 `08a40711a0b196ea7af6f8467c0aeebebbb2a651` 的凭据去重已映射到本地 SQLAlchemy
  backfill。数值默认值修复沿用本地 `b70f581a` 合同：租户和能力匹配、只填空值、保留原别名、
  不覆盖已有数值选择、歧义由调用者事务回滚。本次仅将无 RETURNING 的 UPDATE 执行结果标为
  `CursorResult[Any]`，使用其真实 rowcount；SQL、Session 同步及提交边界均不变。
- 自动 metadata 继续保留上游 `3eff4136` 对 None/schema 的修复意图和本地更强约束。
  不增加 JSON 字符串兼容、不放宽零值、空集合或无效请求的语义。

## 本地验证

- 两条原始失败在未修改的基线上分别复现并保留 raw exit=1。
- 106 项相关 unit 通过，零失败、错误或 skip：直调解析/提取、图片输出、真实 Canvas 模板参数、
  metadata 生成及字段合同。新增提取用例覆盖逐块结果、输入不变、空输入零调用与中途异常传播。
- 7 项 PostgreSQL 定向 integration 通过，`REQUIRE_SERVICES=1`，零失败、错误或 skip。
  覆盖所有能力与晚到 OCR、幂等计数、已有/外租户默认值、歧义与迁移回滚、凭据包装去重，
  以及解析后被另一 Session 改选时 UPDATE 计数为零且独立读回保留该选择。
- 新并发用例初次组合遇到前一歧义测试留下的行，原始 6 passed/1 failed 保留。
  defaults fixture 现于每次用例后清理其创建的七张表，保留 SQL 登记直到清理结束；修后整组合复验。
- 四个改动 Python 文件 Ruff check/format、显式 model_backfill mypy、当前纳管 156 文件 mypy
  和 async DB 检查通过。fixture 隔离修正仅影响 integration；未变的 unit/类型输入按哈希复用。
- 六个成功或失败测试创建的 scratch 数据库均由独立只读连接确认不存在。外层进程、缓存、
  本次源工作树与分支的最终清理见外部回执；借用服务、venv、模型原件和原有 WIP 保留。

Web 合同未改变，master/消费者 pin 保持 `0a5f80fe312331794ffb968d23bc57e1b39880fd`；
本次不重跑 Go/Web 或完整平台门禁。此前登记偏差及真实供应商、生产 backlog、部署和远程 CI
的未验收边界保持原结论。仅判断下一批能否进入规划，不在本次自动派发。

原始命令、JUnit、输入哈希、事务读回和精确清理索引：
`/Users/xldu/.local/state/multirag/validation/20261030-extractor-backfill-followup/delivery-complete.json`。
