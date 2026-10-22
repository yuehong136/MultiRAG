# 7ec87f7c 后二十项移植与八增量复验

本批已按冻结范围本地实现和验证，不 push、不部署；远程 `ci-required` 待运行。

## 锁定范围

- 上游起点（不含）：`7ec87f7cb78889180a1cd67d46d10a247977ae7e`；冻结上限：`519e7d98a5651564d4e35d6648f006cba4baaf4f`。
- 按 first-parent 顺序取其后 20 项，末项 `0580c137fa2eaac8f8774ce32076d1003274c2a7`。独立核对与 `scope.json` 的 20 个完整 SHA 一致，未静默改变上限/游标。
- 初始后端 `c595f17f8a079217fbcb201897adba864da25bf4`；最终源码 `d96b9b2430febdfb3a724bc2e4e9ffc469703142`（组合运行于 `ce86418e`，补修另验），Web `eed958f792c52fac91490acec6a9eae00edd659a`。唯一消费者 pin 为 `.github/consumer-web.json`，`3a723fbe` 已更新；配置提交不代表远程 CI 已执行。
- 外置证据根：`/Users/xldu/.local/state/multirag/validation/20261022-batch-after-7ec87f7c`。范围、实际派发、审查与源提交映射分别见 `scope.json`、`dispatch.json`、`handoffs/*-final.json` 和 `integration/merges.jsonl`。
- 初始主目录 28 处无关 WIP 已逐文件外置备份、记录 SHA-256 和二进制补丁；每次串行合入核对内容。原 PROGRESS 历史 delta 保留，未处理 SDK/ironwood/g129 等其他工作树、服务或分支。

## 独立八增量

这些是基线已包含的本地增量，不计为上游第 21—28 项。V 的生产输入固定为 `c595f17f`，仅新增测试提交 `03a21ee3`（合入为 `29ccdc87`），没有新增生产修复。

| 本地提交 | 核对与本轮证据 |
|---|---|
| `003ef37ed627` | 手工 chunk 建立 dense/BM25 索引，初始化失败传播，释放后的历史读等待加载，文档列表失败安全返回；真实 HTTP、SQL 计数与双索引搜索读回。 |
| `949cb35a304f` | 单调时钟总预算覆盖 readiness 各 RPC/load；失败索引/耗尽抛错。定向单测覆盖 deadline，真实历史/初始化链覆盖调用。 |
| `8278679e80b6` | stale rows 先按 graph/subgraph/entity/relation 分派，再读取 typed identity；真实 Milvus checkpoint 生命周期与失败不删除读回。 |
| `35607e0b1725` | nullable 字段用默认值序列化，显式零/空值保留；真实 native null、HTTP roundtrip 与 disabled=0 读回。 |
| `7c86a8a42945` | 私有数据集不能仅因成员关系获得访问，明确共享后按选定索引检索；JWT/API key 权限矩阵实际运行。 |
| `3dae701e4ffd` | beta 租户检查沿请求 session，能看到未提交/失效状态；没有恢复全局同步 session。 |
| `205868fffcdf` | 租户/provider 模型解析进入 LLM、Agent、HTML、dialog、Memory 和 prompt 的真实 LLMBundle 调用；歧义/缺失/禁用/无类型/跨租户拒绝零 provider 调用、SQL 不变。 |
| `c595f17f8a07` | 文档边界核对；仅提前覆盖 `dc4b8252`、`6a77523b`、`4c97c9ad` 的部分问题，这三个不在本次 20 项，不宣称完整移植或推进游标。 |

V 本轮 320 个定向单测、56 个 Canvas 用例、九文件 71 个组合集成、49 个真实模型消费链、1 个 chunk roundtrip、2 个固定 Web 消费者通过，均零失败/skip；重叠不累加为新覆盖。另有 10 个读回场景因默认 pytest 临时目录被并发清理而仅补跑外置证据，原 JUnit/事件保留。Canvas 的 5 个旧 fixture 失败已先复现，再显式替换其模型解析边界，原构造/reset 断言不变。

core 中一个 `external_consumer` 节点按规则 deselect，已在独立 consumer 运行中实际通过：`test_dataset_search_http.py::test_actual_web_client_against_scratch_http`。这与意外 skip 不同。V 的旧 Web 证据只绑定 `419fc9a`，最终 Web 见下文新组合。

V 的 `V-resource-readback.json` 独立核对 5 库、21 集合、3 桶、40 Redis 键和 13 listener，枚举资源剩余为 0。新 20 项中的 `init_kb` 与 prompt resolver 关键函数已做 AST 比较，保留上述 readiness 和租户解析逻辑。

## 二十项映射与机制比较

