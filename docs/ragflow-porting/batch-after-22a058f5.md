# 22a058f5 后二十项本地交付

本批适用本地实现与验证已完成，代码组合为 `ae59a29ec2b9c2f6a968cae71cdf23636211fc37`；文档提交另由 Git 记录。冻结范围为 `(22a058f5, fcebcebe]` 的连续 20 个 first-parent 提交，参考上限 `519e7d98`；后端基线 `2ff39c6e`，Web 基线 `fd969fd0`。不扩批、不 fetch；未 push、未部署，远程 `ci-required` 尚未运行。

逐项账本已按 `(编号, 上游路径)` 精确关联 **408/408 条决定，366 个去重路径**，缺失和多重匹配均为 0。分工为 A181、W46、I42、M41、R37、C28、P15、F11、S7。该数字包含适配、保留更强机制、有依据不采纳和明确 unsupported，不表示新增了 408 项功能。第20项的重命名 `internal/cache/redis.go → internal/engine/redis/redis.go` 保留旧路径。

## 逐项处置

|项|上游|实际处置与边界|
|---|---|---|
|1|`89aac826`|Web 已知当前作者使用账户头像；共享/匿名场景及无可靠作者身份的历史消息保持中性头像，不推断为当前查看者。|
|2|`115b730d`|Go 独立 ingestion DSL 编译、真实分块和 EXPLAIN；CLI 实际消费，未交付分布式 ingestion/队列流水线。|
|3|`5a7d7771`|保留已有 Go 本地 skill 删除、持久失败状态和新实例恢复机制；补真实 HTTP/PostgreSQL 失败与外租户保留证据。|
|4|`b5a426e6`|Python 接入四种 managed text transport，复用现有控制面、加密密钥、绑定授权和独立 worker；Web 读取 provider spec。600秒 inbox/worker admission 有明确边界，入队后崩溃恢复及 exactly-once 不在本次交付。|
|5|`547139da`|保留 canonical/alias 大小写无关查找、原子 loader 与深拷贝；补 Gitee `jina-clip-v2` embedding 元数据。|
|6|`4115282c`|完整合并 581 新增、227 模型字段修改；逐字段账本保留本地增强，仅归一同模型重复 alias。|
|7|`234f1b7c`|Go 可选 `office_oxide` 原生解析与真实 Office 样本；macOS 证据有效，Linux/Windows/native 峰值内存上限未建立。|
|8|`cafa0f2e`|Go SSE 请求级写 deadline、取消、EOF/flush 与实际持久化；M 的 provider 消费者接入 S helper，不吞错误或改成空成功。|
|9|`3fa15c0e`|Go Canvas DAG、显式 Loop、原生 UserFillUp、授权运行/恢复/取消及 CRUD/发布版本；模型用 owner tenant 绑定，Redis 原子收尾。Switch/Categorize、通用循环、文件表单、多中断、真实模型工具调用等保持明确不可用。|
|10|`d32e05d5`|Go parser 的有界真实内容提取及严格错误；PDF、远程 OCR/TSR 未实现，DLA 仅受控 HTTP 合同。|
|11|`f671e7cb`|当前 Web 不支持 French，不引入无活动消费者的 locale；已有适用语言和缺键门禁保留。|
|12|`e89afbae`|Go parser 配置进入真实消费者，严格拒绝无效参数；内置 OOXML 只提取文本子集，不声称布局/OCR/公式求值。|
|13|`59d42039`|Python 活动密码登录在原请求事务内写 `last_login_time`；错误密码、禁用状态和事务失败均不留下写入。|
|14|`32d5c003`|Go API/CLI 使用正 `int64` 模型 ID，按租户、active/type 在供应商 IO 前绑定；名字选择兼容，名字默认赋值清除旧 numeric 优先级，默认端点迁至 `/models/default`。|
|15|`83e2180e`|保留已有更强 Ollama `/api/tags` 发现与严格协议处理，复用实际消费者回归，不制造重复实现。|
|16|`b5bea72e`|Python/Go 完整不可变文件快照、历史树/diff/content/versions；目标租户与私有数据集鉴权、root lock、SQL回滚及对象补偿。历史/工作对象分离，普通 Files 移删后历史可读；关联文档/skill/connector 编辑拒绝，未实现 GC，普通 Files 并发不在同一 root-lock 协议中。sessions 未知字段恢复精确错误与零写合同。|
|17|`eb6ea284`|追加 337 个 Google 目录声明；6→17 顺序重放后目录 2042 个，元数据标签不构成真实推理验收。|
|18|`400dfd50`|Web 当前 Text region 已支持 S3 自定义值；保留该更强入口，并核实际表单/API/SQL读回。|
|19|`bc963f8c`|Go 新实体使用随机 UUIDv4；读者兼容历史 UUIDv1，numeric model ID 和历史主键不重写。补真实 CreateDataset→SQL→读取/更新/metadata/document 等消费者及跨租户拒绝。|
|20|`fcebcebe`|最后将 wrapper 与本地 heartbeats 移至 engine/redis，迁移21个活动旧调用方；失败 Init 关闭连接且可重试、Close 撤销发布并允许重建、不可变配置与同步快照、真实 socket deadline；保留合法括号/zone IPv6。心跳有界只读，OAuth/document 和 Canvas 原子算法保持原合同。|

