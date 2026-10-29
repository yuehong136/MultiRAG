# 298a23f7 后二十项本地交付

2026-10-30 冻结排除 `298a23f74c41cdcf6dbaca6ff574e69a3a2a8e80`，first-parent 连续
20 项至 `10bbe6b5d40bdd371df5d82c3732198ba2dde8f9`，共 139 条逐提交路径足迹。
后修参考上限为 `519e7d98a5651564d4e35d6648f006cba4baaf4f`；未 fetch，未用更晚工作树。
初始后端 `b4f65432`，初始 Web `688703a5`，起始 pin `bb49639a`。
本批实现和适用的本地验收已接收；未 push、未部署，远程 `ci-required` 待运行。

|项|上游 SHA|处置与实际行为|
|---|---|---|
|1|`1ab51a27`|Python/Web 提供 Tongyi CN/Intl 地域；正确 compatible-mode 地址、显式 URL 优先、未知地域拒绝；native SDK 端点和凭据按请求隔离。|
|2|`d02eb6b5`|Go CLI 独立 API/admin profiles、统一 common dispatcher、私有原子配置保存；admin logout 改 POST。保留非零错误退出和现有入口；Italian 仅格式差异，不引入未支持 locale。|
|3|`c5862929`|GraphRAG batch/community 成功子步骤可恢复；绑定图、frontier、模型、配置和 prompt，Redis 执行时校验 writer token。相同输入部分失败后总调用 3 次而非 4 次；输入变化重算。|
|4|`287a4cfd`|Python 草稿新会话名称回退首个 query 的前 250 字符；Web 保留现有有界分页、完整文件树和 metadata 配置约束，不截成首 100 条。|
|5|`01a2a447`|Go CLI 命令后的 output/admin flags 正确消费，保留 CE 子命令参数与结构化输出；退役无活动消费者的 secondary CE 路径。|
|6|`d1c436b8`|Go 增加鉴权的 agents/prompts GET，完整加载后一次返回，失败不泄漏路径；不改变 Python 生产入口。|
|7|`c1496ffd`|memory 入队写 begin_at 和 DB owner tenant；collect/handle 校验 task→memory/source/tenant；配置按已解析数值模型身份与规范化类型集合比较。|
|8|`f0efa63b`|旧 MySQL/Peewee 脚本不存在，不恢复；当前 SQLAlchemy/Alembic 建表路径无尾逗号问题，隔离 PostgreSQL 迁移验证。|
|9|`719ce15c`|Go 动态库存使用 name 加可选 metadata；注入 catalog 的 service 集中补充，保留 driver 严格协议、原始 ID、顺序与基数，未知省略，0/false/空数组保留。|
|10|`7b8d6f34`|图片输出固定 JSON，direct-file facade 同步归一；OCR/VLM 到 split_chunks 的标签及图像/缓冲区生命周期一致。|
|11|`f97d6396`|保留更强 Baidu raw-key→LiteLLM/OpenAI-compatible 活动链；真实受控 SDK/SSE 验证多帧及失败零写入，不恢复旧 qianfan 包装。|
|12|`08a40711`|活跃 backfill 仅去除 boolean is_tools 的凭据包装差异，保留各模型 tools/numeric ID/default；混合导入复用等价未改实例，已编辑实例不重绑，失败整批回滚。|
|13|`27732081`|保留 MinerU OCR0、真实 PDF parse probe 与原子创建；补齐 probe timeout 到 HTTP，取消排空。Web 使用同一 typed candidate。|
|14|`3eff4136`|保留更强 None/schema 校验、输入/cache 不变、空 schema 不调用模型和任务失败零副作用，不复制旧重构模块。|
|15|`7ed1f1c8`|保留当前 SDK 边界版本归一，mount 与已有 /v1、/v2 合同有效，不重写存量身份或重复拼版本。|
|16|`84482762`|Web 在同一候选中混选发现模型与多个自定义草稿；去重、明确 0/false、失败保留、等待提交。切换地域使旧验证失效且保留密钥草稿。|
|17|`e050f181`|保留更强 Google 多层 nil/空保护及 Ollama 多 text/image、可选 thinking 合同；补当前回归，不退化成仅首项或非法值静默空串。|
|18|`d4fe3bb1`|Go dataset metadata summary GET 复用 actor/context 与全量 SQL 文档选择授权，严格 CSV，错误不泄漏底层信息；不切换 Web 到 Go。|
|19|`acae9329`|auth nil-superuser 已覆盖；heartbeat 改为有界完整 JSON、严格 integer/status/license 码，失败保持重试状态，测试计数使用同步。|
|20|`10bbe6b5`|Web Visual File 接受 array<file> 变量，保留其他数组边界和结构化/null 处理；Loop 按现行 no-debug 合同处理。|