每项已检查完整原 diff、活动消费者和同链后修；后修仅用于本项正确性，不扩大连续游标。Go 是并行实现，不将其描述为 Python 生产后端能力。

| 编号 | 上游 SHA | 线 | 处置与比较 |
|---|---|---|---|
| 1 | `ed01ac999408` | D | 本地适配。混合模板先展开字符串片段，完整引用保留对象/数字/布尔/空值；复用现有格式化与错误语义，补下划线组件 ID。真实 Email 工具使用受控 SMTP。`9b3846e7`、`673aedb5`；`d96b9b24` 补字面空白保留。 |
| 2 | `889aba6a32b3` | A | 本地适配。HuggingFace 协议、尾斜杠、既有 rerank 路径与 query 归一，保持 8 条分批和顺序；复用 URL helper。`ba21d85c`。 |
| 3 | `3c4d1da98fb4` | B | 本地适配。auto/manual、indexing/metadata/both 与合法 vectorize 归一；原始类型载荷在索引前消费，不靠首行猜字段；多 sheet 无损类型/字段合并。私有 SQL 状态记录行任务贡献与解析器新增值，重新解析保留手工字段；失败不报成功。新迁移与 Web 同批。代价是新增所有权状态；收益是可清理生成值且保留多任务复用。`3f0b941a`、`ce86418e`。 |
| 4 | `08bb53bbb11c` | A | 本地适配。活动 CV 注册器新增 BedrockCV，LiteLLM 图像/提示/用量、access-key、STS IAM role 和现有 Web 默认凭据链信封；关闭 STS、空响应失败、60 秒请求边界。既有 boto3/锁文件足够，不重写依赖。`d384da62`、`b5d570db`。 |
| 5 | `39a1773f7f28` | F | 等价后补强。既有 GET/data 清单、目录和连接检查保留；补缺失/null 清单、非法/重复 ID、尾随 JSON 拒绝与路径归一。保留原始 ID 供 model@instance@provider 消费，避免假空成功。`43695be7`、`c78331b7`。 |
| 6 | `51b73850e1da` | C | 直接移植。三份 Dockerfile 的 NEED_MIRROR 默认 1 保持原构建，可选 0；六组原生 arm64 构建和离线读回通过。不改全局 registry。`add39fd0`。 |
| 7 | `13922209e69f` | A | 直接移植并适配。目标 CV/embed/rerank/ASR/TTS 请求补 30/60 秒边界，保持已有受限请求；关闭音频流，FishAudio 不再无限等待，Volc 配置拒绝非有限/非正超时。只改活动 flat 模块。`d15fbf65`、`a585cf23`。 |
| 8 | `f4f8bed9f7af` | E | 本地适配。真实 Gemini SDK 批量向量和 text-embedding-004 目录；严格数量/null/有限及请求维度，零分量有效、全零向量拒绝；实例 URL/key 独立。活动 GetVector 用副本标为 Query，保持文档配置。`a48ec65d`、`e3ebec80`。 |
| 9 | `f852a7524ee1` | E | 等价后补强。保留本地分页、重复 token 拒绝、取消和安全错误，优于原补丁全局 stub/无界 context；补 SDK null 根对象及缺失/null 清单、坏模型名失败。显式空清单有效，不增加供应商调用。`2202f252`。 |
| 10 | `827cceccba89` | F | 生产等价。原 SHA 实际对未知/空区域 URL 报错；本地统一 endpoint 已覆盖 chat/stream/list，实例工厂负责默认回退。保留统一实现，补零 HTTP 拒绝、实际区域路径、取消和并发隔离证据，不复制三份 guard。`c78331b7`。 |
| 11 | `e6cb9faacead` | C | 生产等价。d9cc7159 已覆盖 builtins、属性危险调用和 JS 三种引号 require；不重复补丁。补默认 CI 收集的 17 项测试及实际 /run 拒绝/队列探测；正常代码仍通过。`bf6994bb`。 |
| 12 | `b83e2ae5a282` | D | 本地适配。缺父块回退对应 children；按租户/KB 索引和文档分组，SQL 重查删除/禁用后替换，避免原补丁只读首个索引。保持排序、去重、child vector 和原输入；新增有界 SQL 读取换取资源有效性保证。`af1cde58`。 |
| 13 | `bfb4a0eea2d9` | G | 本地适配。Gitee 目录补 BAAI/bge-m3 与 Dimension；沿同链后修纠正 embeddings 路径。共享严格索引/有限维度解析及 32 分批替代上游弱解码，代价为 ceil(N/32) 次请求。`bb3b5992`。 |
| 14 | `d6660cf156d5` | B | 生产等价。build_chunks 缓存命中/未命中及 run_dataflow 已共用清洗函数，覆盖中文逗号/分号/顿号/CRLF，另有去空白、去重和 Milvus 限额；优于原始 re.split。补真实调用回归，不造空提交。测试在 `3f0b941a`。 |
| 15 | `fa53b93dd57b` | G | 本地适配。vLLM 在既有 /v1 命名空间补共享严格 Encode，key 显式可选，其他云端默认仍强制 key；保留聊天/发现、Dimension、32 分批与失败/取消合同。`ffba29fe`。 |
| 16 | `e46989832eed` | A | 本地适配。HTTP 先检查状态；重排必须完整、唯一、合法整数索引和有限分数，合法零与已校准分数保留。SDK 限时、空输入/数组、Qwen 无 text 错误、HF 后批失败和自定义端点均覆盖；不采用上游 catch/log 后零分成功。`b6b25bff`、`5031cd8a`；`1280e7c1` 在 Cohere SDK 强制类型转换前校验原始响应。 |
| 17 | `77ce88dfcc4a` | D | 本地适配。所有 system、当前 turn、tool 固定载荷计入预算，截断后重算实际 token，不负切片。非正预算沿同链默认 8192；不可拆工具载荷超限明确失败。图片和 tool 协议保留，图片/消息封装计费仍在 provider。`6cd3a8e6`。 |
| 18 | `8ff623fbc44e` | G | 本地适配。Ollama 原生 chat/tags 与既有 factory 保留，补受支持的 v1/embeddings 和可选 key；复用严格共享管线。比较后续 native embed 方案后不叠加第二套解码。`1a8dec97`。 |
| 19 | `4b9636209250` | H | 本地适配。补六个 NVIDIA 目录模型和 endpoint；默认 query，显式 Query=false 为 passage，传 float/END/Dimension；30 秒总预算服从取消。复用共享严格解析及 32 分批，避免重复实现。`618dcea1`。 |
| 20 | `0580c137fa2e` | H | 既有机制保留并补缺口。共享 helper 已有 32 分批和全局顺序恢复，保留严格索引/有限维度/后批失败合同；补 SiliconFlow Dimension 透传和构造 map 副本，0/1/32/33/65 与取消均验。`27647e94`。 |

