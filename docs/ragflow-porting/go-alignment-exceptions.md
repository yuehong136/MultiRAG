# Go 对齐例外记录

本文件记录我方代码中针对具体缺陷的修复和提前采用的两项后续改进。完整逐函数对照、旧测试
去向、原始失败和输入哈希保存在专项本地证据目录；主记录见[Go 结构与合同收敛](go-alignment.md)。
所有修复沿用对应原文件和主要函数，不恢复已删除的兼容门面。

## 具体问题修复

远程 CI 跟进时，真实 Python 消费者还暴露了 `internal/cli/parser.go` 的一处明确缺陷：
严格尾部校验错误拒绝了 filesystem 命令的合法参数，且既有安装/卸载处理函数未接入分派。
在原 `parseFileSystemCommand` 消费完整参数，并由原 `isCECommand` 接通当前命令；
保留 SQL 尾部、分号、NUL、未闭合引号和 admin 模式拒绝，不恢复自定义 `skill-core` 或 `skills` 命令。
`internal/cli/filesystem_parser_test.go` 与真实 CLI 消费者覆盖该修复。

| 位置 | 问题与最小修复 | 验证 |
| --- | --- | --- |
| `internal/entity/base.go` | JSON 扫描丢失大整数、吞坏数据；保留数字精度，拒绝非法与尾随 JSON。时间戳尊重显式创建值，保存时更新修改时间。 | 真实 SQL 扫描、保存与失败读回。 |
| `internal/dao/{user,kb}.go`、`internal/admin/service.go` | 活动用户/租户/成员状态及私有 KB 权限缺失；原查询先验证身份和资格，管理员初始化写入有效租户状态。 | MySQL 正向访问、失效/邀请/外租户拒绝和无副作用。 |
| `internal/service/user.go`、`internal/entity/tenant_llm.go`、`internal/service/model_service.go` | RSA 解密失败曾返回成功密码字符串，合法零预算又被业务层和 GORM 默认值改成 8192；原注册函数报错且不落库，精确保留目录零值。原 AddModel 在写入前拒绝负预算。 | 真实 RSA/MySQL、注册末步失败回滚、独立零值读回、实际 ASR HTTP、禁用与非法请求零副作用。 |
| `internal/ingestion/ingestion_service.go`、`internal/engine/nats/` | 未实现处理曾报告成功，提交失败可能 ACK；真实失败状态持久化后 ACK，SQL 失败 NAK，并修正配置、取消和关闭生命周期。 | 真 NATS 重投、SQL 独立读回及进程停止。 |
| `internal/cpp/rag_analyzer.cpp`、`internal/cpp/opencc/dictionary/text.c` | native 字符串终止、计数、关闭和排序边界可能越界；在原 C/C++ 函数内修正，词形分析使用局部小写缓冲。 | 对应源码重建、真实字典、ASan/UBSan。 |
| `internal/engine/elasticsearch/{chunk,metadata}.go` | typed 条件被丢弃、单 ID 更新忽略额外条件、KB 范围不足、物理 ID 跨 KB 冲突、部分写失败被吞；原查询保留全部合法谓词并检查实际完成状态。元数据保持字面量与原合并路线。 | ES 实际读写、非匹配数字条件零修改、邻居 KB 不变和失败读回。 |
| `internal/engine/infinity/{chunk,metadata,common}.go` | SDK 字符串解析无法正确还原 SQL 双单引号；可表示的引号值使用对应字面量，两种引号同时存在的写条件明确失败。原读取/更新/删除保留范围、零值、错误与数据精度，不伪装查询失败为成功。 | 真 Infinity quoted ID、更新/删除/元数据、解析及无副作用回归；SDK 原接口仍不提供调用中取消。 |
| `internal/common/metadata_utils.go`、`internal/service/metadata_filter.go` | 空选择、非法谓词或大整数可扩大检索范围；原函数保留空匹配、精度和模型错误，并约束自动生成的条件。 | 实际 service 消费、空结果及精确数值断言。 |
| `internal/utility/{ssrf,mcp_client,smtp}.go`、`internal/utility/oauth/` | DNS/重定向、会话/分页、坏帧、取消及错误处理不完整；原请求链固定目标、限制跨来源凭据发送、校验响应并关闭生命周期。 | 实际 TCP/HTTP/SSE/SMTP、取消、错误和独立关闭读回。 |
| `internal/common/logger.go`、`internal/engine/redis/` | 冷启动日志目录及重试/关闭/配置快照存在缺陷；原入口修复并保持健康检查只读。 | 冷启动、race、失败后重试与只读键空间。 |
| `internal/entity/models/mineru*.go` | 任务 ID、状态、返回 URL 和文档响应可误判；原方法验证输入与响应，未完成任务不返回文档。Local 原函数随机取一个文件造成丢数，改为稳定返回全部文档段。 | 真实受控 HTTP/multipart、取消、坏任务与多文件回归。 |
| `internal/entity/models/replicate.go` | 版本名、预测状态、凭据目标、终态及向量校验有误；原函数发送实际版本 ID，约束 polling/stream 来源、传递取消，并校验 SSE done 和向量维度。 | 预测轮询、受控 HTTP/SSE、外来源零请求、取消关闭和向量回归；协议依据为 [Replicate 流文档](https://replicate.com/docs/topics/predictions/streaming)。 |
| `internal/entity/models/{volcengine,xiaomi,xunfei,zhipu-ai}.go` | 坏 embedding/错误信封、残缺流及缺失端点可能伪成功；原函数校验实际响应与完成状态。Xiaomi 仅验证配置的连接检查改为明确未支持。 | typed inventory、受控 HTTP、stream/sender/cancel、维度和失败回归。 |
| `build.sh` | 一侧服务退出后另一侧未停止、退出码被掩盖；原 run 分支停止并回收 peer，传递首个退出码。 | 两种真实进程先退出场景与端口回收。 |
| `internal/engine/elasticsearch/{chunk,common}.go` | 实际 KNN 暴露非模板维度未安装；原建库方法安装请求维度并检查 mapping 确认。Skills 独立索引曾被当成共享 KB 行清理，原 drop 验证具体索引名且要求物理删除确认；原 query_string 使用实际索引的 `*_tks` 字段且不修改调用者字段切片。 | 真 ES 新建/已有索引 KNN、drop 确认和 Skills 全闭环。 |
| `internal/deepdoc/{client,dla}.go` | 坏框、失败及取消曾变成成功空检测；原请求有界读取、校验框和 HTTP 状态、拒绝重定向并传播错误。原 `predictOne` 增加 error 返回，直接迁移原 DLA 调用方；成功结果仍为原扁平列表，合法空检测仍保留零值占位。 | 原包全部 race；multipart、重试、取消、坏框、超限和无远程 OCR/TSR。 |
| `internal/server/{config,variable}.go` | 配置打印可能暴露密钥，签名 key 竞争或读回失败可能使用未持久化值；原入口只输出非敏感配置，SetNX 失败必须读回真实 key，空 key 或读回失败报错。 | 冷启动、共享签名 key 和真实鉴权。 |
| `internal/common/{reference_metadata,kg_query_rewrite,json_types}.go`、`internal/utility/{convert,otp}.go` | 类型化空列表、精确数字或损坏 JSON/恢复证明可能被误读；原函数保留合法空值与 `json.Number`，拒绝非法/重复响应和无效 hash/salt，不增加查询门面。 | 真实原消费者、零值/大整数、解析和恢复失败回归。 |
| `internal/ingestion/chunk_engine.go`、`internal/ingestion/chunk/` | 无效表达式、nil、Unicode 切块和共享编译计划可 panic、丢条件或产生 race；原 Compile/Execute/operator 校验并返回错误。`applyOverlap`、`resolveOverlapConfig` 增加 error 返回，原调用方直接迁移；重复 stage 和声明顺序保留。 | 原表达式/operator/并发复用、实际 CLI CHUNK。 |
| `cmd/`、`build.sh`、`internal/cpp/CMakeLists.txt`、`go.mod` | 冻结构建遗漏 ingestor/native 链接或间接依赖；原构建路线补齐四入口、源码一致的 Office/PCRE2/平台链接和模块图。启动与 CLI 参数失败传递非零退出。 | 四入口 build/vet、真实 native CLI、冷启动与首退出回收。 |
| `configs/go/`、`.gitignore` | 冻结树包含共用私钥；不分发该密钥对，Go PEM 由本机提供并忽略，验收仅生成独有临时 key。 | 真实 RSA 登录、注册与独立临时资源清理。 |

## 可观测性配置

`internal/observability/otel/provider.go` 的原 `NewTracerProvider` 拒绝 NaN、无穷大及大于 1 的采样值；原负值禁用与零值 no-export 路线保留。原 `buildResource` 为稳定 service 属性使用无 schema 的增量资源，与锁定 SDK 默认资源合并，修复启用 tracing 时 1.26/1.41 schema 冲突。保持原 `WithEndpoint(host:port)` 和独立 Insecure 配置，不恢复旧 HTTPS-only endpoint 门面；原 OnError 保留实际异常消息。实际受控 OTLP/HTTP 输出、无导出默认值和错误 span 均执行验证。

## Agent、工具、DAO 与管理端

以下条目保持原目录、文件和函数。记录 ID 对应专项逐条账本；为修复具体错误而改变的内部
签名直接迁移所有调用者，不保留旧签名包装。

| 记录 | 原位置与明确问题修复 |
| --- | --- |
| AGT-01–04、12、13 | `service/agent.go`、`handler/agent.go`、`agent/canvas/` 和原组件的 echo、占位执行、缺失 factory、虚假下载 URL 曾报告成功。保留真实 CRUD、Loop、标量表单、获取内容、HTML/DOCX 和真实 LLM；未接通的公开 Run/Chat/Rerun、通用循环/条件路由、文件表单、PDF 字体及其他请求能力明确报错，在工厂或副作用前拒绝。不把库级 checkpoint/Loop 测试称为公开 Agent 执行证明。 |
| AGT-05、06、07 | 原 VariableAssigner/List/Aggregator/Excel、CanvasState、Parallel 的数字丢精度、负索引、共享快照、取消和重复 resume 缺陷。原 `toFloat64`、`toFloat64OK`、`toInt` 与 `operate` 保留位置，必要返回值改为精确数/错误；JSON 大整数与有限十进制精确保留，普通有限浮点及非终止除法保持原运算语义，拒绝真正的非有限值、除零和下溢。删除另拆的数字转换/运算 helper。原 state 增加 JSON 读写方法及递归克隆，以保留取消与独立快照。 |
| AGT-08 | 原 `agent/tool/exesql.go` 传播真实 SQL 错误，使用只读事务阻止查询夹带写入，正确处理引号、DSN 和精确扫描。原 `exesqlExecute`、`exesqlQueryOne` 接收 `*sql.Tx`，新增 `scanSQL` 仅处理原查询结果的动态列与数字；没有新执行入口。 |
| AGT-09–11 | 原 HTTP、SSRF、Email、Crawler 和各工具请求曾忽略取消、重复 POST、绕过重定向检查或把坏信封当成功。原 `ResolveAndValidate` 增加 ctx，原请求固定地址、有界读取、校验响应并脱敏；原 SMTP socket 由上下文关闭并拒绝 CRLF。修正 DuckDuckGo 原 JSON tag 和 Tushare 原 `Fields string` wire。新增 `decodeToolJSON`、`redactHTTPError` 只承担这些已证实的响应/错误校验。 |
| AGT-14–18 | 原 Agent 创建、session、取消和 DAO 修复默认值时序、非法 DSL、显式空覆盖、重复 close、版本归属、精确引用、日期末秒、Unicode tags、坏日志假成功及 orderby 注入；保留目标原 session 字段与 tag 语义。日志缺 key 可为空，Redis 错误或坏 JSON 必须失败。 |
| AGT-19 | 原 `dao/api_token.go::GetUserByAPIToken` 对重复 token 曾任取身份；要求唯一真实绑定，歧义拒绝，未新增身份兼容层。 |
| HANDLER-01、02 | 原 chat/session 先做身份、成员、session 与模型授权；request ctx 进入供应商，消息以 CAS 更新且 SQL/编码失败不发成功终帧。原 CompletionRequest 可选数值改指针，保留显式零、false、空字符串与精确保存值。 |
| HANDLER-03、05 | 原管理 runtime 选择先检查活动 SQL owner/admin 归属再写 Redis；profile 删除密码 hash 序列化。缺 selector 保持原 HTTP 200 非零业务码，不增加新路由或中间件。 |
| HANDLER-04 | 原 Ask 字符串 channel 无法传递晚到错误；同一管道改用已有 `AskDelta`，请求取消进入原 LLM，DONE 不当内容，只有真实 Final 才发终态。沿用 Search 已授权租户解析共享模型；不增加流类型或兼容 sender。 |
| HANDLER-06 | 六次同名实例并发请求曾产生六行；在原 CreateProviderInstance 事务锁活动 Tenant/Provider 后查重插入，不新增表或墓碑协议。 |
| DAO-01–03 | 原 ingestion 事务不再吞 query/commit、遗漏 rollback 或并发状态变更；持久化 JSON checkpoint 保留实际字符串列表，session 分页拒绝整数溢出。DAO 返回删除计划不等于已完成对象清理。 |
| DAO-04、05 | 原活动成员查询排除 invite/撤销，保留 owner/normal/admin；原设置前缀查询转义 `%`、`_`、`!`，不改变其他列表的原 SQL LIKE 语义。 |
| DAO-06 | 原 MySQL migration 将 INFORMATION_SCHEMA 限在当前库、传播 DDL 错误、在原 ALTER 中处理 composite primary 和安全标识符。MySQL DDL 隐式提交；只证明单项成功/失败读回，不声称跨 DDL 原子回滚。 |
| ADM-01、02、03、08 | 原 admin 中间件拒绝后 Abort；原注册/bootstrap 全部 SQL 初始化事务化且失败回滚；原删除锁 Tenant，完整清理其 SQL 凭据/映射并保留外部共享组，失败返回错误。对象与未实现元数据删除不伪称完成。 |
| ADM-04、07 | 原 heartbeat/store 使用原子计数、本地接收时间和独立快照；原 ingestion task 精确解析 checkpoint，坏日志/query 失败只写一次错误响应。 |
| ADM-05、06 | 未实现角色、sandbox、关闭等原方法不再返回虚假成功。原设置 DTO 的 `VarValue *string` 区分缺失/null 与合法空字符串；其他路由和字段保持原合同。 |

新增 `excelNumericCell`、`validateNumericTree` 分别守住原电子表格数值与嵌套状态校验；
`cloneValue` 只用于原状态边界；原 `StringTransformComponent.doMerge` 返回 error，避免吞掉
数值/合并失败。上述必要辅助均有具体反例，不是按功能重拆目录或引入新实现路线。
`NatsEngine.Close` 和原 `MessageQueue.Close` 是真实连接回收所需的唯一队列生命周期补充。

冻结实现中尚未完成的 Agent、任务或媒体能力保持明确错误；字符串 sender 无法无损表示的
二进制音频不会发明 base64 wire 协议。

## 模型驱动和调用服务的具体问题修复

对照基准为冻结目标 `fcebcebe1e0e798b42c18e5593747cd20eb4c757`。以下分组覆盖本轮模型例外记录的 42 个基础 ID，另列真实消费者验收追加的 2 项缺陷。同一问题涉及多个供应商时，分别修改其原函数，不新增统一驱动、能力门面或兼容入口。本轮没有提前采用后续上游提交的模型架构改进。

### 请求取消、HTTP 路由和实例配置

**记录：** `MODEL-CANCEL-01`、`MODEL-TRANSPORT-01`、`MODEL-CONSTRUCTOR-01`。

**源文件和原函数：** `internal/entity/models/types.go` 的 `APIConfig`，`base_model.go` 的 `NewDriverHTTPClient`、`GetBaseURL`，以及接收 `baseURL map[string]string` 的 61 个原构造器和各驱动原请求方法。构造器文件清单见文末。

**明确缺陷：** 原请求脱离调用方取消；HTTP 构造器丢失自定义 transport 路由，默认重定向还可能把凭据发送到另一端口；构造器直接保存调用者的 URL map，使后续 map 修改改变既有实例的目标。

**最小修复：** `APIConfig` 增加调用方 `Context`，原请求方法通过新增的 `RequestContext` 取上下文。原 HTTP 构造器克隆标准 transport、保留自定义 transport、拒绝自动重定向；nil 或 typed-nil 回退仍保留拨号、TLS 和响应头超时。`GetBaseURL` 保留原 region/显式 endpoint 选择顺序，并拒绝非法 scheme、空 host、userinfo、fragment 等无效地址。每个原构造器就地复制 URL map；`NewInstance` 仍走其原构造器。

### 类型化目录的归属和可变数据隔离

**记录：** `MODEL-CLASS-01`、`MODEL-CATALOG-COPY-01`。

**源文件和原函数：** `internal/entity/models/model.go::InitProviderManager`、`base_model.go::ParseListModel`。

**明确缺陷：** 目录初始化把 provider 名称当作模型 class，覆盖显式模型 class，且 null/未命名模型可导致 panic；目录投影还共享全局切片和指针，调用者修改结果会污染后续查询。

**最小修复：** 在原初始化循环按显式模型 class、provider class、原模型名推导的顺序赋值，并拒绝无效模型记录；在原 `ParseListModel` 复制元数据的切片和指针值。保留目标原有的 ID trim、空 ID 过滤、`owned_by` 后缀、重复项数量和空结果可为 nil 的行为。

### 可选 ChatConfig 的 nil 解引用

**记录：** `MODEL-NIL-CONFIG-01`。

**源文件和原函数：** `internal/entity/models/{aliyun,gitee,siliconflow,nvidia,volcengine,zhipu-ai,vllm,huggingface}.go` 的原 `ChatStreamlyWithSender`。

**明确缺陷：** 合法的 nil 可选配置在读取字段时 panic。

**最小修复：** 各原方法就地使用空配置，保留原字段省略和默认值；不新增统一请求配置层。

### 数值结果不能把缺失值伪造成零

**记录：** `MODEL-JSON-01`、`MODEL-NUMERIC-RAW-02`。

**源文件和原函数：** `internal/entity/models/types.go` 的 `EmbeddingData`、`RerankResult`；`{togetherai,xinference,n1n,novita,baidu,aliyun,zhipu-ai,gpustack,azure_openai,localai,hunyuan,gitee,cohere,voyage,mistral,jina,vllm,nvidia,siliconflow,baichuan}.go` 中实际提供能力的原 `Embed`、`Rerank`，以及相应原 `OCRFile` 输入校验；`huaweicloud.go` 的原嵌入响应类型和处理路径。

**明确缺陷：** 普通 JSON 解码把缺失/null 的 index、坐标或分数变成合法零；行数、索引和维度错误可能产生部分结果或错误关联，nil 嵌入配置还可能 panic。

**最小修复：** 为原有两个结果类型增加 `UnmarshalJSON` 方法，区分缺失/null 与显式零；在各原方法校验其实际协议的数量、重复/范围、维度和有限数值。Huawei 原响应的 `Data` 改用已有严格 `EmbeddingData`。保留各供应商原字段、单次请求数量、响应顺序和截断规则，不恢复统一分批或排序层。

### Eino 原入口的选项和关闭行为

**记录：** `MODEL-EINO-01`。

**源文件和原函数：** `internal/entity/models/llm.go` 的 `EinoChatModel.Generate`、`Stream`、`WithTools` 和 `fromInternalResponse`。

**明确缺陷：** 原桥接静默忽略调用选项和工具，丢弃 reasoning，把 `[DONE]` 当内容输出，reader 关闭后等待首字节的供应商请求仍可能存活。

**最小修复：** 在原 `Generate`/`Stream` 内复制请求配置、限制已授权模型、在 I/O 前明确拒绝当前桥接无法表达的工具调用；原响应转换保留 reasoning，原流过滤终止标记并在 reader 关闭时取消请求。选项处理已内联回两个原入口，新增过的 `requestConfig` 私有抽取函数已删除。

### 已授权模型解析和内部槽位

**记录：** `MODEL-SERVICE-BINDING-01`、`MODEL-SERVICE-CANCEL-01`、`MODEL-SLOT-VOCABULARY-01`。

**源文件和原函数：** `internal/service/model_service.go` 的原解析器、`getModelConfig`、`GetEmbeddingModel`、`GetChatModel`、`GetRerankModel`、`GetModelConfigFromProviderInstance` 及各模型操作；`internal/handler/providers.go` 的原调用入口。

**明确缺陷：** UUID 请求仍解引用未给出的名称/provider 指针；解析后继续使用未解析的配置；外租户、停用模型、错误能力或 SQL 失败可能错误选择凭据和 endpoint；合法登记的自定义模型在静态目录缺失时无法使用。公开 discovery、balance、probe、task 服务方法不能接收请求取消。

**最小修复：** 在原解析器和操作中使用已授权的名称、状态、能力及实例配置，检查 SQL 错误，按原 `NewInstance` 绑定 endpoint。仅在活动且已授权的已保存模型缺少静态目录项时，由其原 `Extra`、`ModelType` 和目标 class 推导组成 `ModelInfo`；不存在的保存记录仍失败。六个原服务方法增加首参 `context.Context`，调用方直接迁移，不保留旧签名包装。内部 `speech2text`/`image2text` 槽位只在原解析处映射为目录的 `asr`/`vision`，公共槽位枚举不另改名。

### 租户删除、重复登记和批量写入边界

**记录：** `MODEL-TENANT-LIFECYCLE-01`、`MODEL-HANDLER-REQUEST-01`。

**源文件和原函数：** `internal/service/model_service.go` 的 `AddModelProvider`、`CreateProviderInstance`、`AddModel`；`internal/handler/providers.go::prepareAddModelRequest`。

**明确缺陷：** 过期归属可能在租户删除后重建 provider/凭据；先查重后插入允许并发重复实例和模型；nil/空白模型、未知能力或负 `MaxTokens` 可进入存储。

**最小修复：** 在各原事务先锁活动 Tenant，再按原关系锁 Provider/Instance，把查重和插入放在同一事务；失败返回错误且不留部分批次。原 handler helper 拒绝 nil/空白名称；原 `AddModel` 在任何 DAO 前检查全部 `Models[]` 的八种目录能力和 `MaxTokens < 0`，保留合法 `0`、`false`。继续按冻结目标只保存首个主能力，不恢复旧多能力归一化结构。`prepareProviderInstance` 和 `prepareAddModelRequest` 都是冻结目标已有函数，不是本次新增拆分；没有新增表、墓碑协议或兼容注册桥接。

### ASR 上传内容与服务器文件边界

**记录：** `MODEL-ASR-PAYLOAD-01`。

**源文件和原函数：** `internal/service/model_service.go::TranscribeAudio`、`TranscribeAudioStream`。

**明确缺陷：** REST `file` 内容被当作服务器路径，既不能读取远端 CLI 的上传内容，也允许请求服务器文件。

**最小修复：** 先完成模型授权和能力检查，再在两个原方法内严格解码非空、最多 32 MiB 的 base64；按实际 MIME 创建 0600 临时文件，检查写入/关闭，并在退出时清理。新增过的 `createASRAudioFile` 抽取函数已删除；不保留路径回退。

### SSE 分帧、完成和取消

**记录：** `MODEL-SSE-MULTILINE-01`、`MODEL-IDLE-LIFECYCLE-01`、`MODEL-SSE-COMPLETION-02`。

**源文件和原函数：** `internal/entity/models/base_model.go::ParseSSEStream`；`{modelscope,localai,xinference}.go::ChatStreamlyWithSender`；`{cohere,novita,longcat,nvidia,huggingface,mistral}.go` 的原聊天方法和 `novitaThinkExtractor.Feed`。

**明确缺陷：** 合法多行 SSE JSON 被逐行误解码；不完整、只有 reasoning 或错误终止的流可发成功 DONE；原 idle goroutine 读取后来被修改的超时全局值产生 race，并把调用方 deadline 改写成 idle 错误。Novita 任意保留字节尾部切断 UTF-8，Mistral 流忽略其已有结构化 content。

**最小修复：** 原 SSE helper 保持单行立即处理，只为未完成的 JSON 累积到事件边界，保留原 1 MiB 上限并在不完整 EOF 时失败。原 idle goroutine 启动前捕获超时值，保留 context 错误身份。各原聊天方法要求真实内容和合法完成，沿用原 sender 顺序；Novita 仅保留实际标记前缀的尾部，Mistral 复用原内容提取函数。不恢复删除过的通用 idle watchdog 或 SSE 门面。

### HuaweiCloud、ModelScope、OrcaRouter、Qiniu、N1N 和 GPUStack

**记录：** `MODEL-AFTER0FB-01`、`MODEL-AFTER2836-01`。

**源文件和原函数：** `internal/entity/models/{huaweicloud,modelscope,orcarouter,qiniu,n1n,gpustack,azure_openai}.go` 的原聊天、目录、数值/媒体方法，以及 `AzureOpenAIModel.deploymentURL`。

**明确缺陷：** 缺字段、空/截断流可报告成功；错误正文泄漏供应商内容；ModelScope 改写调用方取消；OrcaRouter GET 携带 JSON body，且空/错误 JSON 可被当成音频；Azure deployment 名称中的分隔符能改变路由身份。

**最小修复：** 在每个原方法按其协议校验完成和错误，保留真实 sender/context 错误；GET 无 body，原数值/媒体处理拒绝伪成功。Azure 仅对 deployment 身份做 `PathEscape`，不改写配置 suffix。保留 Orca 原无 reasoning 聊天、原重排前缀顺序和各原能力边界。

### Google SDK 原请求边界

**记录：** `MODEL-GOOGLE-01`。

**源文件和原函数：** `internal/entity/models/google.go` 的原聊天、嵌入和 `collectGoogleModelNames`，以及新增的 `googleResponseTransport.RoundTrip`。

**明确缺陷：** 锁定的 `google.golang.org/genai v1.54.0` 在 null JSON 响应处 panic，并丢失 null 向量坐标身份；原消息/选项转换丢失或误判内容，重复分页 token 造成无终止循环。

**最小修复：** 只在原 SDK HTTPClient 的 transport 边界增加一个类型及其 `RoundTrip` 方法，在 SDK 解码前校验原始非流响应。原方法修复角色、content、选项转换和严格向量校验；原目录循环拒绝分页 token 环。保留 SDK 路线、调用方 context 和脱敏错误，不改成自建 Google 客户端。

### Bedrock 签名、数值和 eventstream

**记录：** `MODEL-BEDROCK-01`。

**源文件和原符号：** `internal/entity/models/bedrock.go` 的原常量 `bedrockRuntimeService`，原 `bedrockRuntimeURL`、region/key/STS 处理、聊天、eventstream 解码、嵌入和目录函数。

**明确缺陷：** runtime 签名 scope 使用 `bedrock-runtime`，与 AWS 的 signing name `bedrock` 不符；模型 ID 未转义；缺失数值被当零；收到 `messageStop` 就返回，漏掉后续异常、CRC 错误或未完成内容块。

**最小修复：** 修改原常量和函数，在原解码路径验证数值、维度、完整结束与控制目录。保留默认凭据链/STS 及原 Cohere 批次请求方式；不增加旧 96 条拆批、Query 或静态 session-token API。超时由调用方 context 约束。

### Ollama 原生协议

**记录：** `MODEL-OLLAMA-01`。

**源文件和原函数：** `internal/entity/models/ollama.go::contentToText`、原聊天、嵌入和目录方法。

**明确缺陷：** 丢弃后续文本/图片片段，把生成选项放在错误层级，忽略显式 `think=false`，nil 配置/无效目录 panic，空或截断 NDJSON 仍成功。

**最小修复：** 原循环保留全部文本和内联 base64 图片，明确拒绝不支持的远程图片；使用原生 `options` 和 thinking 布尔值，校验 NDJSON 结束、目录、向量及取消，GET 不携带 body。保留原生 `embeddings` 响应和原路线，不增加统一消息转换层。

### OCR 下载与多页内容

**记录：** `MODEL-PADDLE-01`、`MODEL-OCR-MULTIPAGE-01`。

**源文件和原函数：** `internal/entity/models/{paddleocr,baidu,mistral}.go::OCRFile`，以及原 `paddleJsonlLine` 类型。

**明确缺陷：** Paddle 结果下载可能通过重定向外送凭据或访问私有地址，忽略错误 JSONL/缺失文本；Baidu 的 URL query 干扰 PDF 判断且丢弃后续页；Mistral 缺失/重复页号也可当成成功。

**最小修复：** 在原单个 `OCRFile` 保留提交、原 3 秒轮询和下载路径，校验任务/状态、下载边界及真实文本。原 Paddle 类型用可空字段区分缺失与空白，并读取原生结果变体；Baidu 用解析后的 URL path 判断 PDF 并保留全部页；Mistral 验证唯一页号后按页序汇总。没有恢复旧 poll/download/jobConfig 分拆文件和函数。

### 302AI、Gitee、MinerU 和 Replicate 任务

**记录：** `MODEL-AI302-01`、`MODEL-GITEE-TASK-01`、`MODEL-MINERU-TASK-02`。

**源文件和原函数：** `internal/entity/models/{302ai,gitee,mineru,mineru_local,replicate}.go` 的原推理、`ParseFile`、`ShowTask`、目录函数和 `ReplicateModel.readPredictionStream`。

**明确缺陷：** 错误/缺失数值或分段被当作结果；任务进度伪造成完成文档；非法任务 ID、版本或结果来源 URL 改变请求目标；MinerULocal 只随机读取一个文件；流取消/错误完成未可靠传播。

**最小修复：** 校验保留在原方法。Gitee 保留单次嵌入、`data` 重排、仅返回 TaskID 的提交以及直接 `task/<id>/get` 路线，验证并排序分段索引。MinerU/302AI 原 DTO 的 `Code` 改为 `*int`、保留错误字段，并在原 `ParseFile`/`ShowTask` 校验；保留可选模型和 URL 优先。MinerULocal 按文件名稳定读取全部结果。Replicate 版本预测仅发送 64 位 hash，检查任务身份/状态、允许的结果及 SSE 来源、取消和完整结束，校验向量；原流方法追加 ctx。

新增过的两个 MinerU `UnmarshalJSON` hook 已删除，未保留轮询门面、旧 parser 名称、自适应 API 前缀、统一媒体 flatten 或凭据外送兼容行为。

### 余额和目录响应的真实字段

**记录：** `MODEL-NOVITA-BALANCE-01`、`MODEL-SILICON-BALANCE-01`、`MODEL-FISH-CATALOG-01`。

**源文件和原函数：** `internal/entity/models/novita.go::Balance`、`siliconflow.go` 和 `fishaudio.go` 的原 `Balance`/`ListModels`。

**明确缺陷：** GET 带 body；缺失/null/非数值/非有限金额、矛盾业务状态或错误信封被当成成功，目录缺失字段伪装为空成功，错误正文外泄。

**最小修复：** 各原方法验证自身响应字段，限制读取并保留取消。Novita 保留原数值尺度和浮点值；SiliconFlow 保留原目录投影；FishAudio 保留原完整余额 map、合法零/负 credit 和显式空白 title，不补 `_id` 回退或 currency 别名。

### ASR/TTS 数据存在性和原生音频流

**记录：** `MODEL-MEDIA-PRESENCE-01`、`MODEL-NATIVE-SPEECH-STREAM-01`。

**源文件和原函数：** `internal/entity/models/{cohere,siliconflow,xinference,groq,astraflow,stepfun,fishaudio}.go` 的原 ASR/TTS 方法；StepFun/SiliconFlow 原 `AudioSpeechWithSender`。

**明确缺陷：** 缺失/null 的转录被当作空转录；空数据、JSON 错误或非音频被当成合成音频；坏 base64、无有效帧、超量音频或错误终止被当成成功。

**最小修复：** 原 ASR 私有响应字段保留存在性，明确 `text:""` 仍是合法静音。原 TTS 方法拒绝实际错误/空数据；FishAudio 原 SSE callback 校验 base64、实际帧和 20 MiB 累计量，保留原 EOF 完成。StepFun 校验原 `speech.audio.delta`/终止/错误事件；SiliconFlow 保留原二进制读取循环，拒绝 JSON/text、空音频并限制累计量。保持各供应商原 Params/Format、字段顺序和 sender/context 错误，不增加通用参数白名单或任意二进制转 `*string` 的承诺。

### Azure 过滤注解

**记录：** `MODEL-AZURE-FILTER-01`。

**源文件和原函数：** `internal/entity/models/azure_openai.go` 的原同步/流式聊天方法。

**明确缺陷：** 合法 prompt/filter 注解被误认为缺 choices；同步或已有部分文本之后的过滤失败仍被当成成功。

**最小修复：** 在原方法识别真实注解/偏移，接受合法纯元数据帧，对过滤或坏字段失败，并要求真实可见内容和成功完成。

### Aliyun 原重排响应字段

**记录：** `MODEL-ALIYUN-RERANK-01`。

**源文件和原函数：** `internal/entity/models/aliyun.go::Rerank`。

**明确缺陷：** 已校验原生 `results` 后又解码到 `data` 标签的类型，合法响应变成空结果；缺失 suffix 仍可能发请求。

**最小修复：** 原函数直接用已校验的行填 `RerankResponse.Data`，缺 suffix 在 I/O 前拒绝；保留原 outbound `top_n`、供应商顺序和显式零。

### vLLM 嵌入 suffix

**记录：** `MODEL-VLLM-SUFFIX-01`。

**源文件和原函数：** `internal/entity/models/vllm.go::Embed`。

**明确缺陷：** 非空嵌入请求缺 suffix 时仍访问 `/api/`，随后才报响应错误。

**最小修复：** 原 `Embed` 在供应商 I/O 前拒绝缺失 suffix；保留原 region、endpoint 和单次请求。

### VolcEngine、Xiaomi、XunFei 和 Zhipu

**记录：** `MODEL-ROOT-TAIL-FOUR-01`。

**源文件和原函数：** `internal/entity/models/volcengine.go` 的 `VolcEngine.Embed`/`ListModels`，`xiaomi.go` 的 `XiaomiModel.ChatStreamlyWithSender`/`CheckConnection`，`xunfei.go` 的原同步/流式聊天，`zhipu-ai.go` 的 `ZhipuAIModel.Rerank`/`ListModels`。

**明确缺陷：** 缺失/空/null 向量或错误目录可成功；Xiaomi 流不完整且未请求供应商的 probe 伪成功；XunFei 非整数/缺失 code 可绕过失败；Zhipu 缺重排 suffix。

**最小修复：** 原方法校验向量/维度/错误、流内容与终止、整数 `code=0` 和所需 suffix；Xiaomi probe 明确 unsupported。保留 Volc 同步 suffix、流硬编码 `chat/completions`、同步 content-before-reason 和 raw key；Xiaomi 原 URL/region/Stream 选项与媒体能力；XunFei HTTP 目录、必需 key 和 unsupported probe；Zhipu 原 key、保留参数拒绝及字面路径。接收者实际名为 `VolcEngine`、`ZhipuAIModel`，不引入 `VolcEngineModel`/`ZhipuModel` 别名。

### Baidu 仅 reasoning 流

**记录：** `MODEL-BAIDU-STREAM-01`。

**源文件和原函数：** `internal/entity/models/baidu.go::ChatStreamlyWithSender`。

**明确缺陷：** 只有 reasoning 时仍能发成功 DONE，截断错误丢失 `io.ErrUnexpectedEOF` 身份。

**最小修复：** 原函数仅由非空 content 设置成功内容标志，并保留 EOF 错误包装。同步 reasoning 仍只在显式 Thinkingtrue 时读取，保留原 URL/suffix、重排供应商顺序、`TopN=99` 原样发送和 `TopN=0` 按输入数量处理。

### CoHere 原 callback 和原生嵌入

**记录：** `TAIL-COHERE-01`。

**源文件和原函数：** `internal/entity/models/cohere.go` 的原同步/流式聊天、`Embed`、`ListModels`。

**明确缺陷：** 只有终止帧可伪造成功；可见答案之后的坏 callback 或 reasoning/content 之间的取消仍继续发内容；原生向量/目录缺失可成功。

**最小修复：** 原 callback 校验字段类型、真实内容和终止，在发送之间检查 ctx；原嵌入校验原生隐式索引向量和维度，原目录校验存在性。保留同步 `top_p`、流 `p` 与 `top_p`、目录第一页和 `@cohere` 命名；不自加分页。

### Upstage 请求维度与原始模型名

**记录：** `TAIL-UPSTAGE-01`。

**源文件和原函数：** `internal/entity/models/upstage.go::UpstageModel.Embed`。

**明确缺陷：** 缺失/null 索引或坐标，以及输出维度不一致/不符请求可被接受。

**最小修复：** 原 `Embed` 使用已有严格结果类型检查数量、索引、填充完整性、维度和负配置，保留原结果重排和错误行为；不增加 dimensions wire 字段或 passage/query 模型名重写。

### DeepInfra 原始字段和媒体路径

**记录：** `MODEL-DEEPINFRA-TAIL-01`。

**源文件和原函数：** `internal/entity/models/deepinfra.go` 的原嵌入、重排、ASR/TTS、目录和余额方法。

**明确缺陷：** 请求维度 1–31 被静默忽略；缺失/错误字段、null 目录/转录/余额可成功；namespaced reranker 或 voice 可改写路径；错误 MIME、无数据或不可表示的音频被送入文本 sender。

**最小修复：** 原方法明确拒绝不能发送的维度，保留 `0` 省略和 `>=32` 发送；限制读取、验证实际响应/数值，阻止路径注入，保留原 voice_id 优先、同步音频字节和可表示 UTF-8 的流。合法空转录、负债、原 `currence:USD` 键和原 `ListModels` 连接检查保留；不补 currency 别名或额外分批。

### CometAPI 原额度响应

**记录：** `MODEL-COMETAPI-TAIL-01`。

**源文件和原函数：** `internal/entity/models/cometapi.go::doCometAPIRequest`、`Balance`、`ListModels`。

**明确缺陷：** 缺失/null/非数值/非有限或负总额度、错误可选计数和业务错误可成功；无效余额 URL 或错误正文可能泄漏凭据/供应商内容。

**最小修复：** 原函数校验原额度 map 和 URL，限制读取并脱敏错误；保留合法零、原 key 查询构造及 `ParseListModel` 的空 ID 过滤、重复和 nil 空结果。

### 消费者验收追加：N1N 目录实例 endpoint

**记录：** `MODEL-N1N-DISCOVERY-ENDPOINT-01`。

**源文件和原函数：** `internal/entity/models/n1n.go::N1NModel.ListModels`。

**明确缺陷：** 真实 HTTP/SQL 消费链把已保存实例 endpoint 放入 `APIConfig.BaseURL`，原 `ListModels` 却通过只接收 region 的 `endpointURL` 丢弃该字段，尝试访问全局目录的 `https://api.n1n.ai/v1/models`。首次回执 `4d5e0788b18640e48fa02c0b7c22599a` 中该尝试被非 loopback transport 规则阻断，没有到达真实供应商；这仍是受控边界偏差，保留原失败。

**最小修复：** 只在原 `ListModels` 用原 `GetBaseURL(apiConfig)` 解析完整配置，再按原 `TrimSuffix`/`TrimLeft` 方式拼接 models suffix。原 `CheckConnection` 继续调用 `ListModels`，原 region helper 和其他推理方法不变。新定向测试实际请求两个显式 endpoint 和一个 regional endpoint，校验各 key、原目录投影、配置不变、无效配置零请求及调用方取消；没有新增 helper、签名或兼容路径。

### 消费者验收追加：VolcEngine 缺失目录

**记录：** `MODEL-VOLC-DISCOVERY-PRESENCE-01`。

**源文件和原函数：** `internal/entity/models/volcengine.go::VolcEngine.ListModels`。

**明确缺陷：** `{}`、`data:null` 或其他缺失目录的成功 HTTP 响应被解析为 nil 目录并报告成功。

**最小修复：** 原 `ListModels` 在既有外层解码处要求 `data` 字段存在且非 null；错误类型由原数组解码拒绝。保留合法 `data:[]`，以及原 `ParseListModel` 的空 ID 过滤、trim、重复和 `owned_by` 后缀。原 models 路径和独立 `CheckConnection` 的 `/files` probe 都保留；`/files` 并不是新增目录 fallback，也不能因初始 fixture 把它误当 models 而删除。

### 保留的结构偏差

模型目录的生产文件集合与冻结目标一致，没有缺失原函数或类型。模型包仅保留以下新增声明及原签名变更；其余修复在原函数中完成。

| 偏差 | 位置 | 必要原因 |
| --- | --- | --- |
| 新增 `(*APIConfig).RequestContext` 方法，原 `APIConfig` 增加 `Context` 字段 | `internal/entity/models/types.go` | 原 `ModelDriver` 签名不携带 ctx，通过配置把实际取消传入原请求方法。 |
| 新增 `(*EmbeddingData).UnmarshalJSON` 和 `(*RerankResult).UnmarshalJSON` 两个方法 | `internal/entity/models/types.go` | JSON 默认零值解码不能区分缺失/null 与合法零；原调用路径直接消费严格类型。 |
| 新增 `googleResponseTransport` 类型和 `RoundTrip` 方法 | `internal/entity/models/google.go` | 在锁定 SDK 解码前拦截会 panic 或丢失 null 身份的原始响应；仍使用原 SDK 请求路线。 |
| 原 `(*ReplicateModel).readPredictionStream` 增加首参 `ctx context.Context` | `internal/entity/models/replicate.go` | 原方法的实际 HTTP 请求必须接受调用方取消；没有新增重载或旧签名包装。 |
| 原 `mineruTaskSubmitResponse`、`mineruTaskQueryResponse` 的 `Code` 改为 `*int` 并增加错误字段 | `internal/entity/models/mineru.go` | 区分缺失/null 和成功 `0`，在原任务方法校验；未保留额外 JSON hook。 |
| 原 `paddleJsonlLine` 补原生结果字段及 nullable 文本 | `internal/entity/models/paddleocr.go` | 区分缺失数据与合法空白，读取实际多种原生 OCR 结果。 |
| 原 `huaweiCloudEmbeddingResponse.Data` 改用已有 `[]EmbeddingData` | `internal/entity/models/huaweicloud.go` | 保留数值存在性，避免原匿名结构把 null 坐标变零。 |

合计为模型包新增 **4 个方法、1 个类型**，修改 **1 个原函数签名、5 个原类型声明**。服务层另有 `ModelProviderService.ListSupportedModels`、`ShowInstanceBalance`、`CheckConnection`、`CheckInstanceConnection`、`ListTasks`、`ShowTask` 六个原方法增加首参 `context.Context`；handler/服务其余原符号和签名保持，调用方已直接迁移。

### 原生语义和已退休合同

修复不把各供应商改成统一策略。原单次请求与明确的输入上限、原 region/endpoint/suffix、凭据要求、Params/Format、`top_n`/`top_k`、结果顺序和条件截断均按各原函数。合法 `0`、`false`、显式空转录、空白 OCR 页，以及原解析器允许的空目录/重复投影不因通用校验而拒绝。

旧通用 32/96 条分批、未知 region 回退、自动 slash 去重、全局模型 thinking 默认值和采样互斥、统一媒体参数白名单、全局排序/TopN 夹取、`BoundModel`/可选能力接口、旧数字 ID 绑定和私有 parser 名称等删除合同，分别在测试迁移记录标明。不能把“删除旧 helper”用于退休仍有效的租户归属、显式选项、endpoint 隔离、取消、数值存在性或零副作用断言。

### 证据边界

模型包整包 `go test -race` 登记回执为 `3ac25f2641a047fda5c5fcd512e70e9d`：867 个顶层测试全部通过，0 fail、0 skip；4515 个 pass 事件含子测试及包完成事件。该回执的运行前后模型、目录配置和依赖输入哈希一致；3919 次监听器清理读回均确认不存在，进程组和运行临时目录也已清理。546 个原模型测试中 535 个迁移、11 个删除的私有/兼容合同明确退休。

随后真实消费者验收追加的 N1N/VolcEngine 修复由 `29ba59f22c2b4fc682a425471015759a` 定向 `go test -race` 补验：4 个顶层测试、25 个测试及子测试，0 fail、0 skip。只修改两个原 `ListModels` 函数和对应测试；前述整包回执不冒充这次增量后的另一次整包执行，未变化部分复用其原输入证据。

消费者锁定组合 `ca53d9d83d7041d5a16bba3e4560a247` 进一步通过 N1N/VolcEngine 双实例的实际签名 HTTP、已保存 SQL endpoint/key、真实 CLI、原生 probe、拒绝及缺失目录用例。首次受控边界阻断和中间 fixture 失败回执仍保留。

服务层的事务和 HTTP/SQL 证据另计：`9874254940ec40dc96df2011772af717` 验证生命周期与 ASR payload，`0774570e3e9744c997a80fdc5eb36203` 验证六并发实例创建仅一个成功且独立 SQL 读回一行，`23c4fe4731cd432da527892328d809ac` 验证批次 SQL 失败回滚及 `0`/`false`/主能力，`88b65940a60e4f439a9468ea549d207a` 验证负预算拒绝和注册/ASR 的合法零值。模型单测不能替代这些服务证据，服务结果也不扩大为未覆盖的实际供应商验收。

历史回执 `7423923bed2440fda375be46d7fc9fda` 有一次 Bedrock 受控 fixture 越界访问 AWS 控制端点并收到 403，保留原失败和 `strictProtocolPassed=false`；后来通过的受控测试不抹去该记录。本次没有真实付费供应商、远程 CI、部署或生产验收结论。

### URL map 复制涉及的原构造器文件

以下文件均位于 `internal/entity/models/`；只在原接收 `baseURL map[string]string` 的构造器中复制 map，未新增构造器或配置门面：

`302ai.go`、`aliyun.go`、`anthropic.go`、`astraflow.go`、`avian.go`、`azure_openai.go`、`baichuan.go`、`baidu.go`、`bedrock.go`、`cohere.go`、`cometapi.go`、`deepinfra.go`、`deepseek.go`、`dummy.go`、`fishaudio.go`、`futurmix.go`、`gpustack.go`、`gitee.go`、`google.go`、`groq.go`、`huaweicloud.go`、`huggingface.go`、`hunyuan.go`、`jiekouai.go`、`jina.go`、`lmstudio.go`、`localai.go`、`longcat.go`、`mineru_local.go`、`mineru.go`、`minimax.go`、`mistral.go`、`modelscope.go`、`moonshot.go`、`n1n.go`、`novita.go`、`nvidia.go`、`ollama.go`、`openai.go`、`openrouter.go`、`orcarouter.go`、`ppio.go`、`paddleocr_local.go`、`paddleocr.go`、`perplexity.go`、`qiniu.go`、`replicate.go`、`siliconflow.go`、`stepfun.go`、`togetherai.go`、`tokenhub.go`、`tokenpony.go`、`upstage.go`、`vllm.go`、`volcengine.go`、`voyage.go`、`xai.go`、`xiaomi.go`、`xinference.go`、`xunfei.go`、`zhipu-ai.go`。

## Consumer 与 Skills 的明确问题修复例外

冻结目标：`fcebcebe1e0e798b42c18e5593747cd20eb4c757`。本节对应 consumer 原有 **34 项**明确问题修复，以及后补 Skills 闭环发现的 **7 项**修复。以下改动留在原文件、原函数和原调用路线中，不以旧 Go 兼容接口作为保留理由；本范围没有提前采用后续上游架构改进。

恢复冻结目标的目录、文件、函数、路由及原生配置，本身不是例外。已移除的 PostgreSQL 共享 schema、不可变 Skills 包／持久任务协议、旧模型绑定包装、严格 JSON 网关等，不因旧测试存在就恢复。一个旧测试同时含有效断言与旧私有策略时，两部分分别记录。

本节不代替模型、Agent、引擎、ingestion 及根集成额外修复的记录。下面提到的根引擎配套修复，是 Skills 真实闭环的依赖，不重复计入 consumer 的 41 项。

### 对象存储：保留命名空间、真实错误与有界 I/O

| 编号 | 原文件／原函数 | 明确问题与最小原位修复 |
| --- | --- | --- |
| C01 | `internal/storage/s3.go`：`NewS3Storage`、`resolveBucketAndPath`；`internal/storage/oss.go`：`resolveBucketAndPath` | S3 构造丢失 `Bucket`／`PrefixPath`，共用物理桶时不同逻辑桶的同名对象可能覆盖。保存构造配置，在原 resolver 内保留逻辑桶前缀，与原 MinIO 规则一致。 |
| C02 | `internal/storage/{minio,s3,oss}.go`：`Get`、`Put`、`GetPresignedURL` | 原方法遇错重建 SDK client、丢失错误原因或使用无界 context。保留构造期 client 和 SDK 重试，给原操作设置有界 context，包装真实错误并关闭响应体；`Storage` 公共接口不变。 |
| C03 | 同上：`Health` | 健康检查写固定桶／对象且不清理。改用原 SDK 的只读 `HeadBucket`、`BucketExists` 或 `ListBuckets`，保留超时与错误。 |
| C04 | 同上：`RemoveBucket` | 共用物理桶时删除逻辑桶可能越界；列表／删除失败被忽略。只处理 resolver 对应前缀，保留共享物理桶，返回真实失败。 |

### CLI 与本地 Skills 文件消费者：拒绝假成功，守住实际路径

| 编号 | 原文件／原函数 | 明确问题与最小原位修复 |
| --- | --- | --- |
| C05 | `internal/cli/http_client.go`：`Request`、`UploadMultipart`、`RequestStream` | 非 2xx、业务码缺失／null、畸形上传确认可被当成成功，原始错误体又可能泄露凭据。原方法校验响应信封和状态，限制缓冲响应大小，保留附件信息，以状态／业务码报告错误。 |
| C06 | `internal/cli/common_command.go`：`AddAPIServer`、`GetAPIServerInfo`；`http_client.go`：`Headers` | 指定名称的服务器错误读取当前 profile，可能 nil panic；新增服务器未绑定客户端 token，Web 请求忽略显式 API token。原 registry／client 按指定名称绑定和查询认证状态。 |
| C07 | `internal/cli/common_command.go`：`SaveServerConfig` | 原占位实现未写任何配置却返回 nil。沿原认证检查后明确返回未实现；不恢复旧的配置持久化门面。 |
| C08 | `internal/cli/lexer.go`：`peekToken`、`readQuotedString` | 输入末尾的 `CHUNK` 可切片越界；未闭合引号不能被当成完整字符串。限制原切片边界，并由原引号读取函数返回闭合状态，词法调用方据此拒绝无效输入。 |
| C09 | `internal/cli/parser.go`：`Parse`；`user_parser.go`：`parseRemoveTags` | 叶解析器可能忽略尾随命令／重复终止符，或未消费有效 dataset 参数。在原解析路径检查语句边界并消费参数，保留冻结目标允许的可选分号。 |
| C10 | `internal/cli/admin_parser.go`：`parseAdminListCommand` | `ListAdminTasks` 和 dispatch 已存在，`LIST TASKS` 却无解析分支。补上返回原 `list_admin_tasks` 的缺失分支。 |
| C11 | `internal/cli/user_command.go`：`GetChunk` | 动态 chunk ID 可改变 URL 查询或路径结构。在原请求表达式转义路径段。 |
| C16 | `internal/cli/filesystem/skill_install.go`：`installSkill`；`skill_hub/source/local.go`：`Fetch`、`Inspect` | 包名／相对路径可能逃出临时目录；过滤 walker 之前可能先跟随 `SKILL.md` 符号链接。原物化路径逐段验证，要求根目录和 manifest 为相应的普通文件／目录。 |
| C17 | `internal/cli/filesystem/skill_install.go`：`skillExists`；`skill_uninstall.go`：`uninstallSkill`；`skill.go`：`UploadSkill` | 列表错误被当成技能不存在；空 provider／不支持索引的 provider 可报卸载成功；上传后索引失败仅打印。沿原父目录列表传播错误，明确拒绝缺失能力，返回索引失败。 |
| C18 | `internal/cli/filesystem/skill_install.go`：`NewInstallSkillCommand` | 代理 URL 日志可能输出 userinfo 密码。原日志改用 `URL.Redacted`；不把它扩大解释为任意 URL 查询参数的秘密扫描。 |

### 文件、文档、数据集与 chunk：授权先于副作用，错误回到调用方

| 编号 | 原文件／原函数 | 明确问题与最小原位修复 |
| --- | --- | --- |
| C12 | `internal/service/file.go`：`UploadFile`、`createFolderRecursive` | 外租户父目录被接受，批次后段路径错误可留下已写对象／元数据，嵌套目录首段被跳过。先验证整个批次与父目录归属，修正原递归索引，记录尝试对象及元数据，独立读回已存字节；失败补偿并合并清理错误。 |
| C13 | `internal/service/file.go`：`DownloadAgentFile` | 空 tenant／location、绝对路径和遍历路径可能离开逻辑对象范围。存储访问前拒绝，保留原 tenant 下载桶映射。 |
| C23 | `internal/handler/document.go`：`CreateDocument`、`GetDocumentByID`、`GetThumbnail`、`UpdateDocument`、`DeleteDocument`、`GetDocumentPreview`、`DownloadDocument` | 登录身份不足以授权任意文档 ID。沿原 handler／service 先执行 dataset `Accessible` 检查，再读取对象或写数据；创建者取认证身份。没有新增兼容路由或管理接口。 |
| C24 | `internal/service/document.go`：`toResponse` | 恰为 `1e12` 的毫秒时间被当成秒。原转换分支纳入这个 13 位边界，创建／更新时间一致。 |
| C25 | `internal/service/dataset.go`：`CreateDataset` | 原创建生成 UUID4，而原读／改／删校验严格要求 UUID1，创建后无法读回。在原创建函数调用 `uuid.NewUUID`、去连字符并传播错误；不放宽 UUID1 校验，不改生成访问凭据的全局 token 函数。 |
| C26 | `internal/service/document.go`：`CreateDocument` | 原构造遗漏 ID 与非空 `parser_config`，真实 MySQL 报错。在原构造补现有 token 生成器及空 `JSONMap`，真实创建分别读回 ID 和认证创建者。 |
| C28 | `internal/service/chunk/chunk.go`：`Get`、`List`、`UpdateChunk`、`RemoveChunks`、`RetrievalTest`；`chunk_types.go`；`internal/handler/chunk.go`：`Get`、`RemoveChunks` | 仅加入 tenant 不等于可见私有 KB；嵌套路由未绑定 dataset／document 与 chunk。原方法在 ES／模型查询或写入前调用 `Accessible`，限定可见且请求指定的 KB，校验存储文档归属；原请求结构携带嵌套 ID，传播 `Get` 引擎错误。 |
| C30 | `internal/handler/document.go`：`StopParseDocuments` | 原方法信任 context 中的 `user_id`，缺少 `GetUser`／dataset 校验并可 nil 解引用。先验证原认证身份与 dataset，再绑定／访问服务；保留标准 JSON binder，不恢复旧的重复键／尾随 JSON 网关。 |
| C31 | `internal/service/dataset.go`：`SearchDatasets` | SQL 扫描后的精确数字为 `json.Number`，原保存配置分支只认 `float64`，导致阈值、向量权重、top_k 被丢弃。在原分支补数字类型，保留原默认值、范围限制与合法 0。 |

文档删除的原 `deleteDocEngineData`、`cleanupFileReferences` 还需把失败返回调用方，不能只记日志继续报成功；对应必要返回值差异见下表。对象、索引与 SQL 仍不是一个事务。

### 身份、恢复、OAuth、MCP 与并发写入

| 编号 | 原文件／原函数 | 明确问题与最小原位修复 |
| --- | --- | --- |
| C14 | `internal/handler/system.go`：`GetConfigs` | 公开配置响应包含数据库、provider、SMTP、OAuth 等凭据槽位。序列化响应自己的副本并递归脱敏已知秘密字段，保留非秘密配置，不改源配置。 |
| C15 | `internal/service/user.go`：`GetUserByToken`、`GetUserByAPIToken`；`internal/handler/common.go`：`GetUser` | 实际 HTTP 曾允许禁用 JWT 身份创建 Agent；空 ID／typed nil 也可能被接受。在原方法拒绝非活跃用户和空身份。根集成的 DAO token 查询修复单独记录。 |
| C20 | `internal/service/memory.go`：`requireMemoryAccess` | owner 快捷路径忽略禁用 tenant；团队关系可能接受邀请角色。在原授权函数要求活跃 tenant、活跃成员关系及允许的明确角色。 |
| C22 | `internal/service/user.go`：`GetUserProfile` | 原 profile 返回存储的密码 hash。只移除 `password` 与无用临时变量，其余原字段按冻结目标保留。 |
| C27 | `internal/service/user.go`：`Login`、`LoginByEmail` | 两条原登录路径分别只检查 `status` 或 `is_active`，可能签发禁用账户 token。两者都检查，并在 token／SQL 更新前遵守 `disable_password_login`；RSA／内层 Base64 协议不变。 |
| C29 | `internal/service/tenant.go`：`AddMember` | 真实并发 MySQL 读后写产生 8 个重复邀请。在原函数事务锁定活跃 owner tenant，检查活跃 actor／被邀请者，锁内重查关系再创建；不新增操作门面。 |
| C32 | `internal/service/oauth_login.go`：`OAuthLoginInitiate`、`OAuthCallback`、`registerOAuthUser` | Redis 缺失仍可生成无记录 state；Get＋Delete 可并发重用；禁用用户或真实 SQL 错误处理不完整；新账户引导可能部分落库。要求原 Redis，以请求 context 的原子 GETDEL 消费 state，验证活跃状态、传播 SQL 错误，原 user／tenant／membership／根 File 在同一事务创建。 |
| C33 | `internal/service/mcp.go`：`CreateMCPServer` | 真实并发 MySQL 可创建 8 个同 tenant 同名服务器。在原 tenant 行锁事务内重查重名并创建，保留原 MCP wire 与响应字段。列表筛选／分页／凭据字段的根集成补证另记。 |
| C34 | `internal/service/user.go`：`ForgotIssueCaptcha` | 仅非空检查使畸形邮件地址进入 SQL。在原入口验证规范邮件地址再查询，不恢复旧严格 query／重复键解析器。恢复发信的请求 context 差异见下表。 |

### 推理文本与元数据：保留合法值，拒绝损坏结果

| 编号 | 原文件／原函数 | 明确问题与最小原位修复 |
| --- | --- | --- |
| C19 | `internal/service/think_tag.go`：`NextThinkDelta`、`ExtractVisibleAnswer` | 跨片开标签、嵌套或未闭合私有块可能把推理内容作为可见文本输出。沿原状态保存待定标签前缀与深度，并通过原处理路径提取可见答案。 |
| C21 | `internal/service/metadata.go`：`ExtractMetaFields`、`ParseLengthPrefixedJSON`、`ParseAllLengthPrefixedJSON` | 畸形／不支持的输入可伪装为空元数据，损坏帧尾被接受，大于 2^53 的整数丢精度。完整 JSON 解码使用 `UseNumber`，校验准确帧长度，保留原公开函数名／签名以及合法的 0、false、空值语义。模型生成过滤结果的错误需由原 `ApplyMetaDataFilter` 返回，不把解析失败当成有效空结果。 |

### Skills 原生运行闭环补修

对应路径始终是 `SkillSpace → File 目录／版本／正文 → SkillIndexer → SkillSearch`，使用原 `skill_spaces`／`skill_search_configs` 和原对象、索引接口。没有恢复 `/skill-core`、不可变 assets 包、操作／恢复协议门面。

| 编号 | 原文件／原函数 | 首次明确失败与最小原位修复 |
| --- | --- | --- |
| S01 | `internal/service/skill_indexer.go`：`getSkillContentFromFolder` | 实际选择 `2.0.0` 目录，却因 `SkillInfo` 遗漏 Version 而索引为 `1.0.0`。在原构造赋所选目录的版本。首败 `72baef29a6b34d4cb9ad19a4e222b410`。 |
| S02 | `internal/service/skill_search.go`：`keywordSearch`、`hybridSearch`、`vectorSearch` | ES 索引已将 `orange` 分词为 `orang`，原查询未同样分词，真实关键字搜索无结果；原纯向量分支又带词法表达式，语义命中被过滤并退成空关键字结果。ES keyword／hybrid 原分支调用既有 tokenizer；纯 vector 原分支只传 dense 表达式，Infinity 仍走原文本分析。首败 `9e8d0eb1dbaf45cfb82a6582a9286af3`、`abf3337454b14dd0a0a19d4b6d36d067`。 |
| S03 | `internal/service/skill_search.go`：`getEmbedding`；`skill_indexer.go`：`generateEmbedding`、`generateEmbeddings`、`getEmbeddingDimension` | 原参数含 ctx 却未绑定驱动请求，已取消请求仍外连并返回 nil。原函数早检查取消，并给请求自己的 `APIConfig` 副本绑定 ctx；实际 Search 另验证运行中取消、错误原因及供应商连接结束。首败 `b4bd5c951c104425bff444fd94f0234f`。 |
| S04 | `internal/service/skill_indexer.go`：`ReindexAll` | 外租户请求虽最终拒绝，却先把配置行数从 1 改成 2。原 space 查询和租户拒绝移到配置、模型、索引副作用之前。首败 `ddefb332de304a118bf9c61018e76297`。 |
| S05 | 同上：`ReindexAll`、`getSkillsFromFileSystem`、`getSkillContentFromFolder` | 实际删除 MinIO 中 `SKILL.md` 后，原重建跳过读取错误，破坏旧索引并报告 indexed=2／failed=0。传播原目录／正文读取错误，完整读取源之后才替换索引；检查存在性／删除错误，仅成功索引后发布配置版本。源读取或维度探测在替换前失败时保留上次可检索索引。首败 `490553512de24f26a63eb9230980b0a0`。 |
| S06 | `internal/service/skill_space.go`：`asyncDeleteSpace` | 实际索引删除 503 后仍标记 deleted；文件删除失败／panic 也可假成功，索引已删后的重试又可能永久失败。保留原异步删除，失败或 panic 恢复可重试 active；先检查索引，仅明确不存在才跳过，503／未知错误不得当成不存在。首败 `6514e006d83d42dc93e00aa2c6d75f30`。 |
| S07 | `internal/service/file.go`：`deleteSingleFile` | 实际对象 Remove 返回错误后仅记录日志并继续 SQL；未初始化 Logger 时则 panic。原方法在 SQL 修改前返回包装后的 Remove 错误，保留对象地址供重试。直接失败路径要求不 panic、返回原原因，不能仅以异步 panic 恢复后的 active 状态证明成功。首败 `7954430433794bf0af6840f8a05e67de`。 |

根引擎配套原函数修复：Skills 独占索引的 `DropChunkStore` 不能误用共享 KB 的条件删除；限定 `skill_*` 索引后调用原 `dropIndex`，并检查删除确认。`buildQueryStringQuery` 将 Skills 显式原字段映射到已有 `_tks` 字段、保留 boost，不修改调用方 slice。它们的源码与定向证据由根集成持有。

### 必要签名与请求结构差异

这些差异用于把取消／错误从原函数传回实际调用方，不提供双签名适配或旧接口桥接。调用方同步迁移。

| 原文件／函数 | 差异 | 必要性与边界 |
| --- | --- | --- |
| `internal/cli/lexer.go`：`readQuotedString` | `string → (string, bool)` | 私有词法函数返回引号是否闭合，避免截断字符串被接受。 |
| `internal/service/connector.go`：`GoogleWebOAuthCallback`、`exchangeGoogleWebOAuthCode` | 增加首个 `context.Context` 参数 | 原 Google token 交换绑定请求取消和原有超时；保留原状态、`code_verifier` 和回调路线，不新增 OAuth 门面。 |
| `internal/service/document.go`：`deleteDocEngineData`、`cleanupFileReferences` | 无返回值 → `error` | 原删除调用方能感知索引、元数据、映射或对象清理失败，不吞异常报成功。 |
| `internal/service/mcp.go`：`ImportServers`、`TestServer` | 增加首个 `context.Context` 参数 | 原 live MCP 调用继承请求取消，预取消不外连；handler／全部调用方传当前 context。 |
| `internal/service/mcp.go`：`safeJSONMap` | `entity.JSONMap → (entity.JSONMap, error)` | 畸形配置不能被空对象掩盖；原合法空／null 默认行为保留。并非重建通用严格 JSON 网关。 |
| `internal/service/metadata_filter.go`：`ApplyMetaDataFilter` | `([]string, bool) → ([]string, bool, error)` | 过滤／模型生成结果的真实错误回到原检索调用方，合法空选择仍是独立语义。 |
| `internal/service/user.go`：`ForgotSendOTP` | 增加首个 `context.Context` 参数 | 原恢复发信流程继承请求取消，原认证／challenge／OTP 路线不变。 |
| `internal/service/chunk_types.go`：`GetChunkRequest`、`RemoveChunksRequest` | 原请求结构携带嵌套路由的 dataset／document ID | 让原 chunk 方法校验路径与存储对象绑定；不新增管理接口。 |
| `internal/service/file.go`：`UploadFile` | 返回值改为命名的 `result`／`uploadErr` | Go 函数类型、参数与返回类型不变，仅用于 defer 合并补偿错误；不属于调用接口变更。 |

Skills 的上述原方法没有新增生产函数或改变公开参数／返回类型。Agent 的类型化流事件、ChatSession 的错误／context 传播以及根 FileCommit 的命名返回，由相应 owner 记录，不混为 consumer 的新增接口。

### 真实存储与非原子边界

- **上传补偿不是分布式事务。** 真实 SQL 与对象字节独立读回；上传失败清理本批已尝试对象及元数据，并保留清理错误。不能把它描述为对象存储和 SQL 同时提交。
- **文档清理仍跨 SQL、索引与对象。** 原函数传播失败，不代表已发生的外部删除会自动回滚。共享 File 引用的判断仍在原清理路线；不存在新操作日志门面。
- **Skills 重建仍替换原索引。** 完整源读取、维度探测发生在替换前，相关失败保留旧索引；替换之后的索引写入失败如实返回，不宣称旧索引自动恢复或全流程原子化。原可选 text-only 索引路线保留，不把受控测试扩大成 Infinity 零向量占位的生产证明。
- **Skills 删除可先删索引，再遇对象失败。** 此时索引可以已不存在，空间恢复 active、File 的 SQL 对象地址保留；下一次仅在确认 HEAD 404 后跳过索引删除，继续清理。HEAD／DELETE 503 或未知错误都不能当成“已不存在”。真实对象已删但响应报错的注入也必须可重试，不假造回滚。
- **空间沿原 soft delete。** status=0 与保留的配置／空间历史不等于物理 SQL 擦除；测试结束 DROP 私有数据库的清理证据与业务删除语义分开。
- **原 filename 仍是对象 location。** 实际 MinIO 验证：255 个 ASCII 字符与短中文名称成功；256 个 ASCII 字符和 255 个汉字的单路径段被实际存储拒绝，SQL／对象无部分写入。旧纯 `ValidateSkillUploadPath` 的“255 Unicode 码点可接受”不等于真实上传支持。首个错误假设的失败 `b8b0c611318b44dab585f6fa41e4f7c1` 保留；没有为了该旧承诺引入不透明对象名或新命名兼容层。
- **原 artifact 桶的归属边界不扩大。** 当前受认证的全局 basename artifact 路线没有 owner／run／session 元数据，不写成公开无认证，也不以认证成功冒称已证明 tenant 对该 basename 的所有权；tenant 下载桶的路径检查另有自己的证据。
- **OAuth 证据限于原路线。** 用户 OAuth2／userinfo 回调没有因此获得 ID-token 签名、JWKS、nonce 或完整 OIDC 身份令牌校验证明。Google 连接器原有 `code_verifier` 传递不因此删除。state 消费失败／引导失败不恢复已消费证明供重放。
- **验证服务与外部供应商分开。** MySQL、MinIO、ES 为真实私有服务；供应商 HTTP、ES 错误代理、对象删除后响应丢失为受控故障。不是实际付费供应商、真实网络故障、远程 CI 或生产验收。

### 证据索引

- `consumer-exceptions.json`：C01–C34 与 S01–S07 的函数、问题和原始证据索引；`later_upstream_improvements=[]`。
- `consumer-source-map.json`：冻结原文件／原函数映射；Skills 补修的生产函数列表没有增删。
- `consumer-test-disposition.json`：旧断言逐项去向。Skills 的 query 包装、严格零向量网关和纯 rune 长度承诺明确退休，不用新闭环冒称这些旧私有行为等价。
- `consumer-skill-final-evidence.json`、`consumer-skill-followup.patch`、`consumer-skill-final2-inputs.json`：所有 Skills 首败、修复、五条路径前后哈希及存储边界。最终一般 service 显式 86 文件组合 `1affbc19b8334647864b365039c799d9`，**324 pass／0 skip**，552 个输入哈希执行后无漂移；不称整个 service 包／模型／Agent 全量通过。
- Skills 新增真实测试：`internal/service/skill_core_test.go::TestCanonicalSkillsSQLObjectsAndElasticsearch`、`TestCanonicalSkillUploadPathBoundarySQLObjects`。前者覆盖原生空间／File／最高版本／真实索引／keyword、semantic-only vector、hybrid／授权／取消／失败重试；后者覆盖实际文件名和路径边界。
- 复用的原 scope 证据：CLI／filesystem `a9eeaf58cd2e4d2696b86cede214d40f`；storage 实际 MinIO `7ced1cb5e4924596aa727ae36b19b736`；handler 原组合 `0193aaf5153a4337a500d9e18ddf0b72`（495 pass／0 skip）；active chunk 包 `e1519d7d969e49b58f75ff9c0e8b4c15`（34 pass／0 skip）。后续其他 owner 的新增源码／测试及最终组合需引用各自回执，不由这些旧输入结果覆盖。

## 提前采用的后续改进

| 后续提交 | 采用范围与原因 | 依赖与验证 |
| --- | --- | --- |
| `b9f4d26aadf38b5964fa5328d6763a1a1a86d64e` | 仅原 ES 更新查询的 typed ID 列表和 exists 排除，保留原 `conditionTermsList`、`mustNotExistsClauses` 位置；避免条件消失后扩大写入。 | 冻结目标现有查询接口即可；实际 ES typed 条件/KB 隔离回归。 |
| `3830b99f78d4cc36ad1ce5b3e1a55934ce331d7d` | 仅原 ES 单条更新 `refresh=wait_for`，保证调用返回后可读。 | 沿用冻结 ES 依赖；真实写入后独立读取。 |

未采用更晚上游架构。上述两项与普通缺陷修复分开计入账本，不把提前采用部分描述为冻结提交原有行为。
