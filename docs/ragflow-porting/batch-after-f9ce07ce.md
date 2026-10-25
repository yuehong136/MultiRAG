# after-f9ce07ce 本地交付

冻结范围为 first-parent 连续 20 项：排除 `f9ce07ced14640730fc66d0b64ad4608718cae1f`，
包含至 `4776bfa8a2ead7bdd45e6d0aa860044f9095a344`；直接后修上限
`519e7d98a5651564d4e35d6648f006cba4baaf4f`。没有滚动纳入下一批。
逐项完整补丁及 SHA256 已核对；本批不改变 Python 生产后端与停滞 Go 实现的职责边界。

本地组合的可执行源码提交为 `6039f98299a77be9fe06778450651acd70a2815e`，
后续提交仅补充本文、接口说明和总账。
Web master 与 `.github/consumer-web.json` 固定为
`8ae122d3e6180805c5bcefc4d45ae4c21e8966a7`。
最终 main SHA、完整证据、恢复归档和清理读回由本机批次目录中的
`delivery-complete.json` 绑定；该文件必须完成后才能宣称本地交付关闭。

## 逐项处置

| # | 上游 | 本地结果与质量依据 |
|---|---|---|
| 1 | `7e684411` | 保存搜索权重按字段存在性覆盖，合法零值保留，有限 `[0,1]` 严格校验；贯通实际单/多库排名与 ask 引用。有效库范围先授权；内部 `None` 与显式 `[]` 贯通元数据和 Dealer。Web 已直接保存向量权重，保留。 |
| 2 | `87918650` | REST bot、Dify、chunk、下载模块迁移；原定义、唯一注册、鉴权和二进制下载保留。修复 `not_found` 位于首字符的判断。 |
| 3 | `bf9297a3` | 在实际 Discord 模块说明中增加 Bot Token、server/channel 配置及只读权限；保留批次与删除同步限制。 |
| 4 | `3f02ca7b` | Go Astraflow embedding、rerank、TTS；严格校验索引、数量、数值、媒体和用量，保留原 provider 身份。 |
| 5 | `302f97de` | Go Groq reasoning 配置及 ASR/TTS；multipart、模型能力、终态与取消走真实 facade。 |
| 6 | `e6dd3975` | 与 #13 成对评估；OpenAI 补全保持请求级无状态，不引入随后撤销的持久会话。 |
| 7 | `432e9664` | Go OpenAI 音频端点及 SSE；坏响应、缺失终态、取消明确失败，不伪造成功。 |
| 8 | `50424df4` | 不适用：当前产品仅注册中英两种语言；完整法语补丁不可达，不加入无消费者的键。 |
| 9 | `0ce66557` | REST `session_id` 可回落 `conversation_id`；两者冲突在 I/O 前拒绝，身份与助手绑定保留。 |
| 10 | `8d8ea718` | Go HunYuan chat，租户实例隔离、严格响应和调用边界。 |
| 11 | `5d022d83` | Go Local PaddleOCR 使用租户 URL、允许无 key；loader/factory/实例准入全部贯通，仅接受授权字节，保留云端 OCR 身份。 |
| 12 | `f8c626bb` | 有依据不采用：完整差异中的演示 worker 以 sleep/progress 标记完成，未解析、向量化或入索引；保留真实 Python worker 与 Go SQL 任务视图，不新增无执行器的成功入口。 |
| 13 | `bb148edf` | 完整代码及测试 blob 证明是 #6 的撤销；同步/流式 OpenAI 请求携带会话提示也不改持久会话。 |
| 14 | `5069561a` | REST 默认“服务端历史 + 本次最后用户消息”；显式严格布尔 `pass_all_history_messages` 可重放分支。成功后保存，失败/取消/保存错误不发成功尾帧。当前 Web legacy 完整历史合同独立保留。 |
| 15 | `bb6cfc14` | Go TokenHub chat 与 embedding；自定义模型、能力与实例身份贯通真实入口。 |
| 16 | `69f301b8` | Go HunYuan embedding，按返回索引重排并拒绝缺失/重复/非有限向量。 |
| 17 | `e7d45dd6` | DocGenerator 提供 `doc_id/filename/mime_type/size` 离散输出，每轮清旧值；Web 默认、schema、变量选择及 DSL 保存/重开匹配，原 download 合同保留。 |
| 18 | `e6068a7f` | 本地上传严格 multipart 列配置，文档覆盖在数据集默认之后生效；Web 分文件 CSV/XLS/XLSX 列角色、部分成功和重试保留。纯 Excel parser 通过现有依赖小补丁暴露，无版本升级。 |
| 19 | `9d1006e4` | MinerU HTML 清理保留行/单元格与数学比较文本；MCP 在授权后验证 URL/import 类型并逐请求绑定目标 IP/Host/SNI，重验重定向/SSE，不改全局 DNS，取消释放资源。 |
| 20 | `4776bfa8` | 保留原文档与 runtime 附件的独立授权下载入口；实际 Web 消费者已使用该区分，不增加无消费者别名。 |