表格使用原始类型载荷，避免上游首行猜字段、稀疏行遗漏、仅 manual 聚合和吞保存异常。复用摘要包含有效列角色配置；重新解析只移除解析器新增值。首次无元数据行的并发丢手工值已用两条真实 SQL 会话复现，补 `INSERT ON CONFLICT DO NOTHING` 后锁定/重读；失败时种子行与状态一起回滚（`ce86418e`）。与生成值完全相同的手工重新添加无法辨别来源，仍按生成值处理，已写入元数据配置合同。

Go 实际消费链发现旧全局 APIKey 唯一索引会阻止 vLLM/Ollama 免 key 实例共存；`c95fe831` 为同链最小修复。先建非空 key 条件唯一索引，再移除旧索引；非空 key 唯一性、旧行和云端 key 要求保留。PostgreSQL 18.4/MySQL 8.4.11 的旧表升级、重复启动与失败零行写入另有实际证据。

## 最终定向组合与复用

Go 冻结于 `bfb0fc88a78e6c1113166fef9ad52cd9e9d3cc19`，后续 Python 提交的 Go/目录/锁文件差异为空。根 `integration/go-combination.json` 保存原始命令、exit、日志和资源状态：

| 根组合 | 结果 |
|---|---|
| models/entity/CLI race | 1239 个测试/子测试通过，0 fail/skip |
| 相关 service/NLP 消费者 race | 38 个测试/子测试通过，0 fail/skip |
| E/G/H 实际 HTTP/SQL race | 6 个测试/子测试通过，0 fail/skip |
| 旧实例表迁移/重复启动/唯一约束 race | 3 个测试/子测试通过，0 fail/skip |
| `go build ./internal/...`、`go vet ./internal/...` | 均 exit=0 |

根 Go 组合运行于 PostgreSQL 17.11，供应商 HTTP 与身份边界受控，实际 CLI/handler/SQL 与独立读回保留。四个自有 scratch 库创建前登记，完成后 DROP 并读回 0；私有配置删除。各线三个独立 cmd 的构建按相同 Go 输入复用，原始退出码保留；不因无关 Python HEAD 变化重复构建。

Python 主组合锁定 `ce86418e`、初次 Web `ca3f50a`，根证据为 `integration/python-combination.json`。最终 Web `eed958f` 更新 pin 后重新运行三个消费者，见 `integration/web-final-validation.json`；下表列主组合与最终 Web 结果：