运行合同分别见 [模型实例](../../api/db/services/MODEL_PROVIDERS.md)、
[GraphRAG](../../core/graphrag/README.md)、[Go CLI](../../internal/cli/README.md) 和
[Go 模型驱动](../../internal/entity/models/README.md)。逐 139 路径的完整质量比较在外部本批
`item-ledger.json`，原始结果和源码/锁/配置/消费者绑定在 `validation.json`。

## 本地验证与复用

生产后端使用 Python。锁定组合的定向 unit 520 passed、SQL/Redis/Milvus core 12 passed，
固定实际 Web `0a5f80fe312331794ffb968d23bc57e1b39880fd` 的真实 API client→JWT→Python→
受控供应商→独立 SQL 五场景全部通过，均无 skip。改动 lint/format、8 条 import contracts、
async DB 门禁与当前纳管 mypy 156 源文件通过。Web build、适用类型/lint/i18n/file-size/测试和
中英明暗主题真实浏览器交互已检查；`688703a5` 独立文本/语音改动保留，不计为本批成果。

Go 为并行实现。当前源码 native CMake 构建、词频/POS 运行回归、全 internal build/vet、三个
独立入口链接、实际 JWT/API/SQL 路由及最终 CLI discovery 专项通过。所选包 904 顶层测试
通过，另有 86 个未配置专用环境的额外 live 用例 skip；race 范围 208 顶层通过，45 个额外
live 用例 skip，不把这些 skip 计为验收通过。初次 service 假件失败与新增测试 race 已保留，
最小补修后完整 service unit/race/vet 重新通过；其他包的未变成功输入分范围复用。

消费者首轮 4 passed/1 端口占用失败保留。仅 fixture 对 EADDRINUSE 做 32 次有界重选，
每候选先登记，失败关闭 socket；其他 errno 或耗尽仍失败。最后五场景在实际最终 pin 新跑通过。
后续 test-only 补修未改变生产源码；单测、类型和其他已通过输入不重复跑全套。

## 留下的边界

- 非完整 Python/Go/Web/真实供应商验收；生产、付费账号、常驻 worker、部署及远程 CI 未运行。
- 未改调用链的 `test_extractor_utils_reuses_runtime_extractor_async_flow` 仍有旧 fake canvas 缺少
  globals 的失败，未计入通过；P 额外查纳管外类型时发现的旧 Result.rowcount 问题另列证据。
- 旧 memory 队列消息缺少可信 tenant 时会拒绝并确认；本批没有查看生产 backlog。
- 历史及本批登记偏差不补写。D 早期 cache、失败 smoke 误建 .venv 的路径登记不完整，
  后续已如实观察、精确清理并复验；因此 strictProtocolPassed=false，清理成功不等于历史协议通过。
- 原 28 项 WIP 保留；本批源码、候选恢复包、主线/Web/pin 读回、精确清理及未验证项的最终索引为
  外部 `20261030-batch-after-298a23f7/delivery-complete.json`。不自动派发下一批。