## 验证与证据边界

证据根目录：`~/.local/state/multirag/validation/20261025-batch-after-f9ce07ce/`。
`integration/final-inputs.json` 固定 3,251 个后端、2,469 个 Web 非 Markdown 输入，
锁文件、消费者 pin、native 编译输入及借用资源另有精确记录。
最终验收后重新核对输入哈希，未用旧二进制代替新源码构建。

- Python 组合单测首轮 840 通过、1 个临时验收脚本跨测试模块导入门禁失败；
  修复临时脚本的假件归属后，39 项 harness 定向复跑全部通过。首轮失败保留。
- 最终后端定向集成 31 项通过、零 skip：实际 JWT/API Key、HTTP 业务码、
  持久历史、OpenAI 无状态、可信附件、四种检索模式的元数据/文档范围交集、
  保存权重实际排名、MCP 拒绝无副作用、逐文件表格配置与真实 worker。
- 固定 Web 对最终后端再验收 1 项通过：真实 Chrome 上传 CSV/XLSX，
  同名列使用不同角色；SQL、MinIO 字节、真实解析 worker 和 Milvus 三行读回一致。
  embedding 边界受控，不称真实供应商推理。明暗主题、键盘与 XLS 的 UI 证据复用
  C 对同一 Web 源码、锁文件及依赖补丁的已核对报告；多图按 contact sheet 审阅。
- MCP 双向真实进程协议矩阵 22 行通过，另有 Agent tool binding 跨轮工具调用、
  回收与取消验收；两份脚本锁检查通过。测试仅放行明确登记的 loopback fixture origin，
  未放宽生产地址策略。
- Go 模型包 race：387 顶层、2,844 个含子项的命名测试通过，零失败/skip；
  entity、Local PaddleOCR service 定向 race、internal build/vet 及当前 handler vet 通过。
  CLI/admin/server 分别从本批源码重新链接。
- Go 真实 JWT HTTP/CLI/SQL 验收 15 个子场景及父测试通过：7 个 provider、14 个实例，
  零值、索引重排、媒体/SSE、实例隔离及拒绝无 HTTP/SQL 副作用。
  生成参数零值经实际 SQL 实例 service facade 验证；公开诊断 HTTP schema 未新增数值字段。
- 改动 Python 文件 Ruff lint/format、mypy、8 个 import contracts、async DB 门禁、
  `uv lock --check` 通过。Web 同输入复用 `test:ci`（915 Node、881 Vitest、93 desktop、
  21 tooling）、build、lint、类型/i18n/API/inventory/bundle 检查原始 exit-0 报告。

原始 A/B/C/D/G 首错与精确补跑保留在各自报告中；I 的首错也不覆盖：
旧 Astraflow catalog 数量需匹配新能力；live fixture 需接受 CLI text-part 内容，
数值参数应由其实际 service facade 验证；MCP 运行记录包装器须保留 Popen 类型，
命令构建使用仓库实际 `cmd/*.go` 文件。没有通过吞异常、放宽断言或跳过服务换绿。
A 的未变 chunk/Dify/下载 15 场景及 C 的 Web 证据仅在对应输入哈希匹配时复用，
不把各轮用例数相加冒充全量验收。

## 收口与限制

本批各 owner 的 DB、对象桶、集合、Redis 精确键/前缀与监听器由 I 独立复查；
I 的 Go scratch DB 在零行读回后删除，私有配置、测试 staging、临时脚本和 listener 清理。
本批普通 worktree/branch 仅在 recovery bundle verify、全新 bare clone、
connectivity fsck 与每个 ref 的 SHA 核对后删除；7 个历史 protected managed 树保留。
主目录原 28 项 WIP 保留，PROGRESS 只追加本批块；原 WIP 内容不进入提交。
清理、恢复归档与最后的 WIP 证据由最终 `delivery-complete.json` 引用。

未 push、未部署；远程 `ci-required` 未运行。本批 Web pin 尚未发布，远程 checkout
当前不能取得该提交。没有全库 Python/Go 测试、真实付费供应商、常驻 worker、生产或部署验收声明。
受控 HTTP/SDK、实际本地 SQL/存储/浏览器与真实供应商、远程 CI、生产证据分别记录。
未创建或恢复自动化，未派下一批。