## Python 生产后端、并行 Go、Web

Python 生产后端继续使用 FastAPI/SQLAlchemy。本批活动行为包括 Channel transport/密钥编辑与 admission、成功密码登录时间、文件快照与 sessions 严格字段校验。依赖只按实际消费者最小调整：discord-py 锁至 2.7.1，直接声明已有锁中的 aiohttp/defusedxml；保留本地 Python 3.12/3.13 与 LiteLLM 1.82.5，不复制上游大范围重锁。精确 1.88.0/1.88.1 导入实验在本机成功，不能把上游注释写成本机故障已复现。

Go 仍为并行实现。Canvas 运行绑定 actor、路径 Canvas、首次 DSL/输入和 owner tenant；恢复重新检查 SQL 权限，不能接管别人的 run。两个真实 Redis barrier 验证取消与 waiting/完成收尾的竞争；Lua 写入 Go 编码的精确 JSON，并原子处置 checkpoint 与 lease。active-model 取消测试使用受控模型边界，不能据此声称真实厂商 HTTP/工具调用验收。会话/记忆/引用持久化、产物上传下载和 Webhook/日志运行入口没有随 Canvas 交付。I 的 A/M/F router、startup、DAO 接线与 Redis20 已按当前组合验证。实际 Router.Setup + JWT 的模型 inventory/default、OpenAI driver connection、FileCommit 历史及公开 Echo run 均有独立 SQL/Redis/MinIO 读回和外租户零写证明；Echo 不代表真实模型执行。

Web master 已接收 `e7b7087d`，消费者 pin 已按 I 的 producer 对比决定指向同一提交。实际浏览器链覆盖 Python JWT、PostgreSQL、AESGCM 的 provider 编辑和 LINE 部分密钥轮换；辅助身份/发现端点有 fixture。Web 保持 Python `/agents/chat/completions`，没有切到 Go。未知历史作者不补造身份；390px 分享页标题压缩是保留的已知限制。

## 已有验证与复用条件

以下是 owner 的固定输入局部证据，不相加冒充最终组合或远程 CI。完整命令、raw/JUnit、来源 hash、失败记录及清理见账本中的 `ownerEvidence` 和对应 handoff。

|线|已有证据|
|---|---|
|P|21 unit、7 个真实密码/RSA/SQL回归；独立事务读回，依赖/锁检查与原 WordNet 7 测试。|
|C|293 unit、8 个 PostgreSQL/Redis集成，8 import contracts、asyncDB新增0；改用明确登记 scratch helper 后复跑8项并独立确认库消失。|
|F|Python6、unit15、跨栈PG/MinIO/race1；真实JWT U≠T、私有/team/admin/revoked、对象失败/SQL失败、普通Files移删历史；最后补后Go定向race/vet/binary。|
|S|race13顶层/58含子例，无skip；短WriteTimeout正负控制、完整EOF、PG提交/取消无半写、skill持久失败与新实例恢复；internal build/vet与三个独立main。|
|M|CLI122顶层/247含子例、entity45/122、模型SQL1、Eino race4、provider11/34；实际本地CLI parse/chunk/explain和HTTP chat/embed/rerank/default。另补UUIDv4真实SQL生命周期1项，无生产代码改动。|
|R|默认域、native六种Office真实内容、parser BOM/单根/错误输入、race/vet/build；未变的原生wrapper/fixture按字段复用，后改内置decoder单独复验。|
|A|`9b4fac6e` 固定输入补后 Canvas/component race、公共链race12顶层/26含子例、全internal build/vet；1136输入一致。真实HTTP run四key事前登记与独立清理已闭环。|
|W|产品固定输入 build、lint/typed/i18n/file-size 与 test:ci；报告 source Node1014、Vitest1256、desktop Node93、tooling Node21。这里只是相应测试，不代表桌面安装或完整跨平台验收。|
|I 已有Python组合|receipt `3ffc2e72`：49文件lint/format、8 import contracts、asyncDB新增0、FileCommit定向mypy、333 unit、21 SQL/Redis/MinIO、7 schema；只对应 `python-final-inputs.json`，生产者及运行输入哈希未变，按域复用。|