| 检查 | 结果 |
|---|---|
| 本批修改文件 Ruff/format、依赖分层、async DB、纳管类型 | 全部 exit=0；补修四文件 Ruff/format 亦 exit=0 |
| A/B/D/V 相关文件去重组合单测 | 988 passed，0 failure/error/skip；补修后相关 419 passed，不与前者叠加为独立覆盖 |
| table/父子检索/模型/chunk/权限/元数据范围组合 | 75 passed，0 failure/error/skip，REQUIRE_SERVICES=1 |
| 最终 Web 的真实模型保存、文档保存/下载、检索消费链 | 3 passed，0 failure/error/skip，真实固定 Web eed958f |

组合完成后合入 `1280e7c1`（Cohere 原始响应校验）与 `d96b9b24`（工具引用空白）两处补修，补跑 rerank/tool/Canvas/reasoning 六文件 419 项，零失败/skip。`integration/supplement-validation.json` 保存最终源码 SHA、命令、退出码、hash、AST 和复用依据；没有把 ce86418e 的 988 项表述为最终 SHA 上重新全跑。SQL/HTTP/Web 相关代码、Jina/HF/Qwen/Base 及其他 ComponentBase 方法未变，故复用其运行证据。

只复用输入充分且未变的证据：V 的 GraphRAG、beta/session、Milvus deadline 原组合不因新 Go/文档 SHA 重跑；A/B/D 的真实交集及最后 Web SHA 重新运行。保留原始失败、修复和显式选择，未用放宽断言、转 skip 或吞异常换绿。

Web `eed958f` 的局部 ESLint、生产 build、文件体积/清单、18 个设置组件与 11 个表单测试通过。真实 HTTP 保存、重载、键盘和中英文明暗主题浏览器验收通过；集成 owner 一次审阅标注文件名/路由/1440×1000 的四格 contact sheet，无视觉阻断。`eed958f` 另修复超过 200 列、滚动后 manual→auto→manual 的空白首屏，保留红/绿用例；最新后端/Web 配对浏览器验收已通过，四格拼图重新一起审阅。浏览器验收的列发现由 UI fixture 提供，独立真实任务集成另证明解析→存储→筛选；未伪造业务响应。

sandbox 六组 mirror0/1 实际原生 linux/arm64 构建与六次离线镜像读回通过。分析器 17 新测试、7 既有测试、3 bundle 测试及 15 个实际 /run ASGI 场景通过；13 拒绝场景未调用执行/分配边界，队列读回不变。

## 限制

- 未 push、部署、执行生产迁移或切服务；表格新迁移为 `e7a9b1c3d5f0`，只在 scratch 升降级验证。
- 未运行完整 `make verify` / 完整 core 或远程 `ci-required`；局部通过不代表全库门禁，consumer pin/CI 配置提交不代表远程已执行。
- 未验证真实云模型/本地模型服务器的可用性、质量和成本；受控 HTTP/SDK、SQL、真实浏览器与生产行为分开记录。
- ES/Infinity/OceanBase 表格分支仅源/单测覆盖；元数据保存失败可保留已插入 chunk 并使任务未完成，不宣称跨存储原子回滚。
- Go 使用真实 Darwin 原生库，未做 Linux 原生验收；sandbox 镜像只验证 arm64，ASGI admission 使用受控执行边界，不证明 gVisor 运行隔离。
- V 验证构造选择与 LLMBundle 调用，未覆盖完整 Agent 图或 HTML 生成流程。图片 token 与消息封装成本仍由 provider 计算。

## 提交与清理

所有源、测试和消费者 pin 已串行合入实际 main/master；本报告及 PROGRESS 的最小索引单独提交。各线接受/清理记录见 `handoffs/*-accepted.json`、`handoffs/*-cleanup.json`，每项清理均在本线全部接受后执行。

根 Python 组合的只读资源复核 `integration/python-resource-readback.json` 枚举 3 个 scratch 库、30 个集合、13 个桶、5 个 Redis 键、2 个缓存前缀和 14 个 listener，均已不存在；Go 四个库另有逐个删除读回。没有 global prune/down 或重启既有服务。

本批工作树在无活动 runner、无独有 WIP、证据外置后由各 owner 归档/移除。各分支在主干包含或 patch 等价、无活动工作树引用、可恢复 bundle 验证后删除；集成工作树在文档合 main 后归档。其他任务的工作树/分支/服务均保留。实际最终状态由下面的外置完成记录读回。

最终主干 SHA 与清理读回在外置 `delivery-complete.json` / `cleanup.json` 记录。本报告中的源码组合 SHA 可重现验证输入；文档提交不会被冒充为运行时版本。