Web producer 比对只发现 additive FileCommit schema 差异，既有模型/import/helper 的 AST 一致，已保存 pin 决定。Redis 与共享路由组合已另行验证，复用的未变域按输入哈希限定；没有用旧 owner 的 build 结果替代新组合编译。

## 最终 Go 组合、接收与限定

账本对冻结后端 `2ff39c6e..ef21132c` 的31个非merge提交逐一计算 stable patch-id：24个 owner 后端来源提交各有唯一匹配，且改动路径 blob 相同；接收记录中的 blob 另作佐证。Web3提交按 master fast-forward 保留原SHA。完整27条映射在 `sourceToIntegratedCommits`，未唯一/未映射为0。`ef21132c` 是 A 接收点，随后 `24145253` 完成共享接线/Redis20，`ae59a29e` 修复 IPv6 兼容。最终文档提交及 main 的完整 SHA 记录在本批 `coordination/I-delivery-complete.json`，Web master/pin 为 `e7b7087d0bcbe7fbd4de558cc2cf1d9a8c5bb38d`。

`strictProtocolPassed=false` 保留。历史问题包括：C早期未登记的Testcontainers PG与git-admin路径精度；A旧HTTP hook漏登/漏清key（两个旧metadata已精确恢复清理，不能倒填原协议）；M借入S文件登记晚及一次编译中改源；R早期manifest正文未留；P/F模型资产、tokenizer、Ruff缓存或日志登记偏差；W早期venv借用元数据不完整。原始失败和限定保存在各handoff，后续通过不覆盖历史事实。

I 的42条共享/Redis决定已逐路径定稿，原决定另存历史，完整408条位于 `coordination/I-item-ledger.json`。本地 Go 验证由三个有明确复用范围的回执组成：

|组合与回执|实际结果与复用范围|
|---|---|
|`24145253` / `9c63ad8f`|internal build/vet、3个独立 main 构建；race 为 Redis/Agent/OTel 630、CLI/entity/parser 385、adapter/provider 38、真实公共链49、native parser17，共1119项含子例，均0失败/0skip。总run raw1：最后 native CLI 验收脚本误把正常单行数组按对象解析；保留此失败，不称该run整体成功。|
|同代码 / `3da5d599`|只修验收脚本的JSON envelope；重新构建 native CLI，实际 DOC/DOCX/XLS/XLSX/PPT/PPTX 六条命令均提取文本/Markdown，raw0。1442个代码/配置输入与native库等未变，仅验收helper变化。|
|`ae59a29e` / `c0cf9346`|IPv6兼容仅改Redis Init与其测试；重新执行internal build/vet、3个main、6个完整lifecycle race和49项真实公共链，全部raw0/无skip。其他Agent/model/CLI/parser/native输入域完全不变，按哈希复用上述证据；不称此SHA单次全量已跑。|

独立新进程 `4b899c04` 对本批I登记的33个scratch库、105个真实Redis key、8个物理bucket、192个监听读回全部不存在。私有miniredis以其监听关闭证明资源消失；共享worker的TASKEXE是借用资源，早期只读收集脚本曾错误混入该key，两次raw1保留，修正endpoint归属后通过，未执行删除。所有者及I的精确worktree/ref/admin/cache清理由 `coordination/I-cleanup.json` 记录。

main 的原28项未提交WIP保留且暂存区为空；C的6个重叠路径经三方/精确两hunk正反字节证明保留原改动。后续每次接收核对28个postProtected hash。测试绑定已提交隔离组合，未提交WIP不包含在验收SHA中；不能将“保留WIP”写成全部工作文件最终hash与最初相同。

生产服务、真实供应商密钥/公开回调TLS代理、完整桌面/跨平台、OTLP collector和远程 `ci-required` 均未由这些局部证据建立；没有 push 或部署。

证据入口：`scope.json`、`assignments.json`、`handoffs/{A,C,F,M,P,R,S,W}.json`、`coordination/I-intake*.json`、`coordination/I-consumer-pin-decision.json`；完整408行和输入文件hash在`coordination/I-item-ledger.json`。这些路径均相对于本批验证根，详细本机绝对路径已保留在JSON中。
