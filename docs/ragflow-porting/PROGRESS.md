# RAGFlow 逐提交跟进记录

本记录只写单次提交的处理结论。稳定路径映射见
[RAGFLOW_PORTING_MAP](../enterprise-identity-mcp/RAGFLOW_PORTING_MAP.md)；后续提交按各自任务处理。

## c81081f8ef1f805fcc44642f35c78c61709dae9e · 文档 PATCH parser/Pipeline 整项接受

2026-10-03 后端修后范围提交 `d1177243ef6c3cdcbbe2728ddc6c1665560eede6`
（父提交 `b6e413e995997b77fd3244428c4f8ccfe382ef21`，16 归属路径）已根接受，未 push。
复用已接受的配置/模式纯模块与 SDK 字段组件。API、SDK 与 Web document PATCH
真实消费和旧 parser 独立退出均已根接受。2026-10-04 最终审结时，
冻结十项整项完成数为 **10/10**；本冻结批次完成，不开启下一批。

- document PATCH 使用可信 JWT/API Key、严格字段提供语义、刷新后的完整 Document
  和端点局部 typed/numeric 错误。真实模式变化一次 reset/save；同模式、仅配置和非 parser
  更新不 reset，保存不入队。合法保存仍可更新时间或行版本，不要求它完全不写 SQL。
- DataFlow 按 KB owner、类别、有效权限、当前 SQL DSL 的 File 入口及组件参数预检。
  保全存储中的未知配置、RAPTOR/Graph/metadata、源 File/对象和未请求变更的 enabled；
  旧 change_parser 现已退出；独立退出验收见下方小节。
- source 原生写入前独立保存 SQL 权威恢复材料，Redis 为同 nonce 镜像。
  Doc→Task→image 锁序和完整实际原生行核对保全 SQL 未变化的后来 native winner；
  unknown 阻止同 Task 隐式重放，确认退休后按精确 pair/nonce/wire 清理。
  chunk 整批 ID/doc_id 错配在写前拒绝，实际更新也限定 id/doc_id。
  恢复表迁移核验兼容唯一约束/索引；非空 downgrade 先锁后拒删材料，未执行生产迁移。
- 最终固定输入上 `make verify` **4297 passed**、`REQUIRE_SERVICES=1 make integration`
  **691 passed、零 skip**，自有 listener smoke **exit=0**。
  81 parser 场、115 HTTP 事件、8 schema 场及后来赢家、首次 Redis 拒写/回复丢失、
  exact sameDoc SQL authority/Redis wire/DUMP/nonce、真实 PG 阻塞和同 Doc 双 Task 退休材料
  经独立源码/raw审查及根复核。完整业务体、SQL 生产映射列、native payload/全部向量、
  object bytes 和 Redis/queue 实际材料共同支持结论。
- 已登记精确资源及遗漏 runtime keys 经根独立 fresh 读回均无残留。
  当时 1481 输入、16 gate/commit/current 路径、14 保留路径和941当前材料稳定；
  index 空、原25无关改动保留。这是接受窗口，不声明后续外部修改仍全仓稳定。
- 部分逐场完整 HTTP headers/网络原字节、物理 schema/xmin、历史创建前登记与
  PID OS-start/children 捕获不完整。SQL intent/Redis applied 的连续观察不是原子快照；
  静止后的后来赢家材料一致。旧失败、fixture 归因和 wire 仅 hash 的当时界限保留，
  本轮通过与当前 absence 不倒补历史。未声称全仓 clean、生产 provider、worker DONE/ACK
  或 Web 整项消费通过。

SDK document PATCH 本地范围提交 `ed38de7f4f5ec58dc0d8c4375d2bc9c473b924c8`
（父 `bdd3b7e9`，五个归属路径）现已根接受，未 push。局部业务码检查在绑定
Document 前拒绝 HTTP 200 非零数值 code；async/sync 同行为，原 transport、重试及
mutation 不自动重放语义保持。Python 3.12/3.13 SDK 门禁各 **151 passed**，
SDK 仓库 verify **2956 passed**；真实四测试 **50 PATCH、4 ingest、零 skip**。
四个实际 listener 各 623 backend/10 SDK 已加载模块及 12989 shared dependency 文件
与私有接受运行时/当前29 SDK输入相等；306保存材料、完整物理73表列/xmin/schema、
native双向量、object bytes、Redis/queue经独立审查与根核验。
20场认证/写前拒绝零副作用、全部 PATCH queue保全；同模式只允许精确 targetDoc
时间/行版本及所属KB行版本变化。409后来Task winner、首次存储负确认后的实际恢复、
unknown同Doc Redis journal与独立读回后显式reconcile均有完整实际材料。
Pipeline/builtin ingest验证Task digest和queue分支，不声明 worker DONE。
登记的5 DB、11 buckets/60 objects、44 collections、125 Redis DB1 keys及11队列、
10 ports、5 PID和61私有/临时路径经根独立fresh读回均absent。
200非零业务码反例为正式受控transport；fault为明确边界注入。历史失败、早期薄SQL
审计、框架目录run后登记、资源下载产生的非加载文件差量和children捕获界限均保留；
本范围未扩大确认相邻SDK接口的一般兼容性。

### Web parser PATCH 消费接受

Web 提交 `52bacb65cd01f649b473760bd8cef7ade5c76747` 的25路径范围已根接受。
四条正式测试 lane 共 **1095 passed、零 skip**；build、类型、lint、大小和 bundle 门禁实际通过。
纯提交投影另完成 build 与15个 modal 场景，不能把 stacked tree 六门禁计为纯提交投影结果。
280个真实 HTTP 记录、17次原生 PATCH 及67组完整 stores 支持配置保存、切换、拒绝、恢复
与后来 revision winner 保全；73张物理表及映射列/xmin、完整向量、对象字节和 Redis 原值已审结。
真实浏览器草稿、权限目录、迟到响应和重新读回闭环成立；解析提交仅证明 Task/queue，未声明 worker DONE。
原失败、受控故障、创建后登记和 children 捕获限制保留；浏览器收尾采用保存的完整材料，
独立 native fresh 不冒充重新连接浏览器。证据入口：`/tmp/multirag-c810-frontend-root-accepted.md`。

### 旧 parser 入口独立退出接受

六路径范围提交 `0e8fd6ee0217ceec3e438ff2141a6b26def3f183` 已根接受，未 push。
实际父提交 `b31fa75c2ca5e7f7638f7f75611ef775abc371dc` 包含并行工作；正式验收使用
已接受的 immutable `79637de57245e4537f9b69c4fdd1c58d97e1e630` 加精确六路径补丁。
删除旧 POST handler、专用请求模型与独占导入，共享 parser/source/status/image 服务保留。

同固定输入实际 `make verify` **4466 passed**、`REQUIRE_SERVICES=1 make integration`
**699 passed、零 skip、exit=0**；六份自有 listener smoke 均 **exit=0**。
39 个旧请求逐项核对实际 method/URL/path/query/body bytes/headers 与 request ID，
均遵循公开 global404 合同；HEAD 空 bytes，CORS preflight 按现有 middleware 单独分类。
旧 path/专用 schema 不再出现在 OpenAPI，private calls 为0。
三组完整 mapped SQL、74 个实际 physical 表结构/全列/xmin、native 双768向量与 payload、
对象完整 bytes、Redis DUMP/type/value/stream/group/consumer/PEL 支持零写结论；
只允许每个 exact Redis 相对年龄按该组真实 elapsed 自然推进。

117 项 canonical PATCH 的完整前后存储、886 条 wire、55 个完整 fresh Document 与
独立 SQL/metadata/list-by-ID 读回已消费。新 PATCH 的字段提供/空值/配置/Pipeline/单次
reset、不入队及权限语义保留；旧 before_commit/after_commit 和 generic 故障回归已迁移。
真实 lost-COMMIT、unknown/failed journal、完整原始材料与类型解码、显式 retry、
后来的 File/metadata/document b/native/image/queue winner 保全均已核实。
有效用户自己的 KB 仍按真实 owner 条件处理，不按 normal 标签拒绝；
合法 foreign dataset owner API key 仅写其真实所属目标，普通非 owner/admin 不写他人 KB。

五个 attempt 的失败原材料和 exact ledger 保留；自有 scratch 数据库、集合、对象、Redis、
PID/临时路径与归档目录经独立新连接/OS 读回核实无残留。最初一次521地址的 lsof
因100地址上限失败，原失败与派生断言保留并撤销；后来六批独立原返回覆盖全部 exact ports。
原 native/PID/path 窗口与后来 port 窗口分别绑定，不能倒补 precreation/all-lifetime 证明。

本项运行真实 scratch SQL/Milvus/MinIO/Redis 与进程内 uvicorn，模型/fault fixtures 受控。
physical SQL 含列/PK/FK/index/xmin，未整体保存 trigger/function/constraint DDL；
Redis PEL reader 上限100，实际旧 stream 一条 entry/consumer/PEL。两处并发 SourceRecovery
mapped intent 与随后 physical applied 原值不同，属于两个连接的非原子观察，完整原值保留。
headers 是实际 parsed maps；完整 body/object bytes 已解码。实际取消排空用例返回 plain500，
其 SQL 完成及 recovery key/queue 为空已读回，不称 typed 业务成功。
PID/children 登记为 postallocation，第三方依赖按完整安装版本/lock 绑定，不声称全部文件 hashes。
独立 source/raw/fresh 审查及根完整消费均已完成；其他任务的代码、文档和未提交改动保留，
未将其共享门禁、外部任务或生产 provider/worker/browser 纳入本项结论。

499 oldrun、c511 oldbinary 和 c810 oldparser 均已独立退出并完成根接受，冻结整批关闭。

公开合同见 [HTTP API](../references/http_api_reference.md#更新文档解析配置)；
实现和正式回归见 [parser service](../../api/db/services/document_parser_service.py)、
[source recovery](../../api/db/services/document_source_recovery.py)、
[恢复表迁移](../../configs/alembic/versions/a9c810f1d2e3_add_document_source_recovery.py)、
[parser HTTP 回归](../../tests/integration/test_document_parser_update.py) 和
[source recovery 回归](../../tests/integration/test_document_source_recovery.py)。

## c5116b90e5399d0eb6cf473a100e2467b44ca5ed · 图片读取 HTTP 接线

2026-10-03 后端范围提交 `4a4604c43c784ffe0d8a1933d1d5a6dac3c6e4e2`（6 路径）
完成根审结，未 push。复用已接受的5个 image service/storage 文件，没有改写组件；
Agent Hub、完整 Web 消费及旧 binary 独立退出均已另行根接受，whole c511 已关闭。
下列后端 HTTP 材料保留其原接受窗口，不能当作后续所有改动的门禁结果。

- 三个可信异步读取接口提供 thumbnails、知识库登记图片和 owner runtime 附件；局部
  adapter 覆盖依赖鉴权、query422及安全 typed errors，raw raster MIME、no-store/nosniff
  和已解码 slash 保持。三个实际列表 producer 返回的新 URL 已经真实 listener 取回。
- 此次后端 HTTP 接线窗口已删除无活动消费者的旧 thumbnails JSON 并验证
  routing404/OpenAPI absence；当时旧 binary 仍有 Web 消费者，保留 deprecated 行为。
  后来的完整消费者迁移与独立 binary 退出见下文。
- 最终固定输入 `make verify` **4224 passed**、`REQUIRE_SERVICES=1 make integration`
  **602 passed、无 skip**，自有 listener smoke **exit0**。三次 HTTP fixture/路由/旧协议
  断言失败留存并有修后回归，未修改认证、共享配置或放宽门禁。
- 独立 raw 核查110请求记录、10表完整 SQL、4索引 schema/payload/双768向量、35对象
  完整字节与 Redis stream 前后无写；正常 credential、runtime sidecar、特殊 key、
  故障和旧 binary 兼容明确分界。部分请求 raw 未保存 params/producer method，runtime
  upload 只保存 descriptor 与物理 sidecar；相应用例由固定源码实际执行断言绑定，不补称
  所有 wire 均逐项完整归档。listener lifespan off；不称生产 daemon、worker/provider、Go
  或 Web/AH live 已验，denied/read/close/index fault 为受控 transport 回归。
- 根绑定6个提交 blob、1470源码输入、静态 review 与5组件均无漂移，index 空、原25保留。
  fresh cleanup 219 manifests、7 PG 库、741 Redis keys、205 collections、113 buckets、
  689唯一 bucket/object 对和138 ports 无自有残留；原1088对象计数含399重复登记，
  补核一项原漏 runtime key。PID 无 starttime、privatefile/container 仅声明，锁核查限
  已登记单整数 PostgreSQL advisory keys；不构造完整历史生命周期证据。

本机证据入口：`/tmp/multirag-c511-http-root-accepted.md`、`final-report.md`、
`final-evidence-index.json`、`root-scope-bind.json`、`root-fresh-cleanup.json`、
`root-runtime-cleanup.json`（后五项使用 `/tmp/multirag-c511-http-` 前缀），独立报告
`/tmp/multirag-c511-api-wiring-code-review.md` 与 `/tmp/multirag-c511-http-evidence-review.md`。
公开合同见 [HTTP API](../references/http_api_reference.md#读取缩略图和图片)。

### Web 图片核心范围接受

2026-10-03 Web 提交 `0da15321d033fca85fe00db03ef0d13d7184fa67`（父提交 `66f3424`）
的36路径核心范围已根接受。最终16路径修复差量、独立完整 raw、源码/门禁/提交绑定及
fresh 精确清理已审结；954 CI tests、API176/affected62 和11项正式命令实际通过。
认证、完整 binary/native decode、迟到响应、文档/知识库归属和 GET 全 stores 零写有实际材料。

浏览器初始模型失败后使用捕获的真实请求与另一实际上传 file ID，经正常认证 completion
重放保存并 reload；不称原 SSE 成功、真实 provider 读图或 worker DONE。完整原生/浏览器材料
与历史登记、逐 tab/session 捕获不足的边界保留，未补造历史 raw。
此处为核心范围的历史接受窗口；后续 Agent history 类型与认证保存/reload 闭环已另行接受。

### Agent history 图片闭环与旧 binary 退出

后端三路径 `cebf5e2010e21c55b22d59c1efd76f6df22d288e` 保留来源中已有的
`doc_type/doc_type_kwd`，不从 image_id 猜类型。verify **4340 passed**、服务集成
**702 passed、零 skip** 与自有 listener smoke 实际通过；两种生产消息保存/get/list及
完整图片读回和两种 setup 故障收尾已审结。原14表材料为完整 ORM 映射列，未导出其物理 DDL/xmin。

Web 两路径 `fd287dadf7000d0d7e8ff87419e04ccf8be4f439` 完成认证 Agent 消息保存、
history reload、图片预览与独立引用详情，保全切换/退出后的迟到响应和 Blob 收尾。
正式四 lane **1057 passed、零 skip**，30次真实图片 GET、原生页面解码及7组完整 stores
已审结。参考 producer 受控；不称真实 provider 读图、worker DONE 或任意未知历史兼容。
独立浏览器 provider 不可用时，收尾结论来自完整已保存的 tab/storage/Blob 材料。

旧 binary 退出提交 `79637de57245e4537f9b69c4fdd1c58d97e1e630` 只含三个指定文件，
在已接受 `2b2581b7` 加本项精确 patch 上完成 verify **4438 passed**、服务集成
**698 passed、零 skip** 与自有 listener smoke；均有实际 exit0。
28个旧请求验证 global routing404（HEAD 空 body）、OpenAPI absence、private guards0
与三个独立组的完整存储零写；新接口25个完整 raster及三类 URL producer 保全。
74张物理表结构 metadata/全列/xmin、全部原生向量、完整对象字节和 Redis 原值支持旧入口零写；
未保存完整 literal CREATE/check/trigger DDL，新 GET 保全另限10张映射表和 SQL 写 guard。
新连接逐项核查全部尝试资源，并确认隔离 checkout 已归档且不存在；两处复用的 pytest
路径只证明旧登记身份已替换，不称所有 pathname absent，也不清当前目录。
历史创建前登记、进程 children 和 wire helper 字节绑定不足如实保留，当前 absence 不倒补历史。

独立源码/raw/fresh 审查及根完整消费均已完成，结合 Web/AH/新接口关闭 whole c511。
证据入口：`/tmp/multirag-c511-agent-history-type-root-accepted.md`、
`/tmp/multirag-c511-frontend-agent-history-root-accepted.md`、
`/tmp/multirag-c511-oldbinary-exit-root-accepted.md`。

### Agent Hub 关联真实消费验收

2026-10-03 独立 Agent Hub scope `62ade9d0b7b58fe4e19630c72343c14e4efb1f73`
（4个 Go 路径）已根接受。真实 Gin/JWT/GuestGuard/角色、上游 API Key、SQL/index/MinIO/Redis
与当前生产构建浏览器链完成43 gateway、2 direct-auth、11组完整 stores/MySQL前后零写；
复合 key、完整 raster bytes/MIME、安全头、页面解码/安全失败/重试/blob revoke 有原始材料。
当前717源文件及实际11上游输入绑定；full Go/race分别1141/728条 top-level PASS，frontend
611 tests/92 files及fmt/vet/build/tsc/lint实际exit0、无skip，终止race与六轮失败资源记录保留。
根fresh exact union 6 PG库、24 collections、6 buckets/103对象、97 keys、29 PIDs、15 ports、
13 private paths及自有container/volume/workdir无残留。重复Vary头的raw合并/快照最后值差异、
历史SQL ID/namespace/OSstarttime/private写前登记及browser关闭捕获不足如实保留；不补称
生产daemon/worker、模型质量或512 sniff后的所有stream均完整。没有新功能、持久写或漏清阻断。
独立代码/raw报告及根绑定见 `/tmp/multirag-c511-agenthub-live-root-accepted.md`。
此处只记录 Agent Hub 独立范围；whole c511 后来由完整 Web 消费和旧 binary 退出共同关闭。

## 49912a156e3fb072d3e897b4f107e4ccae52fb15 · 文档 ingest 后端验收

2026-10-03 后端范围提交 `7faf1dcb3974239a472822602a880dcbd95eb549`（30 路径）
完成根审结，未 push。关联 Web 迁移与旧 run 独立退出均已另行根接受，whole499 已关闭。
以下后端材料保留原接受窗口；其他任务改动按各自范围处理。

- 新全局 POST `/api/v1/documents/ingest` 采用可信异步 Principal、全请求预检、
  严格 run=0/1/2、delete/apply_kb 布尔值和完整逐文档结果。保留 canonical
  parse/stop 默认，仅三项知识库 metadata 设置继承；源对象和其余配置保持。
- 真实 SQL/index acknowledgement、Doc/KB ledger、部分排队、失败补偿和公开重试
  按持久恢复材料及实际归属协调。旧 worker 的 insert/count/finally/rollback/image
  与 Pipeline 写入受 Task 世代约束；保留 TOC 排空及母块隐藏。SYNC admin 校验
  本次提交 ID 的完整 DONE 读回，业务失败、空页和缺失 ID 不冒充完成。
- 最终固定输入上 `make verify` **4182 passed**，`REQUIRE_SERVICES=1 make integration`
  **601 passed、无 skip**，自有真实 listener 的 smoke **exit=0**。此前 metadata、
  TOC 假件、换代 fixture 和 canonical 错误断言失败均保留原日志并有修后回归。
- 独立审查覆盖 generation、补偿和 API/admin 修复差量；原始证据复核包含 224 份
  完整存储快照、115 个真实请求记录及所选 Milvus/PostgreSQL/Redis/MinIO 链路。
  ES/OS 延迟 transport 与 OB/seekdb/VastBase adapter 为受控回归，未声明远程服务、
  生产 daemon/provider 或浏览器验收。部分早期 auth/422/canonical 请求只有正式测试
  断言和存储证据，未逐一归档 wire JSON；不补称已保存。
- 根绑定 30 提交路径、1452 份固定源码/配置输入及原无关改动，无漂移、index 空。
  fresh exact cleanup 核查 543 manifests、31 PG 库、4355 Redis keys、1171 collections、
  634 buckets、629 ports 和 219 advisory keys 均无自有残留；补核两项未列入原审计
  的 runtime Redis keys。211 份旧 manifest 没有 PID，既有 PID 无 starttime，故只证明
  已登记进程检查与端口结果，不声明完整历史后代进程生命周期记录。

本机证据入口：`/tmp/multirag-49912a-root-accepted.md`、`final-report.md`、
`final-evidence-index.json`、`root-scope-bind.json`、`root-fresh-cleanup.json`（后四项
均使用 `/tmp/multirag-49912a-` 前缀）；独立报告为
`/tmp/multirag-499-evidence-delivery-review.md` 和 `/tmp/multirag-499-api-final-delta-review.md`。
稳定公开合同见 [HTTP API](../references/http_api_reference.md#批量提交取消或重置文档解析)。
旧 `/v1/document/run` 已独立移除，退役实现 `a10f0b72b33093f8020ed5e20210bd4aa823529f`
与合法 admin/API Key 目标夹具修正 `2b2581b7a76cdf2ca0483c1aac24f3b8fabbd53b` 已根接受。
最终 verify **4402 passed**、服务集成 **698 passed、零 skip** 与自有 listener smoke 实际 exit0；
37个退役请求的完整业务404、private calls0、OpenAPI absence、独立 stores 零写及
canonical/admin 保全已审结。固定源码/配置、实际加载、运行读回与正常局部提交完整绑定；
全部尝试资源经新连接独立核查，无自有残留。原 HTTP 解析体/headers 已保存，但未逐项
保存 response.content；SQL 为完整映射列，未包含物理 DDL/xmin/未映射列。自然 stream
年龄仅按精确字段与 elapsed 界限比较，历史登记和进程身份不足不倒补。
证据入口：`/tmp/multirag-499-oldrun-exit-root-accepted.md`。本项未新增 MultiRAG Go ingestion 接口。

### 499 关联 Web 最终验收

Web组合 `093b681f436ad1544090a216bf2475d76d8d0ba6` 与最后焦点差量
`66f342441a5108f19cc4699f77775a6dfb092719` 已根接受。原25路径包括严格raw response、
canonical parse/stop、显式ingest两delete分支/apply选项、完整partial与失败选择/选项、cache/late/busy；
原固定输入896 CI、19 HTTP/26完整stores及独立代码/raw审查均已消费。最后仅两路径复用现有Radix
焦点约束，16 mounted回归、完整tsc-b/Vite build、局部lint/size/budget实际通过，不把历史896计入新检查。
真实native keyboard补齐busy trap、单次提交、及时业务失败后保false选项/选择并显式retry；
新完整SQL71表/index双768/schema/create/payload/source bytes/Redis DUMP/XRANGE读回、storage0与完整tabs[]
已独立核实。源/门禁/提交25feature hashes相同，根fresh新DB/collection/bucket对象/6keys/2PIDports/4private全absent。
前两browser超时、已解锁的旧意图标签、Milvus异步stats、成功移除opener后BODY、旧关闭原始捕获不足仍保留；
新keyboard listener期间后端5路径合法变化只归已启动实例，两初始dirty文件没有精确bytes备份。
成功为queued Task/queue0→1，不称workerDONE或替新parser后端最终输入验收。根与独立最后差量报告入口
`/tmp/multirag-499-frontend-root-accepted.md`；后续旧 run 独立退出已接受，whole499 已完成。

## 965717c4fbbcaece97cd68ff08c12174cec60a04 · Google provider 与模型推理默认值

2026-10-02 完整评估目标 15 路径，沿用既有冻结来源 `519e7d98`，未 fetch 新批。
Go 范围提交 `27da32e667a9a73327467f77203fef1e54a9ddee`（21 路径），必要 Python
适配提交 `744dad08f136255fbb875ad64f81f08392275f98`（2 路径）；执行者分别提交，未 push。

- Google 文本聊天、sender SSE、历史角色、所有分页模型列表和连接检查使用真实
  `google.golang.org/genai v1.54.0`，支持 region/default BaseURL、请求取消和安全错误。
  空候选/空内容不是成功；reasoning 先于 answer，只有实际答案成功后发送完成帧。
  不支持的 Google embedding、余额及旧 channel-only streaming 明确返回错误。
- 七个 Go 模型 JSON 完整适配模型级 thinking。保留本地 URL/tag/class，MiniMax
  实际 m2 推理能力迁至各模型；未改的 Aliyun 保留旧 provider 默认。
  类型字段采用 `clear_thinking`，现有公开 feature-map `clear_content` 保持。
  handler 显式 `thinking=false` 优先于模型默认，SQL 错误不冒充 disabled-row 不存在。
- Python 读取 `llm_factories.json`，七个 Go JSON 不自动生效；已有 Gemini/Vertex
  注册继续使用。两个活动 LiteLLM async loop 修复同 delta 的 reasoning 与 answer
  丢失，tools 累积 answer，原隐藏推理、wrapper、token 和错误合同保持。

修后 Go build/vet、models race、targeted 和三个独立 cmd main 构建均 exit0；采用
仓库 Go1.25.14 与当前原生 tokenizer，overlay 只定位自有编译产物，不替换行为。
Go1.27.1 在未修改 grpc/Milvus 的既有编译问题如实保留，未借此升级无关依赖。
真实 Gin/service/GORM/PostgreSQL/genai HTTP 验证成功、分页、错误、SQL 状态及取消；
认证和 provider 为受控 fixture，不称远程 Google、生产身份或 Web 对等验收。

Python 正式专项 13 passed，实际 LiteLLM 七次 HTTP 及旧源码失败对照有效：答案原来
丢失，修后保留。wire usage 未进入当前 LiteLLM，原新两版 fallback 均为 1，生产计量
无变化。最后修改后 `make verify` **3830 passed**，`REQUIRE_SERVICES=1 make integration`
**526 passed、无 skip**，自有 FastAPI listener smoke 均 exit0；专项不叠加全量。
1444 输入门禁快照在当时无漂移，后续 API 修改不归入本次冻结版本。

独立 Python fresh 33 ownership 清单资源全部清理。Go 原清理未保存精确 DB/端口，
因此仅重放一次现有正式 live test，加 `/tmp` manifest 插桩且保留全部断言；23 正式
文件 hash 不变，完整独立 SQL 行读回和根 fresh 连接确认精确 scratch DB、两个端口、
两个 PID/private 文件均不存在或关闭。代码与原始证据独立审查无阻断；根核契约、
scope/hash、实际清理与消费者边界后审结。原始证据索引为本机 `/tmp/multirag-965717-`。

### 343bda 关联 Web 最终验收

Web `64378b1d` 清理两个死入口路径，`d1b55f392c50013d681770188cbeecfc999d9ed8`
完成实际 composer 附件所有权修复。生产 hook 管理上传、取消、发送资格和迟到保护，
两页直接渲染其列表；vendor 仅选择文件，上传中仍可移除。Explore 保留已有 retry，
MCP 不新增 retry。file-size 只收紧，没有放宽；当前 Web 工作区/index 为空，未 push。

最后 Explore File UID 修正后完整门禁再次通过：**871 = Node554 + Vitest226 + Desktop81 + Tooling10**，
inventory141、零 skip；lint0error/1497warning、build/filesize/bundle exit0。
独立组件专项22通过，不叠加全量。最终两页实际成功发送、失败、移除、取消迟到、预览，
Explore 重试、真实401与旧404均有 API/模型输入及完整存储读回。MCP 矩阵先于最后仅
影响 Explore 的修改，Explore 受影响行为已再验；不称两页全矩阵全部重新执行。

两库完整 SQL、4D Infinity payload/向量/关系、五历史对象 bytes/queue 保持；22 新原始
附件与22可信描述一一对应，登记失败无孤儿对象。client abort 不代表服务端删已登记对象。
原 cleanup exit1 发生在资源清理断言完成后的跨 owner Git 检查；逐表 counts 未持久保存，
如实保留该限制。独立 fresh 核四精确容器、三卷、private 文件 absent、六端口 closed，
成功浏览器 storage0/tab关闭；外部 runtime 保留。模型受控，无完整 worker/远程 provider
或 Go 附件对等声明。独立代码及原始证据复核无阻断，合用后端结论，343 整项审结。

## 872ff0830451f4b3a02edf9b715115bfb010db06 · TOC 线程生命周期

2026-10-02 完整核对目标的一条 `executor.shutdown(wait=False)` 改动，并沿用冻结
`origin/main=519e7d98a5651564d4e35d6648f006cba4baaf4f`，本轮未 fetch。本地原线程池
在取消、解析空结果等提前退出之前创建，TOC 提交和 schema 获取也在旧 finally 之外，
因此采用完整生命周期的最小适配。相关后修 `cc207b5b05532f6296e72bbe01e9813ae0ead7e1`
只作生命周期参考，没有扩大解析架构或改变 TOC 输出合同。

- 仅真正需要 TOC 时，在受保护的 try 内创建本任务线程池；普通任务和提交前的提前退出不建池。
- 用 `wrap_future` 与异步 shield 等待保留 TOC 和普通索引写入并行，事件循环继续响应。
- 所有提交后成功、早退、异常和取消出口先停止接收任务、取消待运行工作，再于事件循环外
  join 自有线程并取回 future 结果；重复取消沿用现有排空 helper。未消费的 TOC 异常记录到日志。
  已运行线程等待其结束，不能把 `wait=False` 或 future 取消称为强制停止线程。
- 线程收尾先于索引取消清理、session 释放、worker 终态和消息 ack；共享线程池保持可用。
  `insert_chunks`、最新 SQL source 状态继承、旧/新母块隐藏与其他 35 个顶层函数/类 AST 不变。
  没有改 HTTP、模型/provider、对象、任务队列合同；Go/Web 本目标准确为不适用。

### 本轮验证与根审结

正式新增 22 个生命周期用例使用真实线程池，覆盖提交前出口、成功、schema/索引/计数/TOC
失败、业务取消、重复异步取消及 ack/终态顺序；确认线程已结束、关闭后拒绝提交和共享池可用。
相关定向 **32 passed、exit=0**，不叠加进全量。所有正式可执行修改之后：
`make verify` 为 Ruff、8 import contracts、async DB、mypy 127 文件及 **3817 unit passed、exit=0**；
`REQUIRE_SERVICES=1 make integration` 为 **526 passed、无 skip、exit=0**。
1434 文件门禁快照无漂移，根在释放下一 Python 写窗口之前重新核过；后续 Google 修改不属于本项旧快照。

真实隔离运行 **1 passed、exit=0**，走 FileService 上传、真实 HTTP parse、SQL Task/Redis
入队、实际 collect/worker/build_chunks/build_TOC 与索引写入。TOC 等待期间通过真实 HTTP
禁用文档，随后 TOC 继承最新 SQL 状态 0；完整七表 SQL、三条 payload/768 维向量、创建字段、
母子引用、TOC 关联、原对象 41 bytes、Task progress=1、Redis pending=0 和自有线程退出均读回。
再启用仅改变普通/TOC 块的可用状态，母块仍隐藏，其余索引字段及对象字节不变。
parser/model 输出受控，不称生产 worker daemon、远程 provider 或浏览器验收。

原始报告和日志位于本机 `/tmp/multirag-872ff-` 前缀。初始测试夹具与 live 准备失败均已纠正，
没有削弱行为断言。独立审查以新连接只读复核 4 次 live 和 32 份完整 integration ownership
记录：精确 scratch 数据库、bucket、collection、stream/cache 与端口均不存在/关闭，记录的
自有 SQL 行为零。只清本项资源，保留原 25 项无关改动、其他并行 owner 改动及共享服务。
根完整核源码/正式测试、原始本轮门禁、业务码、完整读回与清理，独立审查无阻断；未 push。

## a536980e2 · 旧文档状态接口条件退出

2026-10-02 从本仓 `2f46a4407e04862555ba0fb20914761fc3fb6497` 开始，完成此前
约定的退出续作。Python/Go 新启停合同已验收；独立 Web `66bafd85` 已将单条/批量
操作迁至 dataset REST 并通过真实请求与存储读回。当前 Web、两套 SDK 实际客户端、
MCP/Agent、worker 和 Go 没有旧 `change_status` 的活动调用，因此退出条件已满足。
本轮沿用已审结的冻结基准，不新增上游任务或重新实现原启停功能。

| 范围 | 本项处置 |
|---|---|
| 旧入口与模型 | 删除 `POST /v1/document/change_status`、`ChangeStatusRequest` 及独占导入；OpenAPI operation/schema 均不存在 |
| 共享状态与新入口 | 保留 dataset batch、PATCH enabled、`document_status_service` 与共享 source 写入；其他生产函数/模型 AST 完全一致 |
| 旧失败回归 | 转到活动 REST batch handler，保留严格 body、合法 dataset/Principal、完整 partial data、安全错误、SQL 不写及索引恢复 `[0,1]` |
| async DB baseline | 当前没有旧 change_status 条目，不改其他条目 |
| Web / Go | 本轮没有改动；Go 原本没有旧 route，退役验证不另造 Go 能力或重复其已审结门禁 |

### 本轮实际验收与门禁

- 定向 unit **40 passed、exit=0**；实际状态 integration **25 passed、exit=0**。
- 同一真实 FastAPI listener 的旧 POST 矩阵为 6 类凭据 × 5 类 body，共 **30 次请求**：
  无凭据、有效 owner JWT/API key、另一用户有效 API key、错误 token 和真实过期 JWT；
  空 body、doc_id、doc_ids 数组/字符串与非法状态均返回完整统一 HTTP/code 404、data=null。
- 新 SQL 连接读取七张完整表，独立 Strong Milvus client 读取完整 payload/vector/时间戳，
  原始 MinIO bytes 和精确私有 Redis stream 均 before/after 相等；旧入口执行守卫没有触发。
  随后的活动 batch owner JWT 启用成功，OpenAPI 保留新 POST；同 listener smoke 成功。
- 本轮最终 `make verify`：Ruff、8 import contracts、async DB、mypy 127 文件与
  **3795 unit passed、exit=0**；`REQUIRE_SERVICES=1 make integration` 本轮
  **526 passed、无 skip、exit=0**，最后可执行修改之后运行，Python 源码快照保持一致。
- 最终运行中三条验收各对自己的真实 listener 执行 smoke，均 exit=0、健康端点 HTTP 200。两次状态验收的
  完整读回独立复核相等（共 60 次旧 POST）；两次运行共 57 份精确 ownership manifest。
  57 个 bucket、54 个集合、55 个 Redis key 和 57 个端口独立查为不存在/关闭，
  两个精确 scratch 库均不存在，owned SQL 行数为零。只清本项资源，共享服务保留。

保留新 batch/PATCH 的严格校验、owner/admin 权限、禁用/启用、同状态修复、partial、
跨 dataset 隔离、SQL commit 补偿、并发排空、零分块最新 source 状态继承及旧/新母块
保护回归。实际基盘为 PostgreSQL/Milvus/MinIO/Redis；Infinity 失败与 ES/OS transport
仍采用既有受控回归，不把本轮称为完整 worker/provider 或真实 ES/OS server 验收。
资源只按本次精确 ownership 清理；保留原 25 项无关改动、并行 owner 的独占改动及共享服务。
独立旧 `/v1/document/status` 的 Web 死定义不属于本项范围，没有声称其为活动页面调用。

## 343bda11193dd9d236c3a07f8a8ca8e2809a2517 · 退役会话解析入库

2026-10-02 从本仓 `27665d9bb6193ca7825d3f1e69bd167056c48a7a` 开始，按根冻结队列
沿用预期 upstream remote 与 `origin/main=519e7d98a5651564d4e35d6648f006cba4baaf4f`，
本轮未 fetch。完整目标 diff 只有三条 Python 路径、7 增 177 删：旧上传解析 route、专属
helper 和一条旧测试方法。到冻结基准的相关 first-parent 行为历史没有恢复；后续整体
删除 Python/测试不作为该能力的 revert/re-land。

| 目标足迹 | 本项处置 |
|---|---|
| document_app upload_and_parse | 删除 `POST /v1/document/upload_and_parse`，不新建替代入口 |
| document_service doc_upload_and_parse | 删除专属解析入库 helper 与专属线程 Session helper，只清其独占导入；异步 DB baseline 精确移除该 route 一行 |
| 旧测试方法 | 本地旧保留断言改为退役；两条原会话解析/source producer 集成用例转换为真实 404、无执行、完整存储不变回归 |
| Web / SDK / MCP / Go | 目标无 diff。当前 Web 只有死方法定义与专用测试，没有产品调用，独立清理由根后续协调；两个 SDK 实际客户端走 dataset REST。Go 无该 route/helper，不新增并行能力或宣称 Go 等价验收 |

旧入口原来同步创建会话知识库的 Document/File/File2Document、对象、分块、向量和计数，
这项会话入库能力正式退役。运行时 REST/SDK 附件上传、可信描述与聊天提取继续提供，
行为不等同于会话入库；现行合同集中在 [HTTP 参考](../references/http_api_reference.md#上传运行时附件)。
活动 Web 的 Explore/MCPChat 使用附件 hooks 与新 runtime API；后端、MCP/Agent、worker、
两个 SDK client 及 Go 未发现旧入口或两个专属 helper 的活动调用。

保留 FileService upload_document、parse/parse_docs/get_files、dataset 上传/parse/worker、
写作参考、Canvas/聊天共享资料提取、共享 insert_source_chunks，以及 worker/REST/legacy
source 最新状态与旧/新母块保护。没有改 status/run/change_status/web_parse/metadata/parser，
a536 兼容入口退出由根单独协调。AST 对比确认两个生产文件仅删除一个 route、两个 helper，
所有其他函数/模型定义完全一致；re/xxhash/MAXIMUM_PAGE_NUMBER 的其他实际使用保留。

### 本轮实际验收与门禁

- 定向 unit **24 passed**；相关四个 integration 模块 **31 passed、exit=0**。
  两条退役用例各发送 15 次实际请求：无凭据/JWT/API key/错误凭据/过期 JWT，分别空请求、
  错字段、合法 conversation_id + file multipart，均为完整统一 HTTP/code 404、data=null。
- 真实 conversation/dataset、已有 Document/File/File2Document/Task 及对象、带母关系的
  Milvus payload/vector/可用状态和 Redis task 消息作为基盘；新 SQL 连接、独立 Strong
  Milvus client、原始 MinIO bytes 与具体 stream before/after 完全一致。完整读回 JSON 存档，
  parser/provider、SQL DML、object/index/Redis 写入和临时文件执行守卫均未触发，OpenAPI
  operation 与两个 helper 消失。禁用的零分块文档不再由这个已删除 producer 新增 source。
- 正式保留回归实际验证 runtime REST JWT/API key 与 SDK multipart，可信 owner/size/描述与
  独立对象字节；聊天/Canvas/MCP ID 恢复后内容进入捕获模型；dataset parse 真 SQL Task/Redis
  入队；写作参考真实文本解析与 SQL 保存。其余 source/status/mother/补偿/并发测试原断言保留。
  模型输出在既有明确边界受控，不宣称远程模型或完整后台 worker 解析执行。
- 完整 `make verify`：Ruff、8 个 import contracts、async DB、mypy 127 文件、
  **3795 unit passed，exit=0**；`REQUIRE_SERVICES=1 make integration`：
  **526 passed，无 skip，exit=0**。所有本项可执行改动均在这两项本轮门禁前完成。
  同实际 listener 的 `make smoke` 在正式真实 HTTP 用例中执行并断言 exit=0，原始输出存档。
- 自有 scratch DB、owner IDs、bucket/object 名、dataset/document/SQL 关系 IDs、collection、
  stream/cache key、端口与 token 的非秘密行标识均记录；只删除这些自有资源，各 fixture 用
  独立连接核 SQL 为 0，并核 collection、queue/cache、bucket 及 listener 退出。共享服务与
  外部 Go/native 资源保留，未新建容器/修改共享配置，不按通用资源前缀扫删。

本机原始日志在 `/tmp/multirag-343bda-` 前缀：`unit-initial.log`、`http-initial.log`、
`verify-final.log`、`integration-final.log`；最终 ownership/retirement before-after/smoke 存于
`integration-final-evidence/`。独立清理读回 `/tmp/multirag-343bda-cleanup-readback.json`
与验收报告 `/tmp/multirag-343bda-acceptance.md`：两次运行共 63 份自有资源记录，
63 个 bucket、58 个 collection、60 个具体 Redis key/stream 及两个 scratch DB 均不存在；
SQL 自有行均为 0，自有 listener 关闭，两份完整 retirement before/after 一致，清理 exit=0。
保留原有 25 项无关脏改，不 push，不取下一 SHA；完整交付后等根审结。

## d78013964af8044e4d7b761e0bf1e5113a4bfdd1 · 数据集管理 HTTP 回归

2026-10-02 从本仓 `d30c8d3ae863e913e3d90e7c069109f207d757f9` 开始。
核对 upstream remote 为 `git@github.com:infiniflow/ragflow.git`，按根聊天冻结队列沿用
`origin/main=519e7d98a5651564d4e35d6648f006cba4baaf4f`，未改变基准。
目标只有两个 Python HTTP 测试文件、158 新增行，没有生产、Go、Web、SDK 或 MCP diff。
完整 diff 中 metadata 为 10 个方法、鉴权参数化后 13 例；ingestion 只新增一条有效反向日期
范围用例。PR 描述中的缺 dataset ID 未出现在 diff，本仓补充的缺 ID 404 是本地新增。
目标测试路径到该基准只有 `adf2e0c07` 整体删除 Python 测试，不是行为 revert/re-land。

| 目标与本地缺口 | 处置 |
|---|---|
| dataset metadata GET/PUT、document metadata PUT | 复用既有路由、严格请求模型和真实 JWT/API key 鉴权；用隔离 PostgreSQL 与实际 FastAPI listener 补正式 HTTP 回归，不复制上游 harness |
| dataset owner、document owner/admin | 保持现行权限；dataset admin/真实外人拒绝，合法请求体验证不存在与跨 dataset 文档，不用空 body 的 422 替代业务授权检查 |
| 有效日期倒序 | 最小 service 校验，在访问权限检查后按 UTC 规范化输入；from > to 返回 HTTP 200、code=102、安全 message，相等及单边范围合法 |
| metadata 保存清除 RAPTOR | 真实验收暴露通用 parser helper 的省略删除语义；仅 metadata handler 将原 RAPTOR 配置一并传入，保留其余 parser 字段，不改变通用 parser 更新行为 |
| Go / Web / SDK / MCP | 无目标 diff，不新增派工或协议。Go 当前没有 metadata/config 或 ingestions 等价路由/HTTP harness，属于缺能力，不声明已实现或已验收 Go 等价行为 |

稳定输入、响应、日期边界见 [HTTP 合同](../references/http_api_reference.md#元数据模板配置)；
路径映射及兼容入口见 [移植映射](../enterprise-identity-mcp/RAGFLOW_PORTING_MAP.md)。
Web 文档 metadata 的 `withLegacyFallback` 仍消费 `/v1/document/update_metadata_setting`；
FILE_LOGS 仍消费 `/v1/kb/list_pipeline_logs`（非数据集哨兵日志），REST ingestions 只选择
数据集哨兵日志。保留两个活动旧入口，也不改相邻 status/run/upload_and_parse/change_status。

### 本轮验证与资源边界

- 定向单元 **54 passed**；真实 HTTP **26 passed**。涵盖四个端点无/坏凭证、两类有效凭证，
  dataset PUT→GET、document 数组/schema/空模板与 admin，缺 body、缺/跨库资源、外人拒绝；
  日志正序/相等/单边/时区/status/分页、反序、无权 dataset 和跨库 log。
- `make verify`：Ruff、8 条 import contracts、async DB 门禁、mypy 127 文件通过；
  **3795 unit passed，exit=0**。`REQUIRE_SERVICES=1 make integration`：
  **526 passed，无 skip，exit=0**，包含最终 26 项真实 HTTP 回归及同 listener smoke。
- fixture 只覆盖 `get_async_db` 到真实 scratch PostgreSQL；HTTP/auth/service/SQL 未替换。
  每步独立 SQL 读回完整 parser_config 与相关表整行，其他 dataset/document/log 保持不变；
  未创建本项索引、对象、Redis queue/key、worker/provider 或浏览器资源，不冒称解析执行验收。
- 同一实际 listener 中调用 `make smoke` 并断言 exit=0；最终集成运行的原始 smoke 输出另存
  对应 fixture 的 `.smoke.log`，健康检查也使用该 scratch AsyncSession。
- 资源记录只含本项 database、user/dataset/document/log ID、token name 与端口，不含凭证。
  各 fixture 删除后用新 SQL 连接断言七类自有行均为 0，并确认 listener 关闭；suite 的 scratch
  数据库由现有 fixture DROP，最终只按记录的精确库名独立查询，不扫描/删除公共前缀。
- 初轮 token name 超出 varchar(20)、夹具日期被生产插入钩子覆盖及误设 422 信封已修正。
  日期用插入后 SQL 设置，不禁用生产钩子；真实 RAPTOR 回归修复后重新通过全部相关用例。

本机原始证据：`/tmp/multirag-d780-unit-corrected.log`、`http-corrected.log`、
`verify-final.log`、`integration-final.log`（后三个均为 `/tmp/multirag-d780-` 前缀），
清理汇总 `/tmp/multirag-d780-cleanup-readback.json` 与验收报告
`/tmp/multirag-d780-acceptance.md`：三次实际资源运行共 78 份记录，自有行全为 0、
listener 全关闭；三个精确 scratch 库经独立查询均不存在，清理检查 exit=0。
保持原有 25 项无关脏改及并发新增文件，范围提交，
不 push；完成后等待根审结，本项不推进下一 SHA。

## a536980e229d8a28a6fd55077ca575f2983f59c8 · 文档批量启停

2026-10-02 从本仓 `dd451b00353c81923c407e1dec5f21965bdd2f92` 开始。
核对 upstream remote 为 `git@github.com:infiniflow/ragflow.git`，沿用根聊天本轮已刷新
的 `origin/main=519e7d98a5651564d4e35d6648f006cba4baaf4f`，完整核目标十文件 diff。
本项 Python、Go 分别范围提交，不 push；原有 25 项 Channel/config/docker 等无关改动保留。
本项完成后等待根审结，不开始第三项。

| 目标 diff / 必要后修 | 本项处置 |
|---|---|
| `api/apps/document_app.py` 旧 change_status | 当时因活动 Web 暂留 deprecated，新旧共用正确状态服务；兼容 doc_id fallback、doc_ids 单字符串/数组，部分失败映射非零。Web66bafd85 验收后旧 route/model 已在本项条件退出续作删除；未扩大其他入口。 |
| `api/apps/restful_apis/document_api.py` 新 batch status | 唯一正式 POST；FastAPI 严格 body、异步 Principal/AsyncSession，按当前 owner/admin 权限适配；未采用上游仅 owner 限制、同状态直接跳过或忽略索引失败。现 PATCH enabled 只做必要共享接线。相邻 metadata/parser/图像/import 调整不扩纳。 |
| `test_common.py`、`test_document_metadata.py` | 重建本仓状态契约、真实 SQL/索引、权限和故障回归；不复制上游 metadata/parser 等相邻测试或 harness。 |
| 六处 Web diff：use-document-request、dataset-table、use-bulk-operate-dataset、use-dataset-table-columns、knowledge-service、utils/api | 独立 Web 后续66bafd85 已完成新 dataset REST 单/批启停与真实读回验收并根审结；本 Python 续作未修改 Web，旧入口退出条件已满足。 |
| `bed9cc5a4f72aef0e6ae8d02a6a0e10d94909ea6` 五文件 | 完整核对 source availability 后修，映射到本仓 worker 母/主块、会话 doc_upload_and_parse、REST _add_chunk、legacy create 四个实际写点。没有复制不存在的 task_executor_refactor/Go ingestion pipeline。 |
| `5046626c1796ae832b391a2cfd09d716d349b040` 四文件 | 完整核 handler/service/router/test，实际实现 Go handler/router/auth/service/DAO/DI，同合同独立验收；未采用上游 SQL 回退错误被忽略、同状态早退的缺陷。 |

无本项行为 revert/re-land。`f4d36f708` 是 run/run_status 查询过滤，不扩纳；
`670e688726` 整体删除 Python 及后续整体删除 Go 不作为该启停行为撤回。
稳定输入、业务码、部分成功与兼容退出合同见
[HTTP 参考](../references/http_api_reference.md#批量更改文档启用状态)。

### 状态服务和 source 写入

每 distinct ID 独立事务，以真实 Document 行锁串行；status=0 可重新启用，不使用只查
status=1 的 accessible。有效 KB 的 owner/admin 可写，保留 user_id==tenant_id 的 owner
捷径，其余要求有效 membership。索引真实完成后才提交 SQL；同状态请求仍修复索引。
失败回滚后重新锁定当前 SQL 赢家补偿，补偿无法确认返回安全错误；失败恢复事务也释放
行锁后再继续下一项。成功要求真实索引状态达目标，SQL rowcount 必须为 1，缺失/跨库
不会伪成功或暴露 provider 详情。Python 取消会等待已拥有的阻塞写入结束，再释放 Session。

普通 source 写入前，独立 Session 按可信 doc_id 分组、排序行锁并读最新 SQL 状态，锁只跨
实际 store insert，不跨 callbacks/chunk_num 更新；无法读回全部来源时写入前失败。
状态检查还核真实分块数量，覆盖插入已完成而 SQL chunk_num 尚为 0 的窗口。
母块保持 available=0，重新启用不暴露母块；图谱/RAPTOR/compile 特殊产品不统一重写。
会话旧同步解析链在 worker 自有 Session 中完成，避免 async listener 被同步解析阻塞而与
状态行锁互相等待；没有把请求 Session 传入线程。

Milvus availability 更新用 Strong 全行读回和 upsert，不先 delete；保留完整 source payload、
向量及创建字段，真实部分写入可补偿/重试。检索 available=0 用准确相等过滤，不被 falsy
判断跳过。实际 REST source 验收发现现有 VARCHAR tag 字段收到 list/dict，需要按现行
mapping 序列化为 JSON；只补必要存储输入适配，没有扩大 metadata 功能。
跨 SQL/索引使用补偿，不能保证进程崩溃或外部直接写索引时的分布式原子提交。

### Go 合同与实际边界

Go 使用同一个正式 endpoint、严格 0/1 body 和完整 code/message/data 映射。
该路由接受当前 Python HS256 JWT（同签名密钥、有效期、签名 email 查可信活动 SQL 用户）
和既有 Go login/API token；其他路由的鉴权没有更改，保留当前超管/服务可用规则。
实际 listener 使用同 scratch PostgreSQL，三条同时持有的池连接逐条确认 usr_ai search_path。

Infinity 使用真正 SDK count/update 和 dataset namespace；3022 仅按 SDK 结构化错误码
识别缺表，不匹配错误文本。SQL 执行或提交失败后，包含已结束事务的 sql.ErrTxDone，
均重新读取/锁定 SQL 赢家再恢复真实索引。HTTP 断开不先取消 SQL 锁而遗留仍运行的 RPC。
Go ES/Milvus UpdateDataset 没有写入基盘，显式不可用；有分块返回逐文档失败且 SQL 不变。
无表且确无分块可仅更新 SQL，Milvus 有集合却无法计数当前文档时保守拒绝。没有用 nil
写入桩宣称成功，没有扩整套引擎。复跑跨端验收须准备真实 native tokenizer 静态库和
隔离 Infinity 服务，并显式运行 `document_status_go_acceptance.py`；普通 Python CI 不附带 Go 前置。

### 本轮验证、独立读回与清理

- 最终 `make verify`：Ruff、8 条 import contracts、async DB 门禁、mypy 127 文件通过；
  **3737 unit passed**。最终 `REQUIRE_SERVICES=1 make integration`：**498 passed，无 skip**。
- 本项正式 Python integration 23 个用例，包含实际 FastAPI JWT/API key、owner/admin、
  normal/invite/inactive/foreign、无效 body、缺/跨 dataset、重复、0↔1、same-state 修复、
  PATCH、部分结果、缺集合/重试、查后删除、源插入/counter 竞争、取消排空。
  模型/provider 输出及 False/异常/部分 upsert/SQL/恢复失败为注明的控制边界；
  HTTP、SQL、真实 Milvus 及对象写读均未替换。async/sync 恢复失败用独立连接 NOWAIT
  证明行锁已释放；实际 listener 内运行 `make smoke` 通过。
- 四处实际 source 写入证明零分块禁用后按最新状态插入、混合来源分组、母块隐藏；
  会话 provider encode 内用真实 HTTP 禁用新文档，再查实际首次插入及重新启用。
  独立 SQL 整行、独立 Milvus Strong client 全 payload/向量/创建字段及原始 MinIO bytes
  读回，兄弟文档与其他 dataset 不变，available=0/1 检索正确。
- 显式 Go 真实 HTTP 矩阵 **18 passed，无 skip**：Infinity 真写/读、同状态修复、完整数据
  保留、逐项继续，真实 PG 执行失败与 deferred constraint 提交失败补偿、并发及删除竞争。
  ES/Milvus 分支验证明确拒绝和 SQL 不变，不冒称其支持写入；Infinity/Milvus 使用真实
  客户端验证其他无索引 dataset 的零分块 SQL 更新。ES 使用 nil-store service 控制，
  未连接真实 ES client/server。Go 源/向量/创建字段由独立 Python Infinity SDK 读回。
- gofmt、`go build ./internal/...`、`go vet ./internal/...`、handler/router 专项测试通过；
  server/admin/CLI 三个独立 cmd main 各自临时构建通过。复用本机已有真实 Go 1.25.14 与
  native 静态库；m1cpu 依赖仍有 CGO VLA 编译警告，不影响退出码，未放宽门禁。

原始日志在本机 `/tmp/multirag-a536-` 前缀：`verify-complete.log`、
`integration-complete.log`、`http-final.log`（新增恢复锁用例前 21 passed）、
`go-http-final.log`、`go-build-final.log`、`go-vet-final.log`、`go-unit-final.log`、
`go-cmd-{server,admin,cli}-final.log`、`cleanup.log`、`cleanup-readback.json`。
首次 source 验收的 fixture 路由模块绑定/legacy body/schema 和遗漏 asyncio 导入均已修复，
最终门禁无未解决失败；早期矩阵或历史 retirement 结果不作为本项通过证据。

fixtures 清理自有 SQL 行/库、Redis queue/cache、Milvus collection、MinIO bucket/objects，
独立读回当前 scratch 库/桶/集合/队列均为零。Go 清理自有 Infinity DB、listener、私有配置
与客户端；自有 Infinity 容器无命名卷，删除后确认两端口关闭。仓内临时 native symlink
和三个临时二进制已删除，复用的外部 native/toolchain 保留。未修改业务数据、共享配置或
Web；没有运行完整后台 worker、远程模型/provider、真实许可系统或浏览器验收。

### 同 SHA 根审查补修：鉴权、并发与支持引擎的可用性

初次交付 `ed9197802f67045525bdb74f2c27be81584cf06c` 和
`295d3826baf238c4b585a15d849af0b4976095f0` 的门禁及读回成立，但根审查发现下列遗漏。
本轮逐项补修后重新运行适用门禁，初次通过数不替代这里的证据。

| 具体缺口 | 补修与验收 |
|---|---|
| Go 只核用户 status/is_active，遗漏 JWT logout 与个人 Principal | 本路由增加 is_authenticated、非匿名、有效个人 Tenant(id=user.id) 与唯一有效个人 owner membership。签名 JWT 另拒绝 load_user 所用 INVALID_ 标记；API key 不套用 JWT 登出规则。真实 Python REST logout 后，同旧 JWT 在 Python/Go HTTP 均 401，拒绝前后完整 SQL/索引不变；API key 仍真实成功。逐项核失效用户、租户、个人成员角色/状态及缺失个人租户/成员，保留有效 owner/admin。 |
| 不同文档共享 Go Infinity Thrift client | 每文档打开独占连接，服务只使用该连接执行 count/update/recovery，取消排空后关闭；共享主连接没有被本项状态调用使用。透明 TCP proxy 仅延迟一个实际 Update 请求，观察不同文档走不同 Thrift TCP 连接、另一文档完成、关闭 HTTP socket 后 SQL 锁仍持有、后续同文档赢家正确。HTTP 返回前私有连接均关闭，listener 结束后 proxy/主连接也关闭。不是把不同文档行锁当连接锁。 |
| 母块隐藏只实现于 Milvus | Python Infinity 和 Go Infinity 按 mom_id=id 分离母/子更新；ES/OpenSearch availability 专项使用原子脚本，母块始终 0，子块按目标，并保留真正 no-op。真实 Python/Go Infinity 验启停、same-state 修复、实际 SQL deferred COMMIT 失败恢复；available=1 查询不含母块，完整内容/向量/创建字段/关系/数量保留。已选 Milvus 母块回归继续通过。 |
| ES/OpenSearch conflicts=proceed 丢弃响应返回 True | availability 专项核 total=updated+noops、无超时/冲突/failures，缺表/未知/不完整/部分响应失败。正式调用真实 connector 和 DSL，仅控制 transport 返回；覆盖 complete/noop、各失败形状、SQL 不变、补偿与重试。非 availability 通用更新没有扩改。未启动真实 ES/OpenSearch 服务，不能把该边界当成真实引擎验收。 |
| Python 未验提交失败叠加赢家/恢复前删除 | 隔离 PG deferred constraint 真正在 COMMIT 抛错；具名恢复调度门在真实 rollback 后暂停。删除自有触发器后，真实新 HTTP 赢家提交 0，原补偿按新 SQL 0 恢复；另一用例在已查到并写过索引后、恢复前真实删除文档，返回恢复无法确认并继续下一文档，不伪成功。独立 SQL 整行与 Milvus 完整索引读回符合当前赢家，恢复事务释放。 |

修后实际结果：`make verify` **3772 unit passed**（Ruff、8 import contracts、async DB 与
mypy 通过）；`REQUIRE_SERVICES=1 make integration` **500 passed，无 skip**，含同实际
FastAPI listener 的 smoke 与本项 25 个正式状态用例。ES/OS connector+已有 Infinity 缺表专项
**48 passed**；显式实际 Go HTTP **22 passed，无 skip**，追加个人租户/成员缺失和私有连接
关闭断言后只补跑受影响两例 **2 passed，20 deselected**，不把重复用例合算为新的覆盖数。
Go gofmt、build/vet internal、handler/router 专项与三个 cmd 独立 main build 通过；m1cpu
既有 CGO VLA 警告未改变。原始日志使用本机 `/tmp/multirag-a536-review-` 前缀的
`verify-final.log`、`integration.log`、`connector-unit-final.log`、`go-http.log`、
`principal-rpc-final.log`、`go-build.log`、`go-vet.log`、`go-unit.log`、
`go-cmd-{server,admin,cli}.log`。首次新增用例因 Infinity 无序行返回及测试 DROP FUNCTION
未限定 schema 失败，已按 ID 对齐全部字段并限定 usr_ai，未削弱数据或业务断言。

Go ES 验收实例的 store 为 nil：只证明当前 service 的 capability 拒绝和 SQL 语义，
没有真实 ES client/server，也不能宣称真实 ES 的无索引或写入能力。Milvus 客户端真实但
该 Go 写能力仍不可用；Infinity 真写、真实 SDK 读回。所有故障调度/transport 边界已注明；
没有声明浏览器、完整 worker、远程模型或真实许可系统验收。只清自有本轮 scratch 库、
对象/索引/队列、Infinity 数据库/容器、listener/proxy、私有配置和临时构建产物；共享业务
资源及 25 项无关改动保留。独立清理读回见本机本轮 review-cleanup 文件。

### 同 SHA 旧母块兼容补修

上轮母块验收只覆盖自身 `mom_id=id` 的新形状。基线 worker 的母块 allowlist 删除
自身 `mom_id`，但子块保留父 ID：既有 ES/OS 母块缺字段、Infinity 母块默认空字符串。
只改后续 producer 不能保护这些既有行，启用、同状态修复及失败恢复 SQL=1 都有缺口。

Python ES/OS 现在按可信 dataset/doc 查询完整 scroll 快照，校验超时、分片、精确总数、
每页及终止行数，重复/未知/部分关系读取失败，不执行 availability 写入；游标始终关闭。
脚本按同文档父引用与自身标记保持母块 0。Python/Go Infinity 从当前数据集表的同文档
实际计数和全部 id/doc_id/mom_id 行辨认父块，行数或关系错误时不写；Go 仍使用已有
独占连接。普通缺/空 mom_id 切片正常启用，其他文档的引用不能跨文档隐藏切片。
只更新 availability，保留源字段/向量/创建字段/父子关系/数量，不迁移生产旧数据。

隔离真实 Python/Go Infinity 用例同时放入省略 mom_id 的旧母块（独立读回默认空）、
新自身标记母块、普通空 mom_id 切片、其他文档引用及另一个数据集表。实际 HTTP
禁用→启用、SQL=1 同状态母块修复和 PG deferred COMMIT 失败补偿后，全部 SQL/
索引字段保留；available=1 查询排除两类母块、包含普通切片，其他文档/数据集不变。
受影响真实 HTTP 三例（两栈母块与不同文档 RPC/取消）通过；这些是既有 22 例中的
受影响复跑，不合算为新增 25 例。ES/OS 仍仅真实 connector/DSL 加受控 transport，
没有真实 ES/OS 服务验收。Go ES/Milvus 能力边界保持当前合同；旧 change_status 后续已按本项条件退出续作删除。

本轮修后 `make verify` **3792 unit passed**，Ruff、8 import contracts、async DB 和
mypy 127 文件通过；`REQUIRE_SERVICES=1 make integration` **500 passed，无 skip**，
包含实际 FastAPI listener 的 smoke。connector/Infinity 专项 **68 passed**，受影响
真实 HTTP **3 passed，19 deselected**。Go gofmt、build/vet internal、Infinity 父关系及
handler 严格状态专项、三个独立 cmd main 构建通过。首次 cmd 构建命令误用了旧文件名，
按当前 server_main.go/admin_server.go/multirag_cli.go 更正后分别通过，未修改生产代码
规避构建失败；仅现有 m1cpu CGO VLA 警告。原始日志为本机
`/tmp/multirag-a536-legacy-{verify,integration,unit,http-initial,go-build,go-vet}.log`、
`legacy-go-targeted-final.log` 和 `legacy-go-cmd-{server,admin,cli}.log`。
隔离夹具删除本轮 scratch SQL/对象/索引/队列、Infinity 数据库及 listener/proxy/配置；
独立读回并清理自有 Infinity 容器、native symlink 与三个临时二进制，复用的外部
Go/native 保留。清理记录为本机 `legacy-cleanup.log` 与 `legacy-cleanup-readback.json`。
原 25 项无关改动保留，不改 Web、不 push，等待当前项审结。

## c949096db038f11d44b969902da440a800a75a3f · 本批首项已有等价实现

2026-10-02 只读核目标完整单文件 diff 与当前 Canvas.reset：非 None 显式值保留、类型默认
兜底、deepcopy、缺 env 补齐及 globals 序列化已随 4f 采用在 `bb2431f59e8ed15c627514f069a0c14403c9e496`。
此前记录见下方 4f 小节，本次无剩余实现或新范围提交，不重复历史门禁/运行验收。
Web 当前仍保存 variables.value/globals；Go 无 reset/VariableAssigner 基盘，本项不适用。

## 488c3ef6a306cf11f73dd642c0e7fd0420c4001e · Task 取消 REST API

- 上游 #14393；2026-10-02 核对预期 origin 并 fetch，
  `origin/main` 为 `519e7d98a5651564d4e35d6648f006cba4baaf4f`。
  从本仓 `0550829cd6cea9d2611fa3fa6e3b2f6ddfd5b660` 开始，核对目标四文件完整 diff 与目标树。
  本项为本批第十项，完成后停止并等待下一次派发；本项没有 push 或其他聊天派工。

| 上游 diff | 本项处置 |
|---|---|
| 删除 `api/apps/canvas_app.py` | 初次移植时因独立 Web 的 Agent/DataFlow 活动 PUT 调用保留单一路由；Web POST 迁移及验收审结后，已删除独占文件和旧路由。初始及补修证据保留在下文，本轮退出结果见“旧 Canvas 取消入口退役”。 |
| 新增 `api/apps/restful_apis/task_api.py` | FastAPI POST cancel、PATCH action=stop；异步 Principal + AsyncSession，没有 GET。文档和图任务按有效 SQL 资源/已加入成员授权，运行任务另需服务器可信登记。 |
| `web/src/services/agent-service.ts` 两个取消函数 PUT→POST | 独立 Web 由根聊天另派，`f39ccdb5c72236bd99d7b62c09f92535d6a61e2a` 已完成 POST 迁移并审结；后端本轮只消费该完成事实，不重做浏览器验收。 |
| `web/src/utils/api.ts` 取消 URL→Task REST | 当前稳定合同见 [HTTP 参考](../references/http_api_reference.md#task-api)。本项没有 Web 代码变更。 |

相关后续修复逐项核对：`5885691c683c5cf10954d06087e453e485cef7e2` 的未知/终态幂等采用；
`28a41ed0701adec9222a9dc5851239af70950cd8` 的文档取消日志采用，未扩展无关 Langfuse；
`19ec6245c4fad1c73d500defcaf37f28e9cd77d8` 的授权前禁止 Redis 写入采用，并补齐上游哨兵
绕过的归属缺口；`2223a514de9b8daad18b41b3f06119928d7b53a3` 的 begin2parse、失败进度与
取消传播按本地链适配。当前 chunk 构建已重抛 TaskCanceledException，文档同步已有 CANCEL
守卫，不引入不存在的上游 chunk_builder。未查到 Task 特定 revert/re-land；
`6a4b9be42` 为格式变化，`670e68872` 整体删除 Python 后端不作为本项撤回。

### 归属、生命周期与写入

普通 Task 联查有效 Document/KB/当前 status=1 的 owner、normal、admin；图任务从有效 KB 的
graphrag/raptor/mindmap_task_id 反查，不把 `graph_raptor_x` 当文档。DataFlow 调试响应的
message_id 就是实际入队 Task ID，`dataflow_x` 的归属来自登记与真实队列；Agent 的随机
Canvas.task_id 没有 SQL Task 行，SSE task_id 与 message_id 不同，未伪造 Canvas Task。

`task_cancellation_service.py` 从 SQL 画布取 owner 并验证当前调用者；实际 REST 草稿、发布、
续跑、共享 SDK/OpenAI completion 在首帧前绑定，DataFlow 在入队前绑定。成员的 binding
principal_id 是当前用户，tenant_id 与队列 tenant 是 SQL 画布 owner；客户端 user_id、tenant_id
和 DSL 不提供授权。共享运行链也识别可信 RunContext 平台用户。

`core/utils/task_runtime.py` 定义 Python/Go 共用 Redis `task-runtime:v1:{id}` 协议：version=1、
principal/tenant/resource/kind/state，24 小时 TTL。SET NX 登记；active→cancel_requested 的
CAS 同时写 nonce 取消标志；自然结束只把 active→finished，保留取消状态。旧/过期无登记的
Agent ID 无副作用成功，仍有 SQL 行但无可信归属的旧 DataFlow 调试任务明确拒绝。
运行清理用 SET NX，保留 API nonce 和 TTL；未知、终态、重复或 finish 获胜均不额外写标志。

SQL 行锁序列化取消。Task -1 与单次 nullable 日志、活动文档 CANCEL/progress=0 同事务提交。
持久 `[cancel_requested]` 标记守卫 get_task/update_progress 的迟到写入；begin2parse 不复活
CANCEL 文档，普通失败 -1 无此标记仍允许恢复。Redis False/异常、SQL 提交失败和回滚失败
均非成功；SQL 失败尝试只补偿本次 nonce，补偿无法确认明确失败。响应只确认提交/幂等，
不承诺 worker 已停止；没有分布式事务或新增 Task 查询 API。

### Go 与实际验收

Go 增加实际 TaskHandler/TaskService/路由/DI，通过现有 GetUser 和 SQL API token 鉴权，
使用同一 PostgreSQL 与 raw Redis；没有调用错误的 DeleteByTenantID，也没有编造 Go Canvas
引擎。`dao.PostgresDSN` 将安全校验后的 search_path 放进每连接参数，替代只影响某条池连接
的 SET。真实验收同时持有三条连接，逐条确认 usr_ai；用户名/密码 URL 编码有单测。

`test_task_cancellation.py` 的 38 个真实 HTTP 用例通过：JWT/API key、POST/PATCH/旧 PUT、
未知/终态、nullable 日志、普通文档实际 queue_tasks、三类实际 run_index 图任务、实际 DataFlow
调试入队、邀请/失效与真实加入成员、登记失败首帧前拒绝、SQL/Redis 故障、同时取消、worker
已读任务后的迟到进度、普通失败重试以及当前 set_progress/Pipeline.callback 观察取消。
实际 Agent 草稿/发布/续跑在可控等待后取消，兄弟运行仍成功；独立 SQL/Redis 读回会话错误、
无成功助手终态、无 Agent Task、画布/版本及发布运行编辑副本隔离；自然清理后 API nonce/TTL
不变。等待仅替换 VariableAssigner 的执行边界，Canvas/Begin/Message、鉴权与存储真实。

Go 跨端验收显式运行 `tests/integration/task_cancellation_go_acceptance.py`，避免普通 Python
CI 增加 Go/CGO 前置。它驱动真实 Go Router/AuthHandler/TaskService HTTP listener，取消 Python
实际创建的文档、DataFlow、图任务及 Agent 草稿/发布/续跑；SQL/Redis 独立读回后 Python 的
Canvas/Pipeline 确认取消。Go 使用隔离 signing store，不写全局签名 Redis key；本地许可状态
仅初始化测试运行状态，未验收真实许可系统。其他无关 handlers 不在本项 listener 实例化。

本机原无 Go 和 native 静态库。实际下载校验官方 Go 1.25.14，与 go.mod 的 1.25 系列一致；
Go 1.27.1 与既有 grpc/x-net 依赖编译不兼容，未为本项升级共享依赖。使用当前 C++ tokenizer
源码、真实 PCRE2 与 SIMDe 0.8.2 在自有临时目录构建静态库，未使用 stub、未改 native 源码。
本项使用的仓内 native 符号链接在最终验收后删除。复跑跨端验收需先准备真实 tokenizer 静态库：

```sh
MULTIRAG_TEST_GO=/path/to/go REQUIRE_SERVICES=1 uv run --no-sync pytest tests/integration/task_cancellation_go_acceptance.py -q -s
```

最终 `make verify` 通过（Ruff、8 条 import contracts、async DB 门禁、mypy 126 文件，3695 unit
passed）；`make integration` 385 passed，无 skip，含上述 38 个真实 HTTP 用例及同 listener 的
实际 `make smoke`。显式 Go/Python 跨端验收 10 passed，无 skip；gofmt、Go build/vet
`./internal/...`、DSN/Task handler 专项单测及三个 cmd 入口分别 build 均通过。依赖 m1cpu 的
CGO VLA 编译警告仍存在，不影响退出码；没有为了换绿放宽门禁。原始日志位于本机
`/tmp/multirag-488-` 前缀的 verify、integration、http、smoke、go-http、go-live、go125-build、
go-vet、go-unit、go-cmd、native-config、native-build `.log` 文件；没有连接密钥。
fixture 逐项检查自有 SQL 行、Redis stream/归属/取消 key 和 HTTP listener 清理，共享 fixture
删除 scratch 数据库；不操作业务库，本项没有创建容器/卷。没有运行完整后台 worker 进程、
远程 LLM/provider 或浏览器，真实 worker 进度和 Pipeline 取消观察不能替代这些验收。

### 同 SHA 收尾竞争补修

根审查发现最后 Canvas 事件后，旧 completion 先成功 append 助手，再调用忽略结果的
finish_runtime；取消 CAS 即使获胜，仍会保存/发送成功。补修前正式真实 HTTP 回归分别
复现流式、非流式错误成功（2 failed），不把根的纯内存时序复现当作真实 HTTP 证据。

FINISH_RUNTIME_SCRIPT 现在原子返回 unbound/finished/cancel_requested：先于成功 SQL append
与终帧判定；active→finished 获胜后到达的取消幂等，cancel_requested 获胜则沿现有失败链
保存 user/errors，轮数只加一次。missing、Redis 异常/False 不成为成功。版本仍为 1，
binding 字段、取消/恢复 CAS、授权、TTL、nonce 合同不变；Go 取消服务无需改实现，未新增
Go Canvas。普通后台任务可得到 unbound 并按现有 finally 清理；DataFlow enqueue 失败与
背景 finish 调用不把该结果当成功答案。共享 completion 成功登记只尝试一次，避免 Redis
失败后 finally 重新登记掩盖故障；调试 Agent 同样先判定再写副本/释放缓冲终帧。

共享链先缓冲 workflow_finished/message_end，成功时保留原有 workflow→message 顺序。
普通 REST error 后不再额外发 DONE；beta 继续无新增 DONE，OpenAI error 继续不发成功终态。
非流式失败不返回成功答案；已发的流式内容片段无法回收。成功终帧先序列化再提交消息，
运行登记 finished 不代表 SQL 保存/交付成功。Redis 故障保留明确失败与有限期 active 记录，
不假造已完成。成功提交后发生传输中断不重复改写该轮。

收尾 SQL 故障回归又复现 PendingRollbackError 导致失败轮次未保存（2 failed）；补修为先
rollback，再保存一次 user/errors。真实 HTTP socket shutdown 在取消获胜的首次/续跑中
触发 ASGI CancelledError，使用受保护的失败持久化/清理保留原 nonce、取消状态及一次轮数；
客户端已断开，不声称错误帧交付。断连 fixture 保留 SSE 迭代器并关闭自有 TCP socket，
避免临时迭代器被释放而在目标收尾窗口前意外终止请求。

本轮新增真实验收覆盖 REST/beta/OpenAI 首次、续跑、发布快照，stream 真/假；实际
Canvas Begin→Message 的最后事件后、原子 finish 调用前、成功 SQL append 调度前三个窗口，
finish/cancel 胜负、Redis 异常/False、真实 SQL flush 故障、真实 TCP 断连及独立兄弟成功。
SQL messages/errors/round、runtime 状态、nonce/TTL、原定义/版本/副本均独立读回。
等待只注入实际 Canvas EOF 与持久化调度边界；Redis finish 的异常/False 及 SQL flush 的
before_cursor_execute 一次失败为故障注入，随后仍实际回滚/写入/读回。鉴权、生命周期
正常 Lua、组件、存储和业务响应真实，未使用模型/provider 替身或远程模型。
Go 真实鉴权 HTTP 增加首次/续跑/发布收尾取消和 finish 获胜的 stream 真/假用例。

上一轮收尾补修（`a4afaced`）最终 `make verify` 通过（Ruff、8 条 import contracts、async DB 门禁、mypy 126 文件，
3698 unit passed）；`make integration` 418 passed，无 skip，含新增 33 个真实 HTTP 与同
隔离 listener 的实际 smoke；Go 跨端 18 passed，无 skip，含新增 8 个收尾窗口用例。
全量中三个既有错误流 DONE 断言按根明确合同改为断言不出现 DONE，保留错误码、无成功
答案及 SQL 读回门禁后复跑通过。Go 生产实现/绑定协议未变，未重复初轮 build/vet/三个
入口构建，当前真实 Go HTTP 完整矩阵已复跑。原始日志另用 `/tmp/multirag-488-terminal-`
前缀的 before、sql-before、disconnect-before、http、fault-http、disconnect-http、unit、
verify、integration、go-http、smoke、cleanup `.log`，原有第十项已完成证据保留。
fixture 和额外读回检查自有
SQL/Redis/listener/scratch 库/私有配置清理，最终删除自有 native 链接；仍未运行完整后台
worker、远程 provider 或浏览器。没有改变 Web 或旧 PUT 的已核实兼容退出条件。

### 同 SHA 关闭与失败历史补修

仍是第十 SHA `488c3ef6a306cf11f73dd642c0e7fd0420c4001e`，没有开始下一项。
根指出帧 yield 处 aclose 注入 GeneratorExit 没有保存失败轮次，且 Canvas EOF 已把本轮
assistant 写入 history/sys.history，取消失败轮次仍被下一轮模型消费。
先补正式改前回归：close-at-frame 单测 1 failed；实际隔离 HTTP 首次/续跑的 SQL history
断言 2 failed；真实 HTTP 消息 send 等待时 shutdown TCP 又复现实际 Canvas 未关闭
（1 failed）。单测和根 AST 复现没有当作 HTTP 证据。

新增 AgentStreamingResponse，在 ASGI 响应退出后、请求 DB 依赖退出前保护性关闭 body
迭代链；REST 预取首帧另登记关闭回调，覆盖响应头发送失败且外层生成器尚未启动的情况。
completion 把 GeneratorExit 与 CancelledError 统一为未提交轮次的失败保存，关闭过程中
不 yield，保存后继续传播原关闭；已保存成功轮次不重复 append 或改写。OpenAI 外层显式
aclosing responses，beta 保留 aclosing；debug 显式关闭真实 Canvas 并保护运行收尾，
没有为它新增 SQL 会话。未改授权、身份 helper、binding v1/CAS/nonce/TTL 或 Go 生产实现。

共享 completion 在执行前深拷贝历史前缀，失败持久化恢复此前 history/sys.history，只保留
当前实际新增用户输入；取消、Redis 收尾异常/False、SQL flush 失败都走该路径。
不清空旧成功历史，不批量修历史会话，不改 Agent 定义/版本、发布快照或其他会话副本。
每个相关失败用例独立读 SQL DSL 并重建真实 Canvas.get_history；继续实际 HTTP 运行同一
session，检查真实 Canvas.run 开始前的 get_history 输入与失败 SQL 一致，没有失败助手。

新增 TCP 关闭矩阵覆盖 REST/beta/OpenAI 首次、续跑、发布快照和显式草稿会话，
消息 send 等待与 Canvas EOF 内部 await 两种取消先赢窗口，以及完成/成功 SQL 提交先赢
后在终帧 send 处迟到取消与断连。另验收 debug 两类取消关闭与已成功副本关闭。
send 仅控制真实 ASGI send 的等待边界，随后实际 shutdown 自有 TCP socket；
不是内核缓冲区饱和证明。保持响应强引用，避免 GC 被误当作响应负责的关闭。
实际记录 send 处 GeneratorExit、内部 await 处 CancelledError，SQL message/errors/round、
history/sys.history、恢复/续跑 get_history、定义/草稿/版本/Redis 原副本、runtime/nonce/TTL
均独立读回。结束先赢取消在关闭前不新增 nonce/log，关闭后内部 x 的既有 1 小时 TTL
与 API nonce 的 24 小时 TTL 分别验证。显式草稿会话原有开场消息作为前缀完整保留。
ASGI 2.3 断连、2.4 OSError、响应头失败和外部 Task.cancel 的关闭传播另由正式单测覆盖，
这些 send 假件结果没有当作真实 socket 验收。

Go/Python 显式矩阵追加三种 Agent surface 的消息发送处取消、EOF 等待取消、完成先赢
后关闭；Go 只提供真实认证与取消 HTTP，组件与实际执行仍在 Python，未编造 Go Canvas。

关闭与失败历史补修（`cdda343e`）最终 `make verify` 退出 0：Ruff、8 条 import contracts、async DB 门禁、mypy 127
源文件及 3706 unit passed。`make integration` 退出 0，463 passed、无 skip；其中收尾
HTTP 39 个（原 33 加显式草稿 6）、真实 TCP/ASGI 关闭 39 个，同一次隔离 API listener
的 `make smoke` 通过。Go 显式真实 HTTP 矩阵退出 0，27 passed、无 skip（原 18 加
关闭 9）；未改 Go 生产代码/协议，未重复无关 build/vet/三个 cmd 门禁。
原始日志用 `/tmp/multirag-488-close-` 前缀的 unit-before、history-before、send-before、
unit、verify、integration、go-http、smoke、cleanup `.log`；中途 fixture 修正记录另存
http-fixture-adjustments `.log`，不是最终通过证据。fixture 修正覆盖 Redis 返回字符串、
内部清理 x 的原有 1 小时 TTL、OpenAI 中间空内容帧及草稿预置开场消息/初始 dict DSL，
保持最终 nonce、历史前缀与真实终态断言，没有削弱生产门禁。

fixture 验证自有用户/token、Agent/版本/session/Task 等 SQL 行、Redis 运行归属/nonce/
队列/副本、Python/Go listener 与客户端清理；独立读 PG scratch 库 0、验收队列 0，并
确认同 smoke listener 已关闭。当前自有仓内 native 链接已移除，54 个本轮 Go 验收临时
目录内的 base/stop/log 文件清理，私有 config 均不存在；本轮以前的外部工具链与静态库
及本轮验收日志保留供审查。没有创建容器/卷、改业务库或部署，没有改 web/旧 PUT 退出
条件。仍未验收完整后台 worker、远程模型/provider、浏览器或内核缓冲区饱和；关闭后
不保证错误帧交付，已经发出的片段不能撤回。

### 同 SHA debug 首次迭代前生命周期补修

本轮仅修 debug 在响应头发送前失败/取消的剩余窗口，仍止于第十 SHA；前述已通过的
成功持久化竞争、GeneratorExit 单轮失败保存与 history/sys.history 补修保持原实现。
根的 AST/内存复现不是 HTTP 证据；本轮正式新增 4 个真实隔离回归，改前均失败：
response.start 发送 OSError 与首次迭代前实际 TCP shutdown 的两类窗口，分别在 active
或 API cancel_requested 获胜后关闭。前者实际 HTTP 500，后者在受控响应头 send 等待
中发生真实 socket 断连/ASGI CancelledError；都确认实际 Canvas.run 尚未开始。
改前 active 没有结束，cancel_requested 场景也未执行 runtime finish/cleanup。

debug 的 finish_attempted 现在归响应与 sse 共同持有，新增一次性 close_runtime：执行
finally 和响应 close_callback 共用保护性 finish/cleanup。未启动的生成器 aclose 没有
finally 时，回调仍收尾已登记运行；正常运行、已开始后关闭和已提交副本不重复收尾。
成功资格判定已经尝试后不重试 finish，原 CAS 获胜状态与 API nonce/TTL 不覆盖。
登记后释放 SQL 读事务若失败/取消也走同一收尾，然后继续传播异常；没有提前开始或
执行 Canvas，没有给 debug 新增 SQL 共享会话，没有保存失败副本或在关闭中 yield。
生产仅改 agent_api.py 的这个 debug 分支；beta/OpenAI/旧 SDK helper、授权与通用
response helper、Go 生产代码和 Web 未改。

正式单测调用当前已注册路由函数生成响应，覆盖 response.start OSError/CancelledError、
body send 失败、正常完成和登记后 setup 取消；各验证收尾/cleanup 一次及不吞关闭信号。
实际 HTTP 用例薄包装记录真实 finish、Canvas.cancel_task、Canvas.run 调用，仍执行原
Lua/Redis/SQL/组件，没有假造收尾成功；独立 SQL 会话/Task、定义/版本、Redis 原副本、
binding v1、状态、nonce/TTL 均读回。Go 另追加真实取消 HTTP→Python 两个头窗口验收，
不虚构 Go Canvas。受控头 send 故障不称自然 TCP 头发送失败或内核缓冲饱和。

本轮最终 `make verify` 退出 0（Ruff、8 条 import contracts、async DB 门禁、mypy 127
源文件、3711 unit passed）；`make integration` 退出 0，REQUIRE_SERVICES=1 下 467
passed、无 skip，保留前轮 39 收尾 HTTP 与 39 TCP 关闭回归并新增上述 4 个头窗口。
同一实际隔离 listener 的 `make smoke` 通过。显式 Go HTTP→Python 矩阵退出 0，29
passed、无 skip（原 27 加 2 个取消先赢的 debug 头窗口）。没有把前轮通过数作为本轮
证据，没有改 Go 生产实现或重复无关 native/build/vet/三个 cmd 门禁。
日志用 `/tmp/multirag-488-header-` 前缀的 before、unit、http、verify、integration、
go-http、smoke、cleanup `.log`；before 为正式改前 4 failed，http 为修复后定向 82
passed。单测初版路由选取的 fixture 错误另存 unit-fixture-adjustment，不当作缺口复现。

fixture 核验自有 SQL 行、运行绑定/nonce/副本/队列、Python/Go listener 与客户端清理；
独立读回 scratch 库 0、自有验收队列 0，并确认同 smoke listener 已关闭。自有仓内
native 链接移除，29 个本轮 Go 临时目录的控制/log 文件清理，私有 config 均不存在；
以前已留存的外部工具链/静态库和本轮验收日志供审查。不操作业务库，不创建容器/卷，
不改 Web 或旧 PUT 退出条件、不 push。完整 worker、远程 provider/模型、浏览器和
内核缓冲区饱和仍不在已验范围，响应头前断连不声称任何响应帧交付。

### 旧 Canvas 取消入口退役

本轮从 `beee09f17eb52646986e22f8f2f3872f1959533d` 继续同一第十 SHA，仅退出旧
`PUT /v1/canvas/cancel/{task_id}`。2026-10-02 再核预期 remote、fetch 和目标四文件完整
diff，`origin/main` 仍为 `519e7d98a5651564d4e35d6648f006cba4baaf4f`；前述后修链保持
本地实现，没有新批次或重做 finish/close/history/header 修复。

独立 Web 的 `f39ccdb5c72236bd99d7b62c09f92535d6a61e2a` 已由根及独立审查审结：两类
alias 均使用共享客户端 POST Task REST、编码当前 Task ID、无 body；804 正式测试及
真实 Python/Go HTTP、SQL/Redis 和 Canvas 读回为此前证据。本轮核对当前调用代码，
但不把那些结果当成本轮新跑。前端 Pipeline 验收使用同生产 Hook 的临时入口真实入队，
未验文件上传；浏览器运行在并发依赖升级前，升级后正式测试/build 通过。完整 worker
或 provider 未启动；这些边界不因接口退出改变。

再次核对 API/SDK、两个 Python SDK worktree、MCP、core/agent 后台、Go internal/cmd、
停滞 server/admin 与独立 Web，没有旧 URL 的活动消费者或明确兼容承诺。按移植 Skill
`references/python.md` 的迁移完成判据直接删除 `api/apps/canvas_app.py` 全部 18 行；
自动扫描注册自然退出该路由，不改集中注册。TaskID、StopTaskRequest、cancel_response、
授权服务、Redis CAS/nonce/TTL、生产取消观察链、Go POST/PATCH、SDK `/parse/cancel`
均保留；未注册 Agent helper 不属于此旧 PUT 链，没有顺删共享 Redis 导入或历史数据。
本轮归属只有路由文件、`test_task_cancellation.py` 与三份合同/进度/映射文档。

正式测试移除成功 helper 的 PUT 分支及两条过时成功参数，原 POST/PATCH、JWT/API key、
授权、日志、故障补偿、CAS/TTL、幂等、迟到 worker、finish/关闭/history 断言保持。
新增 10 个退休用例：文档、可信实际 Agent、真实入队 DataFlow 的 active/finished/
cancel_requested 加未知 ID，各发 owner JWT/API key、外人 JWT/API key、无凭据五类
真实 HTTP 请求。50 次旧 PUT 全部 404，OpenAPI 无旧路径；取消服务计数为零，并由新
POST 的真实执行验证计数边界有效。每次独立读回完整 Task/Document/KB、Canvas/版本/
session 行及 Redis binding、nonce、副本、队列 payload，逐字保持；PTTL 只允许自然
递减，未知 ID 不生成标志。背景 finished 状态为隔离 SQL/finish fixture，不宣称 worker
完成；Agent 的等待只控制 VariableAssigner 边界，Canvas、Begin、Message 及存储真实。

删除前正式 document_active 回归失败（旧 PUT HTTP 200），原始日志保留；删除后定向
128 passed、无 skip，含全部当前 Task 取消及此前终态/关闭/debug 头窗口回归。
本轮 `make verify` 退出 0：Ruff、8 条 import contracts、async DB 门禁、mypy 127 源文件、
3711 unit passed。同一隔离 API 的 `make smoke` 通过，检查 ping 与全部 healthz 组件。
`REQUIRE_SERVICES=1 make integration` 退出 0，475 passed、无 skip（前轮 467 去掉旧
PUT 两条成功参数，新增 10 条退役回归）。适用 Python 门禁全部为本轮新跑；Go 只核
当前注册无旧 alias、保留新 POST/PATCH，前轮 native/build/vet/29 个跨端 HTTP 为历史
证据，本轮没有 Go 生产改动或无理由重建矩阵。

fixture 验证 SQL 行、凭据、队列/键、HTTP 线程和客户端清理。额外只读 pytest 观察器
在各轮所有 fixture 收尾后独立检查：定向 128 个、全量 222 个 API 用例各自的 scratch
库、键/队列和 listener 均为 0；另在 pytest 退出后用独立进程再读回两轮资源并确认
同 smoke listener 已关闭。观察器只记录资源标识和读取状态，不替换鉴权、服务或存储。
没有落盘临时凭据/config、启动完整 worker/provider、创建容器/卷或修改业务库。

原始证据为 `/tmp/multirag-488-retire-` 前缀：`before.log`、`http.log`、`verify.log`、
`integration.log`、`smoke.log`、`consumer-audit.log`、`cleanup.log/json`；两轮
`http-resources.json`、`integration-resources.json` 留存自有资源标识供独立审查。
本仓原 25 项无关状态和全部无关 tracked diff 保持，ragflow 的无关状态保持；Web
并行编辑器/工具链任务在此期间继续提交和编辑，本轮未对其状态变化做回滚或纳入提交。
只提交本项五个路径，无 push，等待根审查；本轮不重做前端浏览器、文件上传、完整
worker/provider 验收，也不把提交确认 `data: true` 写成后台任务已停止。

## 82313020c71b8b91873232c2334c2c1c382f1c49 · 列表操作与 strict 模式

- 上游 #14387；2026-10-02 核对预期 origin 并 fetch，
  `origin/main` 为 `519e7d98a5651564d4e35d6648f006cba4baaf4f`。
  从本仓 `9bf2c4d7a797c1ed14ad35db3b2a69e1c850dca6` 开始，核对目标 6 文件完整 diff 与目标树。
  本项止于该 SHA；下一项等待另行派发。

| 上游 diff | 本项处置 |
|---|---|
| `agent/component/list_operations.py` 新增 nth/strict，head/tail 改为切片 | 同名组件适配，显式 `operations_version: 2` 使用新合同，缺失/1 保留历史合同。strict 正确解析布尔字符串，空输出 first/last 为 None，错误清空上一轮结果。过滤/排序/去重保留本地行为。 |
| `test/testcases/test_web_api/test_canvas_app/test_list_operations_unit.py` 新增语义矩阵 | 不复制整包伪造或 `__new__` harness，使用实际 Canvas、Begin、参数检查及 invoke 建立 236 个组件单测；另测实际 HTTP、SQL 和 Redis 边界。 |
| `web/src/locales/en.ts` 改 nth 文案并新增 strict 说明 | 独立 web 由根聊天另派；交接准确合同。宽松 head/tail 超长返回全部，不能沿用上游 tip 中“无效 n 一律空数组”的笼统说法。 |
| `web/src/locales/zh.ts` 更新 head/tail/nth 与 strict 文案 | 同上，nth 支持正负位置，head/tail 明确前/后 N 项。 |
| `web/src/pages/agent/constant/index.tsx` 默认 nth、strict=false | 交接新建节点必须显式写版本 2，历史加载先确定版本 1，不能靠新默认值覆盖旧节点。 |
| `web/src/pages/agent/form/list-operations-form/index.tsx` n 整数输入、负数及 strict switch | 交接本仓实际表单/类型/defaults/normalizer/serializer 落点；本项无 web 代码或浏览器验收。 |

未查到目标的 revert/re-land。后续链：

- `f58fae5fb71bad1970da747dfecc8b241875a44f` 的 topN 大小写/空白归一化采用；
  历史版本归一为 topN，新版本归一为 head，不重释历史 head/tail。
- `38c40e64a98c4657b3ac49de217aa3993bd757ef` 的输入 None→空数组采用，非列表仍报错。
- `3f805a64f15587e16900f85ffd661d49fa9161fc` 的 sort_by 字典字段排序属另一行为，
  不提前合入；保留当前完整 hashable key 的字典排序。`6a4b9be42` 只有格式变化。
  后续整体删除 Python 后端不作为本项撤回。

本地历史 DSL 没有版本字段。缺失或整数 1 保持：topN/省略 operations 取前 N 项，
旧 head/tail 取正数第 N/倒数第 N 单项；非正数或越界保持原空结果，topN 超长保持全部。
历史冗余 strict 字段继续忽略。新版本 nth 支持 1-based 与负数位置；head/tail 取切片，
strict 默认 false，严格范围 nth 为非零且 abs(n)<=len，head/tail 为 1<=n<=len。
`int(n)` 转换保留有限小数截断、整数字符串及 bool；转换失败按 0。版本只收整数 1/2。
新增 marker 的边界在参数 update 的深拷贝，所有加载来源一致；真实 Graph/Canvas
序列化保存该字段，无全局 DSL 迁移、模板修改或运行时 ORM Canvas/发布快照污染。
稳定合同及显式历史转换规则见 [HTTP 参考](../references/http_api_reference.md#列表操作组件与历史-dsl)。

消费者核对覆盖 Python Agent/Canvas、REST 创建/更新/发布/版本/副本/运行/会话/reset/debug、
SDK helper、Channel 历史组件 allowlist；仓内模板无 ListOperations。
独立 web 的 `src/pages/agent/constant/index.ts:initialListOperationsValues` 仍用 topN，
`types.ts:IListOperationsForm` 尚无 strict/version；表单、节点、默认参数和
`operators/normalizers.ts:normalizeListOperationsFormForStore` 及通用 serializer 是实际落点。
新建、历史编辑、导入、复制与保存必须保留版本；不能按字段名推断或批量升级。
本项不替 web 任务完成这些改动。

真实 HTTP strict 验收发现 OpenAI 适配忽略内部 error，旧 completion 非 prepared 分支
还会追加成功助手消息。本项修复共用运行链的错误记录和成功终态时序，再把 error/code、
异常和坏事件转换为 OpenAI error 对象；失败不产生 choices 或 [DONE]。
不以 `**ERROR**` 的成功答案掩盖异常，不放宽 ordinary/SDK 的授权或选择合同。
普通发布运行的无效版本在 Canvas 构造时返回既有通用 HTTP 500/retcode=100，
不是参数细节回显；开流前无会话写入。SDK `create_agent_session` 未注册 HTTP，
本项直接调用真实 helper，再以普通 HTTP 运行其会话，没有新增旧 SDK 路由。

Go 核对 `internal/router/`、`internal/handler/`、`internal/service/`、`internal/cli/`、
`cmd/`、`server/`。`internal/entity/canvas.go` 与 `internal/dao/user_canvas.go` 只承载
通用 JSON DSL/CRUD；`internal/router/router.go:246`、`internal/handler/memory.go:463`、
`internal/service/memory.go:664,667` 的 Canvas 消费尚为 TODO。Go 的 topN 是检索/聊天数量，
没有本组件执行链或对应路由/CLI，不制造 Go Agent 空实现；本项没有 Go 改动或构建声称。

专项：236 组件单测及 33 运行/协议单测通过。17 个隔离 HTTP 用例通过，实际
Begin→ListOperations→Message 覆盖新旧边界、JWT/API key、stream true/false、strict 失败、
None 输入、草稿/发布会话、新建/续跑会话、历史无 marker 续跑、重复 reset/debug、
OpenAI 成功/失败和 SDK 建会话；同一实际隔离 API 的 `make smoke` 通过。
独立 SQL 读回 result/first/last、消息/错误、marker、历史/轮数及 env 默认值，
运行与 debug 前后 Canvas/版本内容相等，Redis 副本字节相等；显式 reset 只保存 SQL Canvas，
已有会话/版本/Redis 不变。未替换组件、模型/provider、业务返回或存储，未调用远程 LLM。
fixture 检查专属用户/token/成员关系/Canvas/版本/会话、Redis key 与 HTTP listener 清理，
共享 fixture 删除 scratch 数据库。本项不写对象/向量，未操作生产数据。

本轮 `make verify` 通过（Ruff、8 条 import contracts、async DB 门禁、mypy 126 文件，
3662 unit passed）；`make integration` 337 passed，无 skip，包含本项 17 个 HTTP 用例
和隔离 API 的实际 smoke。验收中的失败来自测试画布重名、测试 DSL 缺少 sys globals、
对既有通用 HTTP 500 错误文案的错误预期，以及上述 OpenAI 丢失真实 strict 错误。
前三项修正夹具/合同预期，后者修生产根因并加入失败传播回归，未放宽成功或存储断言。

### 活动 SDK 非流式消费补修

根审查指出 `0f9de26c` 未验活动 **POST** `/api/v1/agentbots/{id}/completions`。
该 SDK 路由的非流式分支对共享 completion 第一帧立即返回 code=0；
真实 Canvas 第一帧是 workflow_started，此时 Begin/ListOperations 尚未执行。
隔离实际 beta token、HTTP 与完整 Canvas 复现两个失败用例：strict 和成功请求均
返回 code=0 + started 字符串，独立 SQL 会话 errors 为 null、message 为空，未保存终态。
初始 fixture 的普通 API tokens 没有 beta 值，本轮仅给自有 scratch token 行登记随机 beta，
再通过独立 SQL 读回使用，没有覆盖鉴权依赖或业务返回。

最小补修只改 `api/apps/sdk/session.py:agent_bot_completions` 的消费：非流式读至结束，
用共用 agent_event_error 检测逐帧失败，聚合正文/引用并返回可用终态；未完成 EOF、
坏事件及异常返回非零 SDK code，不用 `**ERROR**` 成功文本。user_inputs 优先于之后
缓冲的 message_end，保留等待表单/tips。两个分支用 aclosing 正确关闭共享生成器；
流式仍直接透传，不新增 [DONE]，不改 beta 鉴权、发布选择或已有会话 DSL。
SDK `agent_completions` 无装饰、旧 `/agents/{id}/completions` 未注册，本轮未改或注册它；
此前的 `create_agent_session` helper 验收不能替代此活动路由。

独立 web `src/api/agent.ts:runExternalAgent` 实际调用 agentbots，传 beta token、
release 和已有 session_id，未显式传 stream（当前默认 true）；
`share/use-shared-agent-runner.ts` 用共享 SSE 消费 message_end、workflow_finished、
user_inputs 和错误。保持这些事件形状；本轮没有 web 修改或浏览器验收，
ListOperations 前端仍等待根另派。成功/等待/失败的当前响应形状见
[HTTP 参考](../references/http_api_reference.md#分享与嵌入-agent-的补全)。

专项运行/协议与组件单测 282 passed（新增 13 个消费、EOF、错误与关闭回归）；
本轮 27 个真实 HTTP 用例通过（新增 10 个 agentbots，加原有 17 个普通 REST/OpenAI
列表操作用例），含同隔离 API 的实际 make smoke。新 v2 strict 失败/v2 成功/历史无
marker 成功均覆盖 stream 真/假、首次/已有会话续跑；发布后改草稿及新建其他会话，
独立读回本次 session 的 messages/errors/DSL marker/result/first/last/轮数，
其他会话、草稿/发布版本及 Redis 编辑器副本保持各自内容。
真实 UserFillUp 在有前置 Message 时暂停，再填列表恢复运行，保留提示并完成 ListOperations。
缺失/坏 beta、误用 API key/JWT 及有效非 owner beta 均真实拒绝，SQL 无会话、无 Canvas
执行，Redis/原画布内容不变。HTTP 验收保留完整 Canvas/run 和真实组件/存储，未用模型/provider、
鉴权或业务响应替身，不调用远程 LLM。fixture 检查专属 SQL 行、Redis key 与 listener 清理，
共享 fixture 删除 scratch 数据库，beta 值随自有 token 行删除；不操作生产数据。

本轮 `make verify` 通过（Ruff、8 条 import contracts、async DB 门禁、mypy 126 文件，
3675 unit passed）；`make integration` 347 passed，无 skip，含上述真实 HTTP 与 smoke。
验收夹具补齐自有 beta token、按实际 SQL 的 DSL 字符串/对象形状构造历史无 marker 会话；
路由返回注解使用 Response，避免 FastAPI 为响应子类联合构造 Pydantic 字段。
AST 对比确认生产文件只改变活动 agent_bot_completions 定义和对应导入，未注册 helper
及其他业务定义不变；本轮补修没有改共享 Canvas/运行 service 或第九项列表语义。

## c1941fd50352d514ecfb20a74785ccb7a1753ad4 · 删除未使用的临时文本解析入口

- 上游 #14367；2026-10-02 核对预期 origin 并 fetch，
  `origin/main` 为 `519e7d98a5651564d4e35d6648f006cba4baaf4f`。
  从本仓 `768fd2f5384ece46e9d4b2103d017f90605a8e5c` 开始，核对目标两个文件完整 diff 和目标树。

| 上游 diff | 本项处置 |
|---|---|
| `api/apps/document_app.py` 删除旧临时 URL/上传文件转文本的 `/parse`、专用文件名 helper 与导入 | 删除本地 `POST /v1/document/parse` 及 `_is_safe_download_filename`、`os.path`、PurePosixPath/PureWindowsPath、HTML parser 和项目下载目录导入。保留其他入口消费的 `FileService`、`is_valid_url`、`html2pdf`、`re` 与上传表单类型。 |
| `test/testcases/test_web_api/test_document_app/test_upload_documents.py` 删除旧入口的 Quart mock 测试和专用导入 | 本仓没有这些旧测试，不复制 Quart harness。新增 4 个隔离 FastAPI HTTP/存储回归，验证退役及保留的调用链。 |

目标没有查到 revert/re-land。当时暂未采用后续 `343bda111` 的 `upload_and_parse` 删除。
343bda 本轮重新沿产品调用链核查：独立 web 的 `uploadAndParse` 只有方法定义和专用测试，
没有活动产品调用；旧会话入库能力现已退役，现行合同以本文最新 343bda 项及 HTTP 参考为准。后续
`a536980e2`（批量 status）、`49912a156`（run）、`c5116b90e`（thumbnails）、
`c81081f8e`（parser）、`f70316911`（preview/download）是独立迁移，不整包纳入。
稳定 API 退役判据沿用技能中的逐接口核对，本轮补充共享 helper、不同解析合同及实际退役验收说明。

实际消费者：核本仓 Python API、agent/core、后台、MCP、scripts，独立 web 的 `src`，
以及两个可见 Python SDK checkout 的 client 路径，没有旧 `/v1/document/parse` 的活动消费。
SDK checkout 中的同名旧后端源码副本不是客户端请求。保留：

- REST 数据集 `/api/v1/datasets/{dataset_id}/documents/parse` 接收 `document_ids`，
  独立 web `src/api/knowledge-document-parsing.ts` 消费，真实后台调度仍经过
  `DocumentService.run`、`queue_tasks`；这不是临时文件转文本的替代合同。
- 当时保留会话 `upload_and_parse`；现已在 343bda 项独立退役，不影响本项保留的共享文本解析。
- `write_app.parse_reference_material` → `ReferenceService.parse_file_content` →
  `FileService.parse_docs(..., "system")` 保留；`web_parse`、SDK datasets chunks 也不变。

Go 实际核 `internal/router/`、`internal/handler/`、`internal/service/`、`cmd/` 和
`internal/cli/`，没有该旧 HTTP 路由或客户端请求。`internal/cli/user_parser.go` 的
`parseParseDataset/parseParseDocs` 生成 `parse_dataset_docs` CLI 命令，与此临时文本 API
不同。本项没有 Go/web 代码变更，不宣称 Go build 或浏览器跨端验收。
AST 比对确认生产文件仅删两个定义，其他函数/模型定义一致；上述保留 service/路由文件逐字未改。
现行合同已补入 [HTTP 参考](../references/http_api_reference.md#上传运行时附件)。

专项验收：4 个集成用例通过。旧入口的空请求、URL 表单、文件上传分别使用无凭据、
JWT、API key、无效凭据，共 12 次真实 HTTP 404，核完整错误体及 OpenAPI operation 消失。
浏览器导入、文件解析、对象写入、Redis 写命令和 SQL DML 均有失败守卫；
独立核 SQL 行数、专属 Redis key、桶对象、Milvus collection 与 `logs/downloads` 前后不变。
在同一实际隔离 API 执行 `make smoke`，检查退出码。

保留数据集解析验证鉴权失败、缺 `document_ids`、不存在 ID 及成功调度，独立 SQL
读回 RUNNING/初始排队进度和 Task，Redis 读回真实消息及 task/doc ID，MinIO 读回源字节。
会话上传验证缺会话、表单错误、未认证及成功 ID；真实文件上传、TXT 分块、MindMapExtractor
和索引写入均执行，SQL 状态/计数、MinIO 字节、Milvus Strong query 的文本和 768 维向量
与本次返回 ID 对应，真实 Redis 模型缓存也读回。只替换模型配置/provider 输出和 Redis
专属命名空间路由，未替换业务返回或数据库/对象/向量存储；未跑远程模型、浏览器或后台解析 worker。
实际 `FileService.parse_docs`、ReferenceService 及写作 HTTP 都解析确定性 TXT，SQL 读回完整参考文本。
fixture 逐项检查专属 SQL 行、Redis stream/cache、MinIO 对象/桶、Milvus collection 和 HTTP
listener 清理；共享 fixture 删除 scratch DB，没有创建本项容器或卷。

额外诊断发现写作参考的空文件/无输入分支用整数 400/500 调用要求 RetCode 的
`get_json_result`，实际空文件 HTTP 为 500。用改动前 HEAD 的实际 handler 定义复现同一
beartype 错误，写作文件和 helper 均未改，这是既有问题，本项未修，也不宣称该分支返回业务 400。
本项保留路径回归的负例采用实际未认证、缺必填字段及缺文档/会话；没有放宽存储读回断言。

本轮 `make verify` 通过（Ruff、8 条 import contracts、async DB 门禁、mypy 126 文件，
3414 unit passed）；`make integration` 320 passed，无 skip，包括本项 4 个真实 HTTP
用例及隔离 API 的实际 `make smoke`。本项没有新增协议兼容层或 Go/web 修改。

## 4f6651968a4d3bd2d6635c048e1b5cf454b5221f · 会话变量默认值与 Explore 会话选择

- 上游 #14399，2026-04-27；2026-10-02 核对 origin 并 fetch，
  `origin/main` 为 `519e7d98a5651564d4e35d6648f006cba4baaf4f`。
  从本仓 `4bcac86494b9470af2f961c62ab9e067d974f5bc` 开始，核对目标两个文件完整 diff。

| 上游 diff | 本项处置 |
|---|---|
| `agent/canvas.py:Canvas.reset` 从变量定义恢复 env 默认值，不再改定义 | 采用后修 `c949096db` 的 `value is not None` 和类型兜底，保留 False/0/空值；object/list 深拷贝，隔离真实 VariableAssigner 的原位 append。补齐定义有但 globals 无的键，序列化保存 globals；不改变 sys 清理及 mem 参数边界。 |
| `web/src/pages/agent/chat/use-send-agent-message.ts` 优先 Explore session ID | 独立 web 由根聊天另派。本仓实际消费者为 `src/pages/agent/explore/hooks/use-explore-session-chat.ts` 与 `src/api/agent.ts`，交接下列现行契约，不修改 web。 |

没有查到目标行为的 revert/re-land。相关链逐项处理：

- `c949096db038f11d44b969902da440a800a75a3f` 修 truthy 判断：采纳，
  同时修本地可变默认值别名问题，避免运行后定义被 append 污染。
- `c1ee17bebc95d708263df3c04f97c3e0520e3d86` 删除 reset 中变量打印：采纳；
  同提交 Invoke 代理改动属于另一行为，不合入。
- `c50f9c59aae2a2ef1da356563a14acca084d255d`、
  `c15241c3a9226c1d9d94f213f1c1689e382bd791` 的新会话清 history/sys.history/path：
  本地 REST 显式建会话与 completion 首次会话已有完整 reset，真实 HTTP/SQL 验证复用；
  已有会话装载自身 DSL 且不 reset。相关 Categorize/MCP 改动不合入。
- `decb5dcb6f25a2be7d92d33277edc444d2cf961b` 与
  `8a3699fa87d3842e6981d1313216674b86d9d81f` 改暂停路径组件 inputs/outputs 的逐轮重置：
  属于 UserFillUp 暂停/续跑的组件状态规则，本项目标只改变完整 reset 的 env 默认值；
  没有搬入该相邻调度重构。后续 Python 后端整体删除不在范围内。

本地消费链：REST 显式创建会话、普通发布首次运行、已有会话运行、OpenAI 消息适配首次运行、
显式 reset、组件 debug 均执行真实 Canvas/组件。SDK `create_agent_session` 没有注册 HTTP 路由，
本项直接调用实际 helper 验证 scratch 事务；修复其把 reset 结果赋回 ORM Canvas 的问题，
使新会话运行 DSL 独立保存，不会随 save 提交覆盖草稿。没有新增旧 SDK 路由。
web serializer 把默认值放入 `variables[name].value` 与 `globals["env.name"]`，本次格式无需迁移。

Go 核对 `internal/entity/canvas.go` 的 JSONMap DSL、`internal/dao/user_canvas.go` 的通用 CRUD、
`internal/router/router.go` 的 CanvasService TODO、`internal/handler/memory.go` 与
`internal/service/memory.go` 的 Canvas 消费 TODO，以及 `cmd/` 和 handler/service 全部入口。
本仓没有 Go Canvas reset/VariableAssigner 执行链；`internal/server/variable.go` 是服务密钥配置，
不是 env 会话变量。目标也无 Go diff，不新增空实现，无 Go 改动或专项构建声称。

现行前端合同见 [HTTP 参考：会话变量默认值与重置](../references/http_api_reference.md#会话变量默认值与重置)：

- 新会话 POST `/api/v1/agents/{id}/sessions`，body `release`，成功 `retcode=0/data.id`；
  当前所选 Explore 会话 ID 放入 POST `/api/v1/agents/chat/completion` 的 `session_id`，
  与 `agent_id/query/stream` 同传。普通 SSE 事件归属发起请求的 session，
  切换会话后不得把在途流的 ID/history 写入另一个已选会话。
- `variables` 是定义，`globals["env.*"]` 是会话运行值；旧会话保持自己的 DSL，
  新会话/reset 恢复默认值。发布与草稿选择、开流前授权/失败及运行 error 合同沿用第五项。
- POST `/api/v1/agents/{id}/reset` 只重置 SQL Canvas，不清已有会话、版本或 Redis 编辑器副本；
  component debug 从 SQL 创建临时 reset 实例且不持久化。普通无 session 且未发布的 run
  消费 Redis 编辑器副本，不能被声称为新会话 reset；草稿新会话需显式先创建。
- 独立 web 正在另项修改；本项只有后端验收，没有浏览器 E2E 或前端完成声明。

验证：改动前 35 个真实 Canvas 单测及 4 个 HTTP 默认值用例均复现失败；
修复后的 35 个变量单测与 6 个既有 release 单测通过。新增 11 个集成用例
（9 个真实 HTTP、2 个实际 SDK helper）通过，覆盖类型兜底、空值、mutable append、
重复 reset、已选会话续跑、其他会话不变、发布/草稿首次运行、显式 reset/debug 及私有画布拒绝。
SQL 独立读回确认定义默认值不被改写、旧会话运行值累计、新会话恢复默认值，
Canvas/发布快照/Redis 保持对应边界；无远程 LLM 调用。
`bb2431f5` 的 `make verify` 通过（Ruff、8 条 import contracts、mypy 126 个源文件、3393 单测）；
`make integration` 312 passed，无 skip，含既有 52 个发布/授权/错误回归和真实隔离 API 的
`make smoke`。首轮 verify 只因两份新测试格式失败，局部格式化后复跑通过。
fixture 确认 scratch 用户/token/成员关系/Canvas/版本/会话、专属 Redis key 和监听清理，
一次性 scratch 数据库由共享 fixture 删除。未部署或操作生产数据；其他任务改动保留。

### 历史空列表模板补修

根审查发现 `bb2431f5` 新增的 keys 联合对 `variables` 无条件调用 `.keys()`，
仓内 11 个真实模板（包括 `data_analysis_beginner_assistant.json`）仍使用 `variables: []`。
真实 `normalize_chunker_dsl` 和 `CanvasReplicaService.normalize_dsl` 保留该形状，
`api/db/init_data.py:add_graph_templates` 会种入模板，REST 模板列表供用户创建 Canvas。
本次真实模板→规范化→完整 Canvas 构造→reset 均复现 AttributeError，
另有 4 个真实 HTTP 建会话用例返回失败。它是本次引入的模板回归，不沿用旧门禁结论。

最小补修只在 `Canvas.load` 把历史空列表转换为运行视图的空映射，
原 DSL 和序列化结果保留 `variables: []`。缺省与空 dict 继续可用；
空 list/dict 及缺省定义的 orphan env 键在 reset 时清为 `""`，已有会话装载/续跑保留运行值。
非空列表、null、字符串和数字没有被静默归一化，当前 reset 对这些非映射形状仍报错；
本项没有新增统一 DSL 参数校验。模板文件、provider 行为、SDK helper、组件调度均无生产改动。

验收边界：11 个真实模板保持全部实际组件、参数检查和 reset，
只替换模型配置 DB 及 provider 构造边界，检查模型假件已成功装配，不接受缺模型异常作为通过。
模板的模型/文档解析/沙箱工作流没有执行；HTTP 验收使用真实 Begin/Message 的确定性 DSL
并保留 `variables: []`，覆盖草稿/发布建会话、首次发布运行、已有 list 会话多轮续跑、
重复 reset/debug、成功终态和独立 SQL/Redis 读回、旧会话/版本/草稿副本边界。

本补修改动前专项复现 13 个单测失败（11 个真实模板、2 个空列表/orphan 组合），
缺省/空 dict 的 4 个对照用例通过；4 个 HTTP 用例均在建会话时失败。
修复后变量单测 56 passed，包含全部 11 个真实模板、6 个无变量形状/orphan 组合、
4 个非法非映射形状及原有 35 个默认值回归；4 个 HTTP 用例通过。
本轮 `make verify` 通过（Ruff、8 条 import contracts、mypy 126 个源文件、3414 单测）；
`make integration` 316 passed，无 skip，包含全部 67 个 Agent 发布/授权/错误/变量用例
及隔离 API 的实际 `make smoke`。已有会话的 orphan 运行值、历史和轮数继续，
新会话清理初始状态；每轮读取 SQL 确认其他会话、原 Canvas、发布快照和 Redis 副本边界。
重复 debug 不写状态，显式 reset 只写 SQL Canvas，所有会话/版本/Redis 读回保持原合同。
fixture 检查专属 SQL 行、Redis key、HTTP listener 已清理，共享 fixture 删除 scratch 库。
本轮没有修改真实模板、独立 web 或生产数据；测试只替换上述模型配置/provider 边界。

## 10e28e5c5f007f12df0cfa1ec36f307341b7316b · nginx 配置源挂载

- 上游 #14361，2026-04-27；2026-10-02 核对预期 origin 并 fetch 后，
  `origin/main` 为 `519e7d98a5651564d4e35d6648f006cba4baaf4f`。
  从本仓 `8060cd8837738a9d0d2cee8a50a2aadf3cf2f304` 开始，核完整两个文件 diff。

| 上游 diff | 本项处置 |
|---|---|
| Helm 工作负载的 mountPath/subPath 改为 `ragflow.conf.python` | 本仓无 Helm chart。修同类活动 macOS Compose：Python 配置源只读挂到 `multirag.conf.python`，保留 `multirag.conf` 为容器可写生成目标；nginx 主配置/proxy 配置也只读。 |
| ConfigMap key 同步改为 `ragflow.conf.python` | 本仓用宿主文件 bind source，没有该 ConfigMap。Linux CPU/GPU 覆盖示例、HTTP/HTTPS 源挂载注释和 Docker 运行文档同步正确路径。 |

目标树中 nginx 主配置仍 include 生成目标，上游 entrypoint 从选中源复制。
未发现本项 revert/re-land。后续 `d441aa033` (#19126) 禁用 macOS 的旧目标挂载，
修其源文件已消失导致的启动问题；本仓源文件仍在，但旧目标挂载会覆盖宿主源，
因此保留本仓活动覆盖能力并修到正确源路径。`53c4f55ff` 后整体删除 Helm chart，
未消除本仓的 Compose 问题；本项不引入 Helm 部署体系。

实际消费者核对：根 Dockerfile 将 python/golang/hybrid 三套源打进镜像；
`docker/entrypoint.sh:start_nginx` 缺省 python，按模式复制为 `multirag.conf`，
`docker/nginx/nginx.conf` 只 include 该生成文件。普通 Compose CPU/GPU 示例默认
注释，macOS 活动覆盖已修；standalone Compose 不覆盖 nginx 文件，原模式选择保持。
`Dockerfile.dev/new` 也调用入口脚本，但没有三套 nginx 源，未找到当前 Compose/CI
引用，本项不扩展这两个旧构建配方。稳定路径映射和 API 消费合同未改变，web 无修改。

Go 具体核对 `cmd/server_main.go`、`cmd/admin_server.go`、`internal/server/config.go`、
`internal/router/router.go`、`internal/handler/system.go`、`internal/admin/handler.go`：
Go 主服务端口为 Python http_port+4（8127），admin 为配置端口+2（8132），
两个 ping 路由存在；入口的 start_server/start_admin_server 根据同一模式启动进程。
三套站点源及端口已匹配，无需 Go 代码改动。本次用真实 nginx 验证选择/分流：
Go ping/admin 分别到 8127/8132；hybrid ping 到 Python 8123、admin ping 到 Go 8132，
`/v1/system/config` 到 Go 8127、admin roles 到 Python 8130。
Python 源 bind 没有盖掉内置 Go/hybrid 源；无 Go 源码变更，未运行 Go build/vet。

本轮运行验收边界：本机无 `multirag:latest`/依赖镜像；已有 `multi-rag-api:local`
是另一项目镜像且无 nginx。构建专用 linux/amd64 Ubuntu 24.04 镜像，使用根 Dockerfile
同一官方源和钉住的 nginx `1.29.5-1~noble` 包、当前三套配置和入口脚本。
实际执行当前入口的定义区及 `start_nginx`，未运行 DB 初始化、API/admin/worker 启动；
8123/8130/8127/8132 上游是隔离 HTTP 合同假件，用响应头/载荷标明接收端口。
因此以下证明 nginx/配置合同，不是完整 MultiRAG/Go 服务启动或真实 DB 健康证明。

- 旧路径在 scratch 宿主文件复现：可写目标挂载被真实 cp 覆盖；只读目标挂载复制失败
  (`Device or resource busy`)，源 hash 不变。没有改动仓库宿主源。
- `docker compose config` 分别读回 macOS CPU、Linux CPU/GPU；macOS 三个 nginx 源
  的 source/target/read_only 正确，Linux 默认无覆盖，均不挂生成目标。CPU/GPU 分别选择，
  两者同开会因既有同 container_name 冲突，不作为本项支持的组合。
- 隔离 Compose 项目执行缺省、python、go、hybrid 和 Python HTTPS 五种启动；
  每次 Docker inspect 确认源 RW=false、无目标 bind，实际写源被拒绝。
  独立读生成文件与选中源逐字相同，`nginx -t` 通过，宿主各源 SHA256 前后相同。
- 每种模式从真实 nginx 入口请求 `/api/v1/system/ping`，得到原 `pong`，并核接收端口；
  admin/config/roles 代理分流及 HTTP 200 中假件业务码 0/73 均保留，未把 200 当业务成功。
- HTTPS 缺证书时真实 nginx 拒绝启动；临时自签证书加入客户端信任后，TLS ping/代理
  请求、`nginx -t` 和 HTTP 301 重定向通过。生产域名/CA 证书未验收；模板明确只适用
  Python 8123/8130，HTTP 源挂载被替换而不是与 HTTPS 占用同一目标。
- HTTP 缺省和 HTTPS 两次 `make smoke` 通过；healthz 的 `db=ok` 来自上述假件，
  只证明 nginx 入口的 smoke 合同，不报告 SQL 或真实 API 服务健康。

本轮 `make verify` 通过：Ruff、8 条依赖契约、async DB 门禁、mypy 126 文件，
3358 unit passed。DB/存储未改，未重复 integration；无 Helm template/deploy 目标。
隔离容器/网络和临时证书已检查删除，验收镜像随后移除；保留运行日志供核对。
没有启动/改动生产或其他任务服务，`docker/docker-compose-base.yml` 的他人改动保留。
第七项 `4f6651968a4d3bd2d6635c048e1b5cf454b5221f` 等待派发。

## 0f2778efe744b5aef879f1743c3ec50fd1143aab · Agent 更新支持发布

- 上游 #14396，2026-04-27；2026-10-02 确认 origin 并 fetch 后，
  `origin/main` 为 `519e7d98a5651564d4e35d6648f006cba4baaf4f`。
  从本仓 `771d6864` 开始，完整核对指定 SHA 的一个文件、两处 diff。

| 上游 diff | 本项处置 |
|---|---|
| `api/apps/restful_apis/agent_api.py:update_agent` 归一化 `release` 并更新 Canvas | 保留字段并写入 Canvas；兼容明确的布尔字符串，避免上游 `bool("false")` 误发布。DSL 保存缺省/空值为草稿；纯元数据增量更新保留现有发布标志。 |
| 同方法把 `release` 传给 `save_or_replace_latest` | 同一个同步 Session 保存 Canvas 与版本；release-only 使用当前 DSL 保存/发布。请求持有 owner Canvas 行锁，版本服务支持 `commit=False`，异常向调用方传播；插入、版本清理和 Canvas 更新一次提交，失败回滚。 |

当前本地的关联读回缺口一并修复：`UserCanvasService.get_by_canvas_id` 的选列新增
`release`，GET 返回当前状态；首次 `/agents/chat/completion` 的 `release=true`
进入已有会话/发布版本运行链，避免使用 Redis 草稿副本。已建会话继续使用其自己的 DSL。
相同 DSL 重复发布不另建快照、版本标题不变；发布后草稿另建版本，已发布快照不删除，
草稿最多 20 个。新快照的创建顺序在同毫秒保存时仍明确，更新时间不早于创建时间。
API 的请求、响应及 Redis 部分失败合同见 [HTTP API 参考](../references/http_api_reference.md#更新与发布画布)。

未找到本项 revert/re-land。后续 `569a29500` (#17576) 改更新响应为 update_time，
`a0438517b` (#18056) 补组件参数验证，分别属于相邻目标；本项保持当前 `data=true`
与 DSL 归一化合同。版本服务后续 `006747090` 修正无序分页，未回退发布行为；
上游整体删除 Python 后端不在本次范围。后续 nginx 源挂载修复见 `10e28e5c` 项记录。

调用方核对：旧 `/v1/canvas` 只保留任务取消，无 set/get/save 消费者；REST 是当前
唯一创建/更新链。复制/模板导入经 POST 创建新画布，未把原发布历史复制到新 ID。
SDK API Key 通过同一 PUT 入口及 owner 校验，实际 HTTP 已验证；`api/apps/sdk/session.py`
的已发布 DSL 选择帮助函数与 `canvas_service.completion` 都使用最新 released 快照。
独立 Python SDK 工作树 `multirag-rest-first-python-sdk` 的 `d0d34818` 无 Agent 更新资源。
独立 web `5c7a77382cb87400bf70ff492b1b310cb89ce3bd` 的 `src/api/agent.ts:setAgent`
透传 release；主编辑器与嵌入编辑器发布时传 true，普通保存省略。`useSetAgent` 成功后
失效详情及版本查询，详情/发布组件读取 release/last_publish_time，API client 兼容 retcode。
没有发现需修改的前端合同，本项不编辑 web，也未做本项的真实浏览器验收。
列表重命名目前也发送 DSL，按既有“保存 DSL 即草稿”语义处理；仅元数据请求仍保持发布状态。

Go 实际核对 `internal/router/router.go`、handler/service、`internal/dao/user_canvas.go`、
`internal/entity/canvas.go` 和 `internal/cli/{parser,client}.go`：无 Agent 更新、发布、版本
或运行路由及服务消费者。DAO 只有泛用 Canvas CRUD，UserCanvas entity 已有 release，
版本 entity 尚无 release；CLI 的 LIST AGENTS 仅解析，执行分派落入未实现分支。
没有相同目标链，本项不新增 Go API/字段，不运行无改动的 Go 编译门禁。稳定路径映射未改变。

验证与实测：unit 覆盖 true/false、字符串、非法类型、缺省/None、元数据与 release-only，
并拒绝版本失败回执。隔离 PostgreSQL scratch 库中的真实 HTTP 完成发布、相同 DSL
重复保存/发布、改 DSL 草稿、再次发布、GET/版本列表与独立 SQL Canvas/version 读回；
缺/坏认证、其他 owner JWT/API Key、非法 release/DSL 均无错误写入。
发布后改草稿，再创建 release=true 会话，独立 SQL 确认 session DSL/prologue/version_title；
首次发布运行用真实 Begin/Message 输出旧发布内容，Redis 独立读回仍为新草稿。
没有模型 provider 假件或远程模型调用。
真实 PostgreSQL trigger 分别拒绝版本 INSERT、Canvas UPDATE、版本清理 DELETE，
HTTP 返回非零 retcode，Canvas/版本/Redis 均保持原值；移除 trigger 后重试成功。
Redis 同步 False 回执另有验收：返回“已保存但副本同步失败”，数据库已提交，
重试恢复副本；数据库与 Redis 不是分布式事务。未声称生产部署或生产数据验收。
全部临时账户、membership、token、Canvas、版本、会话、trigger/function、自身 Redis key
和 HTTP 监听均已清理并检查。隔离 API 内实际执行 `make smoke` 通过。

最终门禁：`make verify` 通过（Ruff、8 条依赖契约、async DB 检查、mypy 126 文件、
3346 unit passed）；`make integration` 253 passed、无 skip。最终树复跑了上述
真实 HTTP/SQL/Redis 与运行验收，含 `make smoke`；没有沿用历史通过数。


### 2026-10-02 同 SHA 运行链补修

从本地 `57a2fafaed346968cce911d20a3d0cd6ad227d5c` 补修，未开始下一 SHA。
隔离 HTTP 先复现：owner 无发布版本、Agent missing、私有画布外部用户、非成员
首次发布运行，非流式均为 HTTP 500。原生成器到消费时才做 setup，流式也可能在
已发 200 后失败；初次测试只覆盖 owner + 已发布版本，未覆盖这些拒绝路径。

`prepare_agent_run` 用请求 AsyncSession 在开流前核对 Agent 存在、owner 或
permission=TEAM 且属于 owner 团队的访问权，读取已发布快照或已有会话的 DSL。
它返回纯值 `PreparedAgentRun`，DSL 与版本标题来自同一版本行；实际执行直接消费，
不再查询一次 latest。模型/组件资源使用 Agent owner 的运行租户，权限使用认证调用者。
准备完成后先推进生成器完成 Canvas 构造与 setup，再发送 SSE 响应头。
普通 REST 已有会话也进入这条准备链，检查 session 与 path Agent 的绑定并保留原 DSL。
SDK/显式创建会话、PUT 更新/发布的 owner 校验未放宽；旧 OpenAI-compatible 适配分支未改变。

开流前返回 JSON：无/失效认证 401 + code401；missing 404 + retcode102；
无权限或 session 绑定错误 403 + retcode103；无发布快照 409 + retcode102；
其他准备失败 500 + retcode100，均不报告成功。前三种准备错误用 HTTP 4xx，是因为
现有 web `lib/streaming/transport.ts:assertSSEResponse` 只按 response.ok 拒绝响应，
200 JSON 业务错误会被 SSE transport 忽略。保留 retcode/retmsg/data=false，现有
web 可以直接显示 retmsg。使用真实 web transport 函数回放 403/404/409 Response，
均按原 retmsg 抛错；这是消费合同回放，未做真实浏览器 UI 验收，未修改 web。

未处理的组件错误、显式 error 帧或运行异常在流式返回 event=error/code100/message/data.error，
随后的 DONE 仅表示传输结束；非流式返回非零 retcode，不再把错误文本包装成成功答案。
实际运行记录 user input 与 errors，不追加本次成功 assistant；成功 message_end 等持久化
之后才发，故结束后异常也不会先送出成功终态。运行生成器退出时显式关闭执行生成器，并取消自身任务。

本轮真实 HTTP + PostgreSQL/Redis 验收：stream=false/true 分别覆盖无发布、missing、
private（即使有 TEAM membership）、TEAM 非成员、缺/坏/失效身份；拒绝无新增会话、
无 Canvas 执行、自有状态不变。owner 和 TEAM member 分别运行真实 Begin/Message 发布内容，
独立 SQL 读回会话的 user_id、DSL、version_title、答案与 errors，Redis 草稿不变；
TEAM 的 PUT 发布和显式 POST sessions 仍拒绝。发布新版本后已有会话继续原 DSL。
在准备后实际插入新的发布版本，确认 stream 两种模式都运行已固定旧快照，并读回新版本
与旧会话标题，覆盖二次选择风险。已有 session missing、绑定到其他 Agent、private 和
nonmember 请求也在开流前拒绝，user_id 字段不能替代认证身份。

错误验收：真实 Message 的无效 Jinja 模板产生组件失败；异常和显式 error 事件在真实
Canvas 构造/首事件之后，于 Canvas.run 边界注入。没有替换 DB、auth、Canvas 构造、
会话写入或远程模型 provider；没有调用远程 LLM。这三类失败在两种 stream 模式均被消费者
识别，SQL 会话有 errors、无本次成功 assistant；Canvas/version/Redis 草稿保持原值。
unit 另覆盖 malformed 帧、非零 code、生成器关闭及 message_end 后异常。
复用隔离 HTTP 验收中的 make smoke；临时用户/membership/token/Canvas/version/session、
自有 Redis key 和 listener 在退出时删除并检查，无生产部署/数据操作。

本轮最终门禁：`make verify` 通过（Ruff、8 条依赖契约、async DB 检查、
mypy 126 文件、3358 unit passed）；`make integration` 287 passed、无 skip。
最终树包含以上真实 HTTP/SQL/Redis、运行及隔离 make smoke 验收，本项收尾未沿用旧通过数。

### 2026-10-02 同 SHA 已加入成员授权补修

从本地 `538b93f22d9da09377fc75582b92154a57fab2b4` 补修本项新准备链。
复核目标完整 diff 并刷新预期上游 origin，`origin/main` 仍为
`519e7d98a5651564d4e35d6648f006cba4baaf4f`；未开始第六项。
原 `prepare_agent_run` 只检查 UserTenant 行存在，沿用了旧 accessible 的缺口：
真实 POST tenants/users 邀请写入 `invite/status=1`，接受前并非已加入成员；
失效成员的旧记录也不能授予 owner 运行租户的资源访问。

修改前隔离 HTTP 复现：未接受邀请者首次/已有会话 × stream=false/true 共 4 例，
均返回 200 并成功执行；已接受 normal/admin 成员的团队关系置为 status=0 后，
相同矩阵共 8 例也错误执行。调用者本人的账号和个人 owner membership 始终有效。

本次只收窄新 `prepare_agent_run` 的非 owner 查询：`StatusEnum.VALID` 且
`UserTenantRole.NORMAL/ADMIN`，符合 UserTenantService 的已加入成员资源访问语义，
同时保留画布 TEAM 条件。owner 由画布所有者身份核对；首次发布与普通 REST 已有
session 共用同一准备链。未改全局 accessible、GET/versions、OpenAI 分支、SDK，
未放宽 PUT 更新/发布和显式创建会话的 owner-only 合同。

验收通过真实 REST 邀请、PATCH 接受、PUT 成员角色变更形成 invite/normal/admin；
仅在邮件调度边界记录调用，不发送外部邮件，不替换认证或数据库。成员删除接口物理删行，
故 status=0 旧记录只在 scratch 库种入：先真实加入并核角色，再更新目标团队关系状态；
调用者个人 membership 保持有效。无远程 LLM，运行使用真实 Begin/Message。

首次和已有会话分别验证两种 stream：invite、失效 normal/admin、private、非成员在
开流前返回 JSON 403 + retcode103/data=false；body user_id 不能替代认证身份。
拒绝不构造 Canvas、不新增会话，已有会话完整 SQL 读回不变，Canvas/version 与 Redis
草稿读回不变。owner、有效 normal/admin 在两种 stream 均可首次运行和继续原会话，
运行租户仍为 owner；再发布后旧会话保持原 DSL，更新/显式创建会话仍拒绝非 owner。
临时账号、成员关系、token、Canvas/version/session、自有 Redis key 和监听器均检查清理。
Go 当前只有管理查询/DAO，无对应发布运行入口，本轮不修改 Go；web 消费现有 403/error
合同，无前端改动或真实浏览器验收。

本轮最终 `make verify` 通过：Ruff、8 条依赖契约、async DB 门禁、mypy 126 文件，
3358 unit passed；`make integration` 301 passed、无 skip。其中本项 52 个真实 HTTP
用例通过，包含隔离 API 的 `make smoke`、独立 SQL/Redis 读回与清理断言；未沿用旧通过数。

## 61a24a2c14dde696244646e1ec69e5f150eeda54 · 聊天附件上传迁入 REST

- 上游 #14359，2026-04-27；本轮确认 `origin` 并 fetch，`origin/main` 为
  `519e7d98a5651564d4e35d6648f006cba4baaf4f`。目标代码取指定 SHA tree，完整核对 7 文件 diff。
  从本仓 `70dd89a4` 开始，只处理本项；下一项 `0f2778efe744b5aef879f1743c3ec50fd1143aab` 等待派发。

| 上游文件/行为 | 处置 |
|---|---|
| `api/apps/restful_apis/document_api.py` | 采纳 `/api/v1/documents/upload`，重复 `file` / query `url` 互斥且须其一，单对象/多数组。PR 描述的 `documentss` 拼写错误不采纳。FastAPI + 异步 Principal + 请求级 AsyncSession；直接 await 已有 async service，不复制上游线程池包装异步函数。 |
| `api/apps/document_app.py` 删除旧路由和导入 | 开工时 web 的 conversation/use-chat-upload/use-mcp-upload 使用旧路由，迁移期间先保留 deprecated。web `5c7a773` 完成真实跨端验收后，本项收尾删除 `/v1/document/upload_info` 及唯一专用导入 `UploadInfoArgumentError`。 |
| `test/testcases/test_web_api/test_common.py` | 不复制上游 HTTP harness；重建当前 unit fixture 与 scratch PostgreSQL/MinIO 的真实 HTTP 验收。 |
| `test/.../test_upload_info_unit.py` | 采纳测试意图，但不采用只看 data 存在、条件性数组断言；明确验证类型、元素字段、业务码、鉴权、owner、独立字节读回和实际消费者。 |
| `web/src/hooks/use-chat-request.ts` | 前端另派；本项核对并修复后端聊天/MCP 附件消费链，未修改 web。 |
| `web/src/services/next-chat-service.ts` | 前端 `5c7a773` 已完成代码与隔离跨端验收；准确 multipart/响应/认证契约，SDK 既有入口保留。 |
| `web/src/utils/api.ts` | 新 URL 已落实两仓，web 当前 tracked 文件无旧路径活动调用，真实上传和消费者验收通过。 |

未查到本项的 revert/re-land；后续 `a4f325be2` (#16264) 恢复旧路径兼容，
本仓初期据已查明活动消费者保留兼容；当前 web 完成迁移验收，按用户授权和已约定退出条件
删除旧别名。`e35860ad7` (#16269) 另补 Go 上传与 metadata batch；
其中 downloads descriptor、正确字节数、HTTP 错误与 URL 内容类型归一化作为交叉核验。
后续整体 Python 后端删除另属其他提交。已实际核对本仓 Go `internal/router/router.go`、
`internal/handler/document.go`、`internal/service/file.go` 与 `internal/cli/{client,http_client,contextengine/*}.go`：
Go 无 upload_info 路由/descriptor 消费者；活跃 CLI 文件 provider 使用 `/files`、dataset provider 使用
`/datasets/{id}/documents`，本次新增附件入口未改变它们的路径/数据合同，因此本项不改 Go。

必要本地适配：当前 REST 文档与 SDK 文件两个网关共用 `FileService.upload_infos`，
SDK `/files/upload_info` 保留字段 `files` 与已有鉴权；旧 web metadata 网关已退役。
新入口的 JWT / SDK Key 都校验
有效用户与个人 owner membership，`created_by` 使用服务器 Principal 的 platform_user_id。
存储仍为 `<owner>-downloads`，没有写入 dataset/File/Document 表或宣称解析完成。
`size` 改为实际 bytes 长度，URL 生成的 PDF 使用正确 MIME。
存储回执按现有 adapter 处理：GCS False 失败，MinIO 有效对象成功；S3/OSS/OpenDAL
的 None 要核对象存在，失败不返回附件 id。上传服务在同 owner 空间登记 `<id>.upload.json`，
登记失败补偿文件；批量失败只补偿本批 location 与描述，明确暴露清理未确认；
取消请求屏蔽对写任务的直接取消、等待完成并补偿，强制进程终止及存储不可达不保证回滚。

同项 MCP 最小适配：保持 `ChatRequest.files: list[str]`，按 Principal.platform_user_id
读取服务器登记，校验 id/owner/字段及对象存在；不信前端自造 descriptor、created_by 或
助手消息。真实 Canvas 读取/解析后，文字加入本轮用户消息，图片放入 visual_files_var
再走 Agent 的既有模型 images 路径。普通/structured、tools/no-tools 与相关非流式共用，
每次调用清空旧附件且预解析只消费一次，重复调用重新验证；缺失/越权/描述或 blob 缺失/
解析失败流前 HTTP 400。structured error 不再包装成功 retcode；流中错误发 500 后终止，
普通流亦不再跟成功完成帧。非流式 structured 实际走结构化分支并累积全文。

URL 保留原初始/DNS/redirect 防护，并修复 HTTP 预检与浏览器第二次请求之间的绕过：
浏览器每个 HTTP GET/HEAD 经 `common/safe_crawl.py` 转发，每个新目标与 redirect 在请求前
校验并 DNS 绑定，不用环境代理重新解析目标；浏览器旁路走自持有、拒绝连接的代理，
禁用 loopback bypass 和非代理 WebRTC UDP，并拦截 WebSocket。抓取失败、空内容和
安全阻断返回可识别参数错误，不存成成功附件。CI 的 integration job 安装 Chromium 运行资源。

实际验收：scratch PostgreSQL 的两位 owner、真实 JWT 与 API Key、随机 MinIO bucket/
owner key、随机 Redis descriptor key、完整 API 的临时 HTTP 监听（不启后台 lifespan）。
单文件与重复 file 的文本+PNG 多文件验证完整元数据、逐对象独立字节读回、错误 owner key 无对象；
实际 `split_file_attachments` 和 `Canvas.get_files_async` 解析文字/图片，再把 Redis 读回的
descriptor 交给消费者。核心阶段旧 web/SDK 入口亦实际上传并读回；退役收尾改为验证
旧路径拒绝且不写对象，REST/SDK 继续上传并独立读回字节和可信描述。缺/无效认证、失效 membership、
缺/混合/空字段、内网 URL、真实 MinIO 失败和第二次写失败均核业务码与无成功 data。
第二次失败后独立列举对象，证明本批无残留且既有对象仍在；unit 另覆盖 False/None 回执、
清理失败诊断、描述登记失败补偿和请求取消后无对象。真实补偿验收发现 nest_asyncio 的
Python Task 与 beartype 的 C Task 注解不相容，drain 边界改用 Awaitable，已复跑真存储补偿通过。

另用真实 HTTP 上传文本+PNG，独立 MinIO 字节/可信描述读回，再仅提交 IDs 到 MCP。
使用真实 ChatAgentAdapter、Agent、Canvas 和解析器，普通/structured、tools/no-tools、
流式/非流式 8 分支在模型调用边界捕获到了文件正文和正确 image data URI；REST/SDK
上传登记也经 ID-only 聊天实际消费。missing/foreign ID、删除 blob、真实损坏 DOCX 解析
失败均覆盖普通/structured/非流式，模型未被调用。provider exception/错误标记均无成功完成帧。
模型 provider 为捕获假件；tools 分支选用工具存在假件，没有调用远程 LLM 或外部 MCP 工具。

真实 Chromium 控制测试：只把已验证测试域的 HTTP transport 映射到本机 origin，
真实 SSRF guard 对其他目标保持启用；预检 200 后第二次返回内网 302、JS 导航、iframe、
图片、fetch 与 WebSocket 均未命中内网 trap，错误 code=101 且无存储对象；正常测试 origin
生成 PDF 并独立读回。外部 URL `https://1.1.1.1/cdn-cgi/trace` 亦实际抓取、存储、
解析后读到 `h=1.1.1.1`。`httpbin.org` 首次尝试因本机代理 Fake-IP `198.18.*` 被拒绝，
没有放宽共享防护；缺 Chromium 时明确失败，安装运行资源后验收成功。
所有 scratch 用户、membership、token、MinIO 对象/bucket、Redis key 和 HTTP 监听都在退出时清理。

核心提交 `691a889c` 门禁：`make verify` 通过（Ruff、8 个分层契约、async DB 检查、mypy 126 文件、
3324 unit）；带外部 URL 选项的 `make integration` 249 passed、无 skip。
隔离 HTTP 验收内实际运行 `make smoke` 通过；URL、真实浏览器防绕过、附件可信恢复与
模型输入捕获均在当前树复跑。未将旧运行结果当本次门禁证据。

2026-10-02 收尾再次 fetch，上游 `origin/main` 仍为上述 SHA。前端
`5c7a77382cb87400bf70ff492b1b310cb89ce3bd` 对后端
`691a889c` 的真实浏览器隔离验收完成。ExplorePage 文件选择器上传 TXT/PNG，实际
conversation/completion 的 FileService 解析结果进入 system prompt/images；MCPChatPage
上传后只传两个字符串 ID，经 Canvas 解析进入模型边界。structured 分支用临时入口挂载
相同 useMcpUpload/streamStructuredChat；模型 provider 为边界假件，未验工具配置 UI
或执行外部 MCP 工具。401 后恢复凭据重试、即时取消无可发送 ID、具同等模型配置的
第二 owner/missing ID 流前 400、普通/structured 错误非零无成功完成帧及非流式 500 通过。
独立 MinIO 逐字节/owner/size/可信描述核对 10 个前端对象和 2 个 SDK 对象；隔离库 10 张相关
SQL 表均为 0 行，测试桶/对象/描述清除，专用 Redis 清空后容器和匿名卷移除，四个自有监听关闭。
主浏览器 origin 存储清空；旧错误/失联标签页因策略或超时未强行处理，测试账号与有效凭据已失效。
该次核对未见旧别名消费者；当时仅移除该别名，保留 REST/SDK、
`/v1/document/upload_and_parse` 及 Agent 共用 helper。会话旧入口后续已在 343bda 项退役，
REST/SDK 与 Agent 共享附件提取继续保留，现行合同见 HTTP 参考。

退役收尾门禁：本轮 `make verify`（Ruff、8 个分层契约、async DB 检查、mypy 126 文件、
3323 unit）与 `make integration`（249 passed、无 skip）通过。后者在隔离 HTTP API 内
实际运行 `make smoke` 通过；旧路径返回 HTTP/code 404、data=null，不写对象，路由与
OpenAPI 均无该别名。REST JWT 单文件/API Key 多文件及 SDK `files` 继续 code=0，
逐对象独立读回字节、owner/size 和可信描述；MCP 内容消费回归保留。随机测试桶及对象/描述、
scratch 用户/token/membership、Redis key 与自有 API 监听退出时均已清理，scratch DB 由 fixture 删除。
首轮新断言误将统一 404 的 data=null 视作异常，按现有错误协议改为完整响应断言后复跑通过。

当前契约集中见 [HTTP API](../references/http_api_reference.md#上传运行时附件)。
本项未运行远程大模型或完整外部 MCP 工具调用。
没有修改既有 Agent 上传/同步 webhook 的鉴权和事务链。同步 webhook 尚有旧线程池调用
async upload_info 的既有签名问题，本项未为附件路径迁移扩大整个 webhook session 改造。

## c446c403deb749e8e290de83bbf5f18d29f9a265 · PDF bbox 分批与 OCR 裁图懒转换

- 上游：`infiniflow/ragflow` #14385，2026-04-27；核对完整 2 文件 diff。
  2026-10-01 核对 remote 并 fetch，`origin/main` 为
  `519e7d98a5651564d4e35d6648f006cba4baaf4f`。本项从 `eae7f094` 开始，
  下一指定 `61a24a2c` 等待另行派发。

| 上游 diff | 本项处置 |
|---|---|
| `deepdoc/parser/pdf_parser.py` 的 `img_np` 懒转换 | 采纳：OCR 检测仍需要一次图像数组；只在确实缺文本、需要裁图时再执行一次 `np.asarray`，同页多个待识别框共用该数组。保留本地旋转 quad 和乱码回退。 |
| 同文件的 bbox 分批与 `_to_global_boxes` | 采纳并适配：默认 50 页、保留明确范围与 100000 默认上界；大小窗口一律转换为原 PDF 全局 1-based 页码。累计高度跨批连续，文字/表格的全部位置和标签只偏移一次。加载下一窗之前释放整页图像和局部状态；返回裁图已是独立副本。 |
| `deepdoc/vision/layout_recognizer.py` 的 DLA 提前初始化 | 采纳配置选择及提前初始化控制，覆盖 `DEEPDOC_URL` 和旧 `TENSORRT_DLA_SVR`，并使现有 `forward` 走同一客户端。两仓与目标 tree 均缺 `dla_cli.py`，后续历史也没有可信协议实现：不采纳或编造远端传输实现，缺客户端时在本地模型/下载前明确失败，本地模式保留。假客户端只用于测试分支和后处理，不是远端可用证据。 |

`self.outlines = extract_pdf_outlines(fnm)` 是目标 diff 的原有上下文，本项保留其兼容性，
没有将其记作新增书签能力。本仓的 `2846a939` 页上界与明确范围、`0d87ceca` 书签路径
继续保留。上游后续没有修复小窗口与分批窗口页码不一致的问题，本项统一修正；
`26ce0ed54` 另对超大 OCR crop 做 OpenCV 尺寸限制，本项未提前移植该独立优化。
布局 recognizer 后续的垃圾门槛、科学 PDF 图像合并和 Python 删除不是本项目标范围。

必要本地适配：`core/flow/parser/pdf_chunk_metadata.py` 多栏排序改读首个选中页面的
`bbox_page_width`，避免从最后一批首张图取混合页幅的错误宽度；最终裁图就绪后不要求
整页图像仍存在。旋转表格重 OCR 原有代码混用 `page_from`，改为在窗口内使用
1-based 局部页码；最后统一偏移。跨页文字和表格保存每页的位置，不仅保留最后/首个页。
窗口重置清理文字、语言判断、布局、表格、图像及 PDF 引用；真实混合扫描/文本测试
证明下一窗能再次用嵌入文字。进度把批内 OCR/布局/表格反馈映射到整体范围，单调到 1。
空范围直接返回，无法确定总页数时明确失败，避免默认 100000 上界跑空窗。
运行选项在新 `common/deepdoc_config.py` 类型化读取正常应用配置，兼容上游环境名，
正整数校验与显式空 URL 语义见 [DeepDOC 运行说明](../../deepdoc/README_zh.md#pdf-bbox-分批与运行选项)。

验证：真实 53 页混合页幅 PDF，在批大小 1、7、50、100 下对照全量与非零范围
`[3,53)`、`[49,53)`；页号/末页、文字、布局、位置、标签、累计高度、裁图尺寸与
像素摘要完全一致，书签含第 51 页。弱引用观测当前窗口最多 7 张整页图像，返回时已释放。
另覆盖复用解析器、空范围/计数失败、无文本数组的文字页、同页两框只转换一次、
扫描→文字窗口、跨页文字/表格、旋转表格局部页码、多栏排序，以及两种远程环境名、
标准配置优先级、缺客户端失败和本地模型/下载回退。远程选择的测试明确禁止执行
本地模型构造和下载；没有真实远端推理验收。

独立进程渲染对照：同一 121 页 PDF，612×792 点，72 dpi（612×792 像素），
OCR/布局/表格推理与合并使用假件，PDF 渲染、文字提取、bbox/裁图真实执行。
三种窗口均返回 242 个 bbox、末页 121，包含裁图像素的结果摘要一致
（`3b3eb4a80d938e84b4a74f30aee9d8b0b1312efcae83859ca9489c08c605d614`）。

| 批大小 | 整页图像驻留峰值 | RGB 整页像素 MiB | 初始/峰值 RSS MiB | 解析秒数 |
|---|---:|---:|---:|---:|
| 7 | 7 | 9.71 | 581.80 / 703.62 | 0.60 |
| 50 | 50 | 69.34 | 574.88 / 796.89 | 0.40 |
| 1000（本样本全量窗） | 121 | 167.80 | 571.73 / 838.67 | 0.40 |

最终代码复跑批大小 7：结果摘要相同，驻留仍为 7 张，初始/峰值 RSS
564.62 / 710.73 MiB、解析 0.59 秒；单次 RSS 受依赖与运行环境影响有波动。
这些是单次本机进程观测，初始 RSS 含依赖加载，峰值还包括 bbox/裁图副本和 PDF 对象，
不能用整页像素上限代替进程总内存上限。复现命令入库到
`tests/manual/pdf_bbox_batch_memory.py`，每种批大小应在独立进程运行。
另用本地模型执行 2 页、72 dpi、每批 1 页的文本 PDF，实际 OCR/布局与文本合并
成功返回页 1–2 的 bbox；这不是大型高分辨率完整 OCR 或复杂长表的模型压力验收。
flow 的 preview restore 仍全文渲染 216 dpi；全部输出裁图随返回列表驻留，跨批文本/长表
合并粒度也可能变化，本项不声称完整 flow 的内存压力或跨批合并语义已全部解决。

交付：最终代码 `make verify` 通过（Ruff、8 条 import contracts、async DB 门禁、
mypy 125 个源文件、unit 3256 passed）；由于 bbox 位置会进入后续索引元数据，
另跑 `make integration`，246 passed、无 skip。没有更改启动、路由或健康检查，
未加跑 `make smoke`。文档链接、scoped diff 检查通过；没有修改 web、Go、上游工作树
或其他会话的 Channel 文件。

## 4303be223fba929fe2982249ce6faafd764cd1b3 · 保留升级后的元数据 Schema

- 上游：`infiniflow/ragflow` #14383，提交于 2026-04-27；完整 diff 仅含
  `rag/svr/task_executor.py`。2026-10-01 fetch 后 `origin/main` 为
  `519e7d98a5651564d4e35d6648f006cba4baaf4f`。
  从本地 `c1c24fe1` 开始处理，本项止于该 SHA，下一指定 `c446c403` 等待另行派发。

上游修复 v0.24 → v0.25 升级后 `list(metadata_dict)` 误把 JSON Schema 变成键名
列表的问题。本仓原链已用 `turn2jsonschema` 接受 schema，但没有合并
`built_in_metadata`。现由 `common.metadata_utils.build_metadata_config` 在每次解析
构建有效配置；缓存读取、缓存写入和生成 schema 都来自这份配置。

| 输入 | 当前行为 |
|---|---|
| Schema dict，`properties` 为 dict | 保留 `$schema`、`required`、`additionalProperties`、组合约束及用户属性；把内置字段转换后并入属性，同名内置属性优先。 |
| Dict 的 `properties` 缺失或非法 | 归一为 `{type: object, properties: {}}`，再合并内置属性，不保留失效 schema 的其他约束。 |
| 旧字段 list | 拼接内置字段 list，保持既有列表转 schema 规则。 |
| 其他类型 | 使用内置字段 list。关闭提取或最终无属性时跳过模型和缓存，不写入元数据。 |

配置合并和提示词枚举注释使用独立副本，避免修改原 parser_config、跨切片共享的
schema 或缓存键。`gen_metadata` 能处理没有 description 的 enum 属性以及合法的
布尔属性 schema。生成结果从切片中消费并删除 `metadata_obj`，不传给索引；
空模型结果不再触发 KeyError。多切片合并沿用字符串和标签列表规则，原有标量字段
参与最后一轮合并；数字、布尔值、null、对象和结构化列表均可保留。结构化值按原子
值处理，同名已存在时保留先写入的值，避免把 PDF outline 和字符串标签拼在一起。
保存返回 False 时任务报错，不发送元数据完成进度。

后续链核对：`f0cb7a544`、`b36314699` 将上游执行器拆层，合并配置仍进入缓存与生成；
`3eff41361` 修复列表字段的 enum/description 为 null 时失效，本项包含该必要修复；
`e9cace9a0` 修复元数据合并丢弃非字符串值，本项包含对应语义，并保留合法空数组。
没有发现目标修复被回退。`c8d1b21ae` 另增加 file_name/update_time 的确定性填充，
属于独立行为，本项不提前移植；Python 删除与 Go 迁移也不在范围内。

调用链核对：文件上传与 REST 文档创建沿用完整 KB parser_config，
`TaskService.get_task` 从 Document 读取配置；KB/文档设置接受 schema 或旧 list，
文档列表与 KB 详情的 `turn2jsonschema` 消费继续兼容。专用 REST 自动元数据
`fields` 接口仍为列表契约，本项没有将其改成 schema 编辑器。`gen_metadata` 当前
只有标准任务解析这一处调用；dataflow 在索引前聚合并删除临时 `metadata`，复用相同
合并函数，因此也保留非字符串值。`analyze_v2` 的 metadata_fields 是独立提取配置，
没有套用本次 schema 合并。Milvus 配置下经 `DocMetadataService` 写独立
`t_ai_document_metadata` 表；ES/Infinity 继续使用既有 metadata store，本项未改变
其映射或写入协议，也未修改 web、Go 或其他会话的 Channel 文件。

验证：新增单测覆盖 schema 顶层与字段约束、同名内置属性、旧列表、非法/空配置、
null 字段、提示词不改配置、缓存读写参数一致及内置字段变化、空回复、关闭提取、
元数据合并与保存失败。新增 5 个集成场景，在一次性 PostgreSQL scratch 库创建
Tenant/KB/Document/Task，由 `TaskService.get_task` 取任务，真实 MinIO 取件和 naive
文本解析，经真实 Redis 缓存、实际元数据 service 写入，再用独立 Session 和表记录
读回。Schema、旧 list、非法 properties、仅内置字段、空配置均覆盖；第二次解析
命中缓存，修改文档内置字段后重新生成并读回新增字段。模型和模型配置查询使用假件，
未调用真实 LLM，也未运行后续 embedding/向量索引。临时记录、对象、bucket、缓存键
及 scratch 数据库均在验收后清理。

交付门禁：`make verify` 通过（Ruff、8 条 import contracts、async DB 门禁、
mypy 124 个源文件、unit 3229 passed）；`make integration` 通过（246 passed，
无 skip）。本项不涉及启动流程、路由或健康检查，未加跑 `make smoke`。
ES/Infinity 未做本次真实后端写入验收；本次解析与持久化运行证据对应 Milvus 配置下
的 SQL metadata store。独立 web 工作树保持干净，scoped diff 与文档路径检查通过。

## d88f7ac8d2a573997d8a9c46e077ff068cbb38b4 · 删除未使用的旧评估与 KB 入口

- 上游：`infiniflow/ragflow` #14394，提交于 2026-04-27，父提交为 `290f0294`。
  2026-10-01 核对 remote 并 fetch；`origin/main` 为
  `519e7d98a5651564d4e35d6648f006cba4baaf4f`。核对目标 3 文件完整 diff（1500 行删除）。
- 初次提交 `1e25c6a0` 依旧兼容规则仅标记评估 API 为 deprecated。用户确认应删除
  查明无调用的接口后，本项收敛为实际删除，并更新共用 Skill 的逐接口退役判据。
  本项止于这个 SHA，后续提交单独派工。

| 上游 diff | 当前处置 |
|---|---|
| 删除 `api/apps/evaluation_app.py`（479 行） | 删除本仓同名文件，移除全部 17 个 `/v1/evaluation` 操作。web、可见 SDK、MCP、HTTP/benchmark 和后台均未发现消费者；用户明确下线这个未使用的入口，无需新增替代 API。评估 service、4 张数据表和历史数据保留；入口退役没有执行数据清理。 |
| 删除 `api/apps/kb_app.py`（446 行） | 上游父提交中 10 个旧操作已在三引号注释内。本仓删除对应 10 个 handler、6 个专用请求模型及无用导入，清理其旧引用和 async DB 基线条目。剩余 16 个本地扩展入口保留，包括 web 实际使用的文件日志能力。 |
| 删除 `test_evaluation_routes_unit.py`（575 行） | 本仓没有该 Quart 假件测试文件。保留已有 REST/鉴权/存储回归，增加真实 FastAPI 27 个已移除操作的 404、OpenAPI 边界及文件日志业务响应/鉴权测试。 |

已移除的评估入口（路径前缀 `/v1/evaluation`）：

| 操作组 | 删除范围 |
|---|---|
| 数据集（5 个） | `POST /dataset/create`、`GET /dataset/list`、`GET/PUT/DELETE /dataset/{dataset_id}` |
| 案例（4 个） | `POST /dataset/{dataset_id}/case/add`、`POST /dataset/{dataset_id}/case/import`、`GET /dataset/{dataset_id}/cases`、`DELETE /case/{case_id}` |
| 运行（5 个） | `POST /run/start`、`GET /run/{run_id}`、`GET /run/{run_id}/results`、`GET /run/list`、`DELETE /run/{run_id}` |
| 分析与导出（3 个） | `GET /run/{run_id}/recommendations`、`POST /compare`、`GET /run/{run_id}/export` |

上游的 `POST /evaluate_single` 在本仓从未注册，不新增空成功桩。知识库 REST 数据集
不承接评估数据集；本次下线不声称评估能力已迁移。旧评估导入和 `run/list` 的已有问题
不再通过 HTTP 暴露，保留的 service 没有在本次任务中重构。

已移除的 KB 入口（旧前缀 `/v1/kb`，替代前缀 `/api/v1`）：

| 旧操作 | 当前替代 |
|---|---|
| `POST /create` | `POST /datasets` |
| `POST /update` | `PUT /datasets/{id}` |
| `POST /list` | `GET /datasets` |
| `POST /rm` | `DELETE /datasets`，请求体 `ids` |
| `GET /{kb_id}/knowledge_graph` | `GET /datasets/{id}/graph/search` |
| `DELETE /{kb_id}/knowledge_graph` | `DELETE /datasets/{id}/index?type=graph`；新契约同时处理任务绑定 |
| `POST /run_graphrag`、`GET /trace_graphrag` | `POST/GET /datasets/{id}/index?type=graph` |
| `POST /run_raptor`、`GET /trace_raptor` | `POST/GET /datasets/{id}/index?type=raptor` |

消费者核对：独立 web `fa30aef2` 工作树干净，`knowledge.ts` 已用 REST 管理 KB，
`knowledge-index.ts` 已用统一索引 API；上述 10 个旧操作及评估 API 均无调用。
文件日志页面 `use-log-list-state.ts` → `knowledge-ingestions.ts:listFileLogs` 仍调用
`POST /v1/kb/list_pipeline_logs`，现有 REST 摄取列表不含这些文件下载日志，因此保留
该能力。两个可见 SDK checkout（`multirag-python-sdk-v1`、`multirag-rest-first-python-sdk`）、
MCP、HTTP 参考和 Go benchmark 使用数据集接口；Python benchmark 直接使用知识库
service 和检索器。未发现后台导入被删 handler 或通过其他入口调用评估 service。

Go 核对：旧 KB update/graph 有独立的鉴权 router、handler/service；CLI 还有 KB
标签与元数据请求。评估实体参与 DAO 初始化，没有评估 handler/service。目标没有 Go diff，
本次 Python API 删除不修改 Go 的独立实现。

后续链：两份旧 API 文件未恢复；`faf77a5a8` 后来补评估 token usage，
`a0e65637e` (#16614) 再删评估 service，`670e68872` 移除更多 Python API。
本项只跟进 API 删除，不提前删除本地 service、数据表或迁移到 Go。

验证：删除后的 `make verify` 通过（Ruff、8 条 import contracts、mypy 124 个源文件、
unit 3207 passed）；async DB 基线随被删图谱路由收缩 1 条，存量由 58 降为 57，
门禁仍检查新增违规和过期条目。`make integration` 通过（241 passed）。
一次性 PostgreSQL scratch 库启动真实 HTTP API，`make smoke` 通过，全部健康组件 `ok`。
实际请求逐项确认全部 27 个旧操作返回 404；OpenAPI 无评估入口，KB 保留 16 个操作。
文件日志业务码 0、匿名请求 401；REST 数据集创建/列表/详情/更新/删除、空图谱读取、
GraphRAG/RAPTOR 状态读取与解绑成功，无文档的索引启动返回明确非零业务码。
写入、删除和解绑均独立 SQL 读回，预置评估数据行仍存在。未运行真实模型驱动的完整
GraphRAG/RAPTOR 构建；任务排队、分派与权限由既有单元及集成回归覆盖。
AST 对比确认剩余 KB handler 与请求模型行为未改；文档路径、链接和 scoped diff 检查通过。
临时进程和数据库已清理，本项未修改独立 web 工作树。

前端交接：上述旧操作的调用方已迁移，本项无需前端修改。将来退役其余 KB 本地入口
时逐项查实际调用；文件日志需先建立等价 REST 契约，再迁移 `listFileLogs` 及页面消费。

## 290f0294d6e043f64fb1c79b5780421cfc48d045 · 沙箱产物下载迁移到 REST

- 上游：`infiniflow/ragflow` #14348，提交于 2026-04-27；核对目标提交的 4 文件完整 diff。
  2026-09-27 fetch 后 `origin/main` 为 `313ca90f6abd7682fe8523e16fd67b3653a3fa84`。
  本项止于此提交；下一指定 `d88f7ac8` 单独处理。

| 上游 diff | 本项结论 |
|---|---|
| `agent/tools/code_exec.py` | 产物 URL 改为 `/api/v1/documents/artifact/{filename}`。上传时把随机文件名精确绑定到可信用户、本次运行 ID 和可选会话 ID，URL 携带 `run_id` 与可选 `session_id`。绑定失败则尝试移除对象且不输出不可下载链接。 |
| `api/apps/document_app.py` 删除旧入口 | 本仓有旧调用方，保留 `/v1/document/artifact/{filename}` 并标为 deprecated；旧新路径共用鉴权、绑定校验、文件名白名单、原始字节和安全响应头。 |
| `api/apps/restful_apis/document_api.py` 新增入口 | 新增 `/api/v1/documents/artifact/{filename}`，由异步请求会话和当前用户身份校验；实际下载在工作线程，HTML/SVG 强制 attachment。 |
| `web/src/components/next-markdown-content/index.tsx` 更新链接识别 | 前端属于独立 `../web` 仓；其当前 `6894adf` 已在 `src/lib/agent/artifact-url.ts` 和 `src/components/chat/MarkdownArtifact.tsx` 识别新旧路径并保留查询参数。本项未编辑前端仓。 |

后续链核对：`212429bf9` 增加上游基于助手消息文本的归属检查，但文本可被提示诱导
复述已知的其他用户 URL，不能作为文件所有权凭据。`93f6d647d` 为流式预览加
`session_id` 兜底；仅验证该会话可访问仍不能证明指定文件属于该会话。本仓采用上传时
登记的精确文件、用户、运行和会话绑定，下载不读取助手消息文本。Agent SSE 在
`canvas.run` 完毕后才持久化助手消息；精确绑定使流式上传后、落库前即可下载。
没有持久化会话的调试运行按用户、文件和运行绑定授权。历史上未登记的裸链接无法
安全证明归属，不能继续下载。登记按沙箱产物保留期到期；对象沿用现有生命周期配置，
配置失败时可能滞留，但没有登记仍不可下载。

前端对接：保留 `?run_id=<运行 ID>`，会话运行还保留 `&session_id=<会话 ID>`；
不要只凭 `session_id` 构造其他文件 URL。新旧地址都用同一凭据拉取原始二进制，
401 和非零业务码均当失败处理。独立 web 仓的 `artifact-url.ts`、
`MarkdownArtifact.tsx`、`markdown-artifact.test.ts` 和单步调试附件列表是对应联调点；
当前前端单测只覆盖 `session_id` 查询参数，完整 `run_id` 链接尚待前端仓补契约测试与联调。
另需修正前端 `fetchArtifactBlob` 对合法 `.json`、`.html` 附件的处理：它调用的通用
`assertPreviewResponse` 将 `application/json`、`text/html` 一律视为错误，当前这两类
附件即使后端返回正确原始字节也无法在页面下载。合法附件带 `Content-Disposition`
文件名，业务错误 JSON 没有；应结合已识别的产物 URL 与响应头区分业务错误、登录页
和合法附件，再做对应回归；本项未编辑独立前端仓。

验证：定向单元与 scratch PostgreSQL/Redis 测试 15 passed，覆盖流式落库前放行、他人和
错运行拒绝、助手复述他人 URL 拒绝、会话删除后的拒绝。一次性 API + scratch PostgreSQL +
真实 MinIO 运行 CodeExec 上传方法，并验收新旧路由有效令牌原始字节、无令牌和他人令牌、
错误路径、伪造助手引用、删除对象后的读回；临时对象、登记、数据库和进程均清理。
`make verify` 通过（8 条 import contracts、mypy 124 个源文件、unit 3177 passed），
`make integration` 通过（241 passed），`make smoke` 通过。真实模型驱动的完整 Agent SSE
未运行；落库前时序由独立会话行、CodeExec 上传和真实下载组合验收。

## 2846a939981b41e155ef9975727bfb0e7f7a0ca8 · 修正大型 PDF 页数截断

- 上游：`infiniflow/ragflow` #14382，提交于 2026-04-27；按目标 SHA 核对完整 24 文件 diff。
  2026-09-27 fetch 后 `origin/main` 为 `313ca90f6abd7682fe8523e16fd67b3653a3fa84`。
  此项只移植 Python 解析与任务链；下一指定提交 `290f0294` 留待单独处理。

| 上游 diff | 本项结论 |
|---|---|
| `common/constants.py`；`deepdoc/parser/{pdf,docling,mineru,opendataloader,paddleocr,docx}_parser.py` | 增加解析页上界 `100000` 与独立的任务标记 `100000000`。DeepDOC、Vision 默认不再在第 299 页截止，Docling、MinerU、OpenDataLoader 不再默认在第 600 页截止；PaddleOCR、DOCX 同步统一默认值。DeepDOC 的 `parse_into_bboxes` 接收并传递明确页范围，文字提取失败时只为实际渲染的页分配空列表。纳入后续 `3a829fb6d` 的 Vision 指定页范围原页码修正。 |
| `rag/app/{book,email,laws,manual,naive,one,paper,presentation,qa,resume,table}.py` | 对应本仓 `core/app/` 同名模块，统一“全部页”默认值与 DOCX/PPT/Excel 调用处的哨兵；保留现有解析模式选择和显式页范围。 |
| `api/db/db_models.py`、`api/db/services/{document,file,task}_service.py` | 统一任务表默认标记、排队分页区间、非分页任务和复用任务判断；本仓文档/文件直接解析入口沿用解析页上界。 |
| 两个上游 SDK route 测试文件 | 仅调整其整包伪造的 `common.constants` 测试桩；本仓没有同类测试桩，不复制。新增本地真实 PDF、任务分片和 scratch DB 回归。 |

本仓还有 `document_analysis_service.py`、`pipeline_analysis_service.py`、
`guard_detection_app.py` 和 `core/flow/parser/parser.py` 的直接解析入口，已同步页上界；
`core/svr/task_executor.py` 的非分页任务进度标记也同步使用任务常量。
`core/flow/parser/parser.py` 的 DeepDOC bbox 路径未配置指定页范围，当前使用新默认值。

后续链核对：`3a829fb6d` 修正 Vision 指定范围的原始页码，本项已纳入；
`c446c403d` 再将 bbox 解析分批并延迟加载页面图像，是单独的内存优化，当前 302 页
轻量 PDF 验收不代表高分辨率完整 OCR 对超长 PDF 的内存压力已解决。
`81361c210` 后续修正 MinerU API 对页范围的转发；本仓现有 MinerU API 模式
仍将页码固定为全量，Docling/OpenDataLoader 调用链也尚未转发显式范围，
这些后端的范围控制不能用本次 DeepDOC/Plain/Vision 验收结果推断。

验证：生成 302 页轻量 PDF，实际经 `pdfplumber` 渲染、提取并读回末页 `PAGE302`；
另验证第 301–302 页指定范围、短 PDF、提取失败时的回退列表长度、Vision 原页码、
任务切分覆盖末页及 scratch PostgreSQL 中的非分页任务标记。
`make verify` 通过（8 条 import contracts、mypy 124 个源文件、unit 3163 passed），
`make integration` 通过（240 passed）。未运行完整 DeepDOC OCR/版面模型及真实外部解析服务。

## c3eac4103a0408f9b8d25948e625e58821b5d54a · 阿里云 Go 模型提供商

- 上游：`infiniflow/ragflow` #14379，提交于 2026-04-27；父提交为上项 `0b46ab07`。
  核对了目标的 9 文件完整 diff。2026-09-27 fetch 后 `origin/main` 为
  `313ca90f6abd7682fe8523e16fd67b3653a3fa84`。
- 本项已移植到 Go 并行实现。本仓 Go 服务从 `configs/models/` 加载提供商；
  Python 从独立的 `configs/llm_factories.json` 加载模型，已有 `Tongyi-Qianwen`
  和 DashScope 调用实现，本项没有改 Python 代码或共享配置。

| 上游 diff | 本项结论 |
|---|---|
| `conf/models/aliyun.json` | 映射为 `configs/models/aliyun.json`，保留初版 `Aliyun` 名称、`qwen-flash` 和三个地域；模型列表 URL 采用后续 `a75e733b3` 修正的 OpenAI 兼容路径。初版 `series: "deepseek"` 与模型不符，不带入。Embedding/Rerank 的模型目录和能力属于后续提交。 |
| `internal/entity/model.go` | 将模型/提供商的 `Series` 改为后续 `f670913bb` 确立的 `Class`，同时迁移本仓六份使用 `series` 的 Go 提供商 JSON，保持原有类别值。模型名无 `-` 时保留本仓安全推断；提供商显式 `class` 值直接传给模型，不照搬初版取提供商名称的错误。 |
| `internal/entity/models/common.go`、`internal/entity/models/types.go` | 思考解析参数与 `ChatConfig` 同步使用 `ModelClass`；原有空指针处理和 `qwen3` 解析语义保留。 |
| `internal/entity/models/factory.go`、`internal/service/model_service.go` | 工厂注册 `Aliyun` 驱动；聊天服务继续从模型目录把类别传到驱动配置。 |
| `internal/entity/models/gitee.go`、`internal/entity/models/siliconflow.go` | 思考解析调用同步传 `ModelClass`，不改变两家原有请求协议。 |
| 新增 `internal/entity/models/aliyun.go` | 实现单消息和多角色同步聊天、sender 流式聊天、模型列表及连接检查。修正初版 `Name()` 误报 `siliconflow`、流式默认 `false`、未知地域产生空 URL、默认 scanner 丢弃大事件、异常结束仍发送 `[DONE]` 等问题；对 HTTP/业务错误显式返回错误。Embedding、余额和旧 channel 流式接口明确返回不支持，不伪装成功。 |

后续链核对：`effc84a04` 重构 Go 模型服务，`f670913bb` 将初版 `Type` 改为
`Class`；`a82ae4a99`、`2ad854c58` 后补 Embedding 和 Rerank，`827cceccb`
修正驱动名称及未知地域 URL，`04aa8d04e` 扩大 SSE 缓冲区，`a75e733b3`
修正模型列表路径。当前上游主线又迁移到共享 `BaseModel` 和 `Tongyi-Qianwen`
提供商名；这些架构及名称变动不在本次提交范围。

验证：用本机已有的 `golang:1.25` Docker 镜像运行 `gofmt`，
`go test ./internal/entity/models ./internal/entity`、`go build ./internal/...`、
`go vet ./internal/...` 均通过。mock HTTP 测试覆盖提供商注册、类别推断、
同步/多角色消息、地域、128 KiB 流式事件、`[DONE]` 和异常响应。
额外的 `go test ./internal/service` 因缺少 `internal/cpp/cmake-build-release/`
下的 C++ tokenizer 静态库而在链接阶段失败；本项没有修改该 native 构建链。
未配置真实 DashScope 凭据，因此没有对外部服务发 live 请求；Python 文件未改，
不运行 Python 门禁；已检查本次 diff、路径和链接。
下一指定提交 `2846a939` 不在本项范围。

## 0b46ab07c59eb715cbb4c1623724a11bda57b398 · 恢复 OpenAI 兼容聊天补全

- 上游：`infiniflow/ragflow` #14380，提交于 2026-04-27；核对目标的 10 文件完整 diff。
  2026-09-27 fetch 后 `origin/main` 为 `313ca90f6abd7682fe8523e16fd67b3653a3fa84`。

| 上游 diff | 本项结论 |
|---|---|
| 新增 `api/apps/restful_apis/openai_api.py`，从 SDK session 迁移 `/chats_openai/{id}/chat/completions` | 新建同名 FastAPI 路由模块，提供 `/api/v1/openai/{id}/chat/completions`；旧路径由同一 handler 保留并标为 deprecated。使用已有 API Key 异步鉴权和请求级 `AsyncSession`，按租户及有效状态读取聊天助手。`/chats/{id}/completions` 是本仓仍有调用方的原有契约，本项保留；`/chat/completions` 是另一种统一会话 API，不复用为 OpenAI 协议入口。 |
| `model` 占位符及指定模型 | `"model"` 使用助手配置；具体模型从当前租户的聊天模型记录验证并选择其 `tenant_llm_id`。对本次请求使用深拷贝的 Dialog，避免 ORM 自动 flush 将覆盖模型写回助手。响应中的 `model` 是实际使用的模型名。 |
| `messages`、同步 JSON、SSE、引用及元数据 | 校验消息角色、最后一条用户消息与文本内容；文本数组拼接，非文本内容显式报参数错误。新路径省略 `stream` 时默认非流式，旧别名保持默认流式。SSE 只发增量正文，完整最终正文放扩展字段 `final_content`，末帧携带 usage 和可选引用后发 `[DONE]`；错误帧不伪装为成功结束。引用元数据经当前异步会话的 `run_sync` 查询，可筛选字段；`metadata_condition` 无匹配时传 `-999` 防止退化成无过滤检索。Python OpenAI 客户端会将 `extra_body` 合并到 JSON 顶层，因此同时支持顶层和历史嵌套格式。 |
| HTTP/Python 参考与 benchmark 路径 | 更新本仓 HTTP 参考、OpenAPI 筛选示例与稳定路径映射；本仓没有上游 `python_api_reference.md` 和 benchmark 脚本，不复制文件。实际用本机 OpenAI SDK 的 MockTransport 核对 URL 拼接：`base_url` 须止于 `/openai/{chat_id}`，上游文档末尾再加 `/chat` 会得到重复的 `/chat/chat/completions`。 |
| HTTP 测试 helper 的 related questions 路径、音频单测及旧 session 测试删除 | 本仓已有 `/searchbots/related_questions`，旧 `/sessions/related_questions` 保留兼容；音频路由在本仓独立实现且本次生产 diff 未改动，不复制上游 Quart 测试。使用本仓 FastAPI 测试覆盖新旧路由、鉴权、模型、同步与流式响应、引用、元数据及错误语义。 |

后续链核对：`bd6251f46` 将新路径默认响应改为非流式，`09d0a1745` 处理数组消息内容，
`5b02fe484` 消除流式最终答案重复；本项纳入这三个必要修复。
`e6dd39753` 的 session ID 改动由 `bb148edf4` 回退，未纳入；
`a75ea7ba7` 的生成参数覆盖、`3bfad1f00`/`6a77523bf` 的后续模型映射及
`24af0875e` 的引用元数据展示配置属于后续独立行为，未提前移植。
本地适配额外修复上游引用元数据 helper 缺少数据库参数、流式异常仍发 `stop` 的假成功，
以及 SDK `extra_body` 实际在顶层的请求形状。下一指定提交 `c3eac410` 不在本项范围。

验证：定向路由单测 23 passed；`make verify` 通过（8 条 import contracts、mypy 124 个源文件、unit 3156 passed），
`make integration` 通过（239 passed）。隔离 PostgreSQL scratch 库启动真实 HTTP API，
`make smoke` 通过（ping/healthz 全部组件 `ok`）；新路由非流式、SSE `[DONE]`、旧别名默认流式、
缺失 API Key 401、未知模型业务码 101、指定模型响应均通过实际请求，独立查询确认 Dialog 模型未改变。
回答生成在隔离进程中用固定模型桩替代外部 LLM；真实模型推理与真实检索引用元数据没有端到端运行，
对应路由行为由定向测试覆盖。临时数据库和进程已清理。

## 4dcc42e0e14ad4a93373f08b325757cba285ac54 · 统一数据集与索引 API

- 上游：`infiniflow/ragflow` #14222，提交于 2026-04-27；核对了完整 51 文件 diff。
  2026-09-27 fetch 后的 `origin/main` 为 `313ca90f6abd7682fe8523e16fd67b3653a3fa84`。
- MultiRAG 主体已有提交：`a2f10eed`、`def8625c`、`b17e9a8b`。本次补齐图谱读取新路径，
  并标明旧路径的兼容状态；下一上游提交 `a9e5724b` 不在本项范围。

| 上游 diff | 本项结论 |
|---|---|
| `api/apps/restful_apis/dataset_api.py`、`api/apps/services/dataset_api_service.py` 的数据集详情、摄取概览/日志、标签聚合/修改、元数据配置及统一 `graph/raptor/mindmap` 索引 | 主体由上述三次 MultiRAG 提交语义移植；保留 FastAPI、AsyncSession、显式删除路径和旧接口兼容层。本次发现并补齐 `GET /datasets/{id}/graph/search`，复用现有图谱 service。 |
| `api/apps/restful_apis/document_api.py` 的 `POST /metadata/update`、文档 parse/stop | 已由本地 `document_api.py` 与 `document_api_service.py` 实现；混合有效/无效 ID 的 parse 响应保留非零业务码及部分执行结果。 |
| `api/db/services/doc_metadata_service.py` 的 ES 元数据整字段替换 | 已由 `metadata_store_engine.py` 等价实现：用脚本替换 `meta_fields`，避免对象深合并留下被删除的 key。 |
| `api/apps/kb_app.py` 删去旧路由与相应旧测试 | 当次将删除范围内仍存在的旧路由标为 deprecated，新行为在 `/api/v1/datasets` 实现。其中后续 `d88f7ac8` 涉及的 10 个操作现已删除，见该项最新记录；其余本地入口按实际调用逐项退役。 |
| `POST /datasets/{id}/embedding` | 暂不采纳：上游后续 `70a49c947` (#16936) 明确删除这个未使用的端点和 service。现有 `/v1/kb/check_embedding` 是抽样校验，不是该端点的等价替代。 |
| `sdk/python/ragflow_sdk/modules/dataset.py` 将自动元数据配置改用 `/metadata/config` | 本仓无该客户端 SDK 源码；服务端新路径已存在，旧 `/auto_metadata` 仍为 deprecated 兼容入口。客户端方法迁移留给 SDK 所在任务。`api/apps/sdk/dataset.py` 是服务端空 router，并非该客户端模块。 |
| `web/src/...` 九个文件的调用路径调整 | 前端是独立 `../web` 仓，按移植 Skill 另排；本次只保证新增的图谱读取路径在服务端可用，未改前端。 |
| `sdk/python/test`、`test/playwright`、`test/testcases` 的新增、改写与删除 | 不复制上游 Quart/Peewee 测试 harness；本仓现有 dataset/index、文档解析、元数据及存储回归测试覆盖已移植行为。本次扩充图谱路由契约测试和异步依赖树检查。 |

后续链核对：`35f6d81b7` (#14402) 后续扩展图谱检索 REST 行为，不能作为本项的额外实现范围；
`70a49c947` (#16936) 删除本提交新增的 embedding 入口，因此该入口不移植。

验证：`make verify` 全绿（8 条 import contracts、mypy 124 个源文件、unit 3100 passed）；
图谱新旧路径的成功/鉴权失败响应及 OpenAPI deprecated 状态由 unit 覆盖。
`make smoke` 未通过：本机没有运行中的 API，Redis 与 MinIO 不可达；本次未启动会初始化配置数据库的服务入口。

## a9e5724b46e9f006b90ddd70f812fb59840c6806 · 统一文档创建入口

- 上游：`infiniflow/ragflow` #14345，提交于 2026-04-27；目标父提交是上项 `4dcc42e0`。
  2026-09-27 fetch 后 `origin/main` 为 `313ca90f6abd7682fe8523e16fd67b3653a3fa84`；
  本项按目标提交的 10 文件完整 diff 评估。

| 上游 diff | 本项结论 |
|---|---|
| `api/apps/restful_apis/document_api.py` 新增 `type=local|web|empty` | 移植到现有 FastAPI `POST /api/v1/datasets/{id}/documents`。`local` 保留已有 `file`/`files` 批量上传与返回数组契约；`web` 接收表单名称和 URL，校验租户与 URL 后转换 PDF，复用现有 `FileService.upload_document`；`empty` 接收 JSON 名称，创建虚拟文档与文件关联。网页的 Selenium 与同步存储链在自有会话的工作线程执行；空白文档经请求会话桥接遗留同步 service。 |
| `api/apps/document_app.py` 删除旧 `/web_crawl`、`/create` | 本地仍有旧调用方，两个路由保留可用并标记 deprecated，待消费方迁移后退役。 |
| `docs/references/http_api_reference.md` 的三种请求示例 | 更新本仓同名参考文档和 REST 模块 README；按本地契约说明 `file`/`files`、返回形状和不自动解析。 |
| `test/testcases/test_web_api` 四个文件的 helper/验收改写 | 不复制上游 Quart/Peewee harness；本仓增加 FastAPI 路由、鉴权、URL 阻断、空白文档与文件关联等单元回归，保留已有上传回归。 |
| `web/src` 三个文件的调用切换 | 本地前端在独立 `../web` 仓，本项不改该仓；服务端新路径已就绪，旧路径供其迁移期间使用。 |

后续链核对：`a339e8a57` 处理批量文件部分成功，属于后续上传契约变更；
`6e0e49592` 修复阻塞式上传/网页处理，这里已经按 MultiRAG 异步边界将网页链放入线程，
两条后续提交均不作为本项额外范围。现有 `is_valid_url` 检查输入 URL 的出网地址；
浏览器抓取过程仍依赖既有 Selenium 实现。

验证：`make verify` 通过（8 条 import contracts、mypy 124 个源文件、unit 3115 passed）；
新增创建模式与既有上传路由定向测试通过，另增 OpenAPI 兼容标记检查。
`make integration` 因 Redis `127.0.0.1:6379`、MinIO `127.0.0.1:9020` 不可达而未运行测试；
`make smoke` 因 API `127.0.0.1:8123` 未启动而失败。本项未启动会初始化配置数据库的服务入口，
因此当时真实存储写入与网页抓取端到端验收尚未完成。

补验收（2026-09-27，基础服务启动后）：`make integration` 通过 235 个测试。
API 使用仅供本次验收的新建 PostgreSQL scratch 库启动，`make smoke` 通过；健康检查的
数据库、Redis、文档引擎和存储状态均为 `ok`。通过真实 REST 请求创建了 `empty` 虚拟文档、
`local` 文本文档和 `web` PDF 文档，逐个通过列表接口读回，并独立查验 scratch 库中的
文档行和文件关联。删除文档、读回空列表、删除数据集后，API 停止且 scratch 库已删除；
未对配置的 `xldu` 库运行启动迁移。公网域名在本机被 DNS 映射到保留地址而被 SSRF 校验拒绝，
网页模式改用通过校验的 `https://1.1.1.1/cdn-cgi/trace` 完成验收。

补验收发现 webdriver-manager 在本机将可执行路径指向 `THIRD_PARTY_NOTICES.chromedriver`，
导致网页创建返回业务错误。已在共享 `html2pdf` helper 中选择同目录真实 `chromedriver`，
并补齐其执行权限；增加对此缓存布局的回归测试。修复后网页创建成功并完成上述读回与清理。
修复后重跑 `make verify`（unit 3116 passed）与 `make integration`（235 passed），均通过。

## 3ad3241ae06f414d2ccd2c92fda8c576bb96a96a · 保存 RAPTOR 摘要层级

- 上游：`infiniflow/ragflow` #13286，提交于 2026-04-27；父提交为 `a9e5724b`。
  核对了目标提交的 3 文件完整 diff；2026-09-27 fetch 后的 `origin/main` 为
  `313ca90f6abd7682fe8523e16fd67b3653a3fa84`。

| 上游 diff | 本项结论 |
|---|---|
| `rag/raptor.py` 返回 `(chunks, layers)`，少于两个输入返回 `([], [])` | 对齐到 `core/raptor.py`，保留原有聚类和摘要过程；`layers[0]` 是原始节点，后续边界对应逐级摘要。 |
| `rag/svr/task_executor.py` 为索引中的摘要写入 `raptor_layer_int` | 对齐到 `run_raptor_for_kb`：按摘要节点在完整结果中的下标映射层级，第一层为 1，第二层为 2；保留本地 `pk` 和文档 ID 规则。另有四处本地分析调用点消费原来的纯列表返回值，均解包新返回值并继续使用原摘要列表。 |
| `conf/infinity_mapping.json` 新增整数列，ES 由 `*_int` 动态模板处理 | 更新 `configs/infinity_mapping.json`；本地 Milvus 是默认文档后端，其显式 schema 另在 `configs/mapping.json` 增加 INT64 字段。ES/OpenSearch 已有 `*_int` 动态模板。 |

后续链核对：`bf4864e61` 为后续新增的 RAPTOR `extra` 字段修补 Infinity 写入，
本项摘要未写 `extra`；`2717ee283` 引入新的 Psi RAPTOR 构树流程，
`62f94cd59` 将实现拆到 `raptor_service.py`，`0c2fb622e` 调整小层聚类。
这些是后续独立行为；当前上游主线仍在任务执行和重构后的 RAPTOR service 写入
`raptor_layer_int`，未回退本项语义。

验证：新增单测覆盖 RAPTOR 返回边界和 1、1、2 多层摘要索引字段；
`make verify` 通过（8 条 import contracts、mypy 124 个源文件、unit 3118 passed），
`make integration` 通过（235 passed）。隔离 Milvus 集合实测新 schema 为 INT64，
摘要层 2 和未带字段的普通切片默认值 0 均可在 flush 后读回；用旧 schema
模拟的动态字段集合也读回层 2，两个临时集合均已删除。
Infinity 未配置运行实例，映射和既有列迁移逻辑仅经代码核对，未做真实后端写入验收。
VastBase 的现有映射连 `raptor_kwd` 也未定义，本项不扩大到修复该后端原有的
RAPTOR 写入契约；该后端的 RAPTOR 层级落库仍待单独处理。

## 33bb464ce3f5598bf3107a8598d86fef9a4011d7 · 聊天共享页误发 Agent 请求（仅审查）

- 上游：`infiniflow/ragflow` #14190，提交于 2026-04-27；父提交为上项 `3ad3241a`。
  核对了 5 文件完整 diff。2026-09-27 fetch 后 `origin/main` 为
  `313ca90f6abd7682fe8523e16fd67b3653a3fa84`。
- 独立前端 checkout `../web` 的 HEAD 为 `75db97866bc496294e5db55fe829fd47006750ef`，
  本次只读审查，未跨仓编辑。其当前路由有 `/agent/share`、`/chats/widget`，
  没有上游的 `/chats/share` 页面。

| 上游 diff | 本项结论 |
|---|---|
| `web/src/hooks/use-agent-request.ts` | 实际 diff 将 `useFetchSharedAgent` 改名为 `useFetchFlowSSE`，并给查询加 `enabled: !!sharedId`；并未新增可由调用方传入的 `enabled` 参数。查询函数仍调用 `agentService.getAgent(sharedId)`，对应 `/api/v1/agents/{id}`，且目标提交中该 hook 已无调用方。本地 `../web/src/hooks/use-agent-query.ts` 另有按 Agent ID 启用的同名 hook，不应照搬上游的改名。 |
| `web/src/pages/next-chats/share/index.tsx` | 移除共享聊天页的 Agent 查询、`from` 分支和 Agent 头像读取，统一使用聊天信息的 `avatar`。根因是把聊天 `dialog_id` 当 Agent/Canvas ID 请求，触发权限错误；本地没有此页面，当前无需移植。若以后实现聊天共享页，应从聊天信息取头像，且不得用聊天 ID 调 Agent 查询。 |
| `web/src/locales/zh.ts` | `rootAsHeadingTip` 仅换行排版，无文案变化；不移植。 |
| `web/src/pages/user-setting/data-source/data-source-detail-page/index.tsx` | `useAddDataSource` 调用只调整空格，无行为变化；不移植。 |
| `web/src/pages/user-setting/data-source/hooks.ts` | import 与函数参数仅调整排版，无行为变化；不移植。 |

独立 web 仓待办：若新增 `/chats/share?shared_id=...&from=chat`，先确认后端的聊天共享信息及
公开访问契约，再实现独立聊天数据查询；不要复用 Agent/Canvas 详情查询。
增加覆盖请求边界的回归：聊天共享页实际打开后网络请求中没有
`/api/v1/agents/{dialog_id}`（也没有 Canvas 详情请求），聊天标题、头像与消息正常显示；
同时验证 `/agent/share` 的 Agent 信息与头像仍正常、缺失 ID 时不发详情请求。
改动发生在 `../web` 时运行其 `npm run lint`、`npm run build`、`npm run test:unit`，
再用浏览器核对请求与业务码，不能只凭 HTTP 200 判断。当前本地 Agent 共享页使用
`useFetchExternalAgentInputs`，并不调用同名 `useFetchFlowSSE`。

MultiRAG 后端无需移植：目标提交没有后端 diff；其修复是去掉错误的前端请求，
不能通过放宽 `/api/v1/agents/{canvas_id}` 的 Canvas 权限校验来掩盖问题。
上游后续链中未发现对此删除行为的 revert 或直接修补；当前 `origin/main` 的聊天共享页
仍不调用 Agent 查询。后续 `5a2cd36b4` 修改共享页语言同步，属独立问题。

验证仅针对审查文档：核对目标 diff、上游当前状态、本地路由和调用链；
检查本次文档 diff、路径与链接。不改可执行代码，因此本项不运行 Python 或 web 门禁。

## f3b7d55a1e4f2fa2748979caaef42f93651d41c8 · Infinity 更新遇到缺表

- 上游：`infiniflow/ragflow` #14153，提交于 2026-04-27；核对目标的 3 文件完整 diff。
  2026-09-27 fetch 后 `origin/main` 为 `313ca90f6abd7682fe8523e16fd67b3653a3fa84`。

| 上游 diff | 本项结论 |
|---|---|
| `rag/utils/infinity_conn.py` 的 `update` | 移植到 `core/utils/infinity_conn.py`：Infinity 抛出精确的 `TABLE_NOT_EXIST`（3022）时记录缺表并返回 `False`，其余错误原样抛出；连接始终释放。本地实现同时覆盖查表与更新期间删表。 |
| `memory/utils/infinity_conn.py` 的 `update` | 本仓确有活跃的 memory Infinity 存储路径，按相同规则移植，并覆盖查表、更新与连接释放。 |
| `api/apps/document_app.py` 的 `change_status` | 移除按异常文字包含 `3022` 分类的逻辑；存储返回 `False` 或抛异常均返回非零业务码及每文档错误，不把状态更新报告为成功。 |

本地还有 `api/apps/services/document_api_service.py:update_document_status_only` 供 REST 文档状态路径调用；
原逻辑未检查存储 `update` 的布尔结果。本项使已有切片的文档在 `False` 时返回业务错误，
未解析、无切片的文档沿用旧路由规则跳过存储更新。其他 Infinity 错误仍进入原有服务端错误响应。

后续链核对：上游 `a536980e2` 将旧 `change_status` 路由迁移到 REST 批量状态接口，
不是本次缺表处理的回退；当前 `origin/main` 的文档与记忆 Infinity `update` 仍保留
`TABLE_NOT_EXIST` 判断。下一指定提交 `0d87ceca` 不在本项范围。

验证：新增单测覆盖两种 Infinity 存储的查表和写入阶段 3022、其他错误、正常更新与连接释放，
以及旧路由和 REST 路由的失败业务码、未解析文档跳过存储更新。`make verify` 通过
（8 条 import contracts、mypy 124 个源文件、unit 3132 passed）。
`make integration` 通过（235 passed）。
本机无 Infinity 容器，`127.0.0.1:23817` 也不可连接，本项未做真实 Infinity 写入验收。

## 0d87cecae2e47b3f9b46836d2c3d06b97f082f4d · 持久化 PDF 原始书签

- 上游：`infiniflow/ragflow` #13287，提交于 2026-04-27；核对目标的 3 文件完整 diff。
  2026-09-27 fetch 后 `origin/main` 为 `313ca90f6abd7682fe8523e16fd67b3653a3fa84`。

| 上游 diff | 本项结论 |
|---|---|
| `rag/app/naive.py` | 对齐到 `core/app/naive.py`：PDF 解析器已有书签时，在首个切片附临时 `__outline__`。按后续 `907587243` 修复，从实际的 `(title, depth, page)` 三元组取前两项，避免原提交的解包异常。 |
| `rag/app/manual.py` | 对齐到 `core/app/manual.py`：复用本地 manual 解析时已经提取的书签列表，不依赖部分解析器没有提供的 `pdf_parser.outlines` 属性；无书签不附临时字段。 |
| `rag/svr/task_executor.py` | 对齐到 `core/svr/task_executor.py`：移除切片中的 `__outline__`，把 `[{title, depth}]` 写入 `DocMetadataService` 的文档元数据，并按布尔返回值区分保存成功与失败。 |

本地 `FACTORY` 还含 book、paper、laws、one、presentation 等 PDF 解析路径；
当解析器未传 `__outline__` 时，任务执行器从原始 PDF 在工作线程提取书签。
本地 `run_dataflow` 绕过 `build_chunks`，也在索引前从关联的 PDF 文件提取并保存书签；
`analyze_v2` 使用临时文档 ID、没有 Document 记录，不属于此持久化路径。
书签写入放在自动元数据生成之后，避免通用 `update_metadata_to` 将已有的字典列表过滤掉；
写入时保留其他已有元数据，并以新书签替换旧 `outline`。无书签不写入。
保存失败时记录警告或异常，沿用上游的解析继续语义，不记录虚假的持久化成功。
本地 Milvus 配置下，`DocMetadataService` 将 `meta_fields` 存在独立的
`t_ai_document_metadata` 表，而不是直接改 `Document.meta_fields` 列。

后续链核对：`907587243` 修正上游的书签三元组解包崩溃，本项已包含；
`f0cb7a544` 将上游任务执行器拆层并记录元数据写入返回值，未回退书签行为。
当前上游主线仍有 naive/manual 的临时书签传递与任务执行器写入；下一指定提交
`6a23dfee` 不在本项范围。

验证：单测覆盖 naive 真 PDF 有/无书签、manual 三元组、其他 PDF 解析模式的回退、
临时字段从全部切片清理、元数据合并与保存返回 `False` 或异常时不记录成功。
隔离 PostgreSQL scratch 库用生成的 PDF 运行标准解析，再通过
`DocMetadataService` 独立读回 `outline` 和原有字段；另直接验收 dataflow 补充路径
的取件、保存与读回。带书签和无书签各覆盖两条路径，随后删除临时文档、数据集与元数据；
`make verify` 通过（8 条 import contracts、mypy 124 个源文件、unit 3141 passed），
`make integration` 通过（239 passed）。未启动完整 Canvas dataflow 管道；其书签
取件与元数据写入部分由上述隔离测试验证。

## 6a23dfeec1632d25736fcbff33cc9fb2a53d4e1c · 共享 UI 组件目录约定（仅审查）

- 上游：`infiniflow/ragflow` #14381，提交于 2026-04-27；完整 diff 只有
  `web/CLAUDE.md` 新增 8 行。2026-09-27 fetch 后 `origin/main` 为
  `313ca90f6abd7682fe8523e16fd67b3653a3fa84`。
- 这是一条前端协作约定，不改运行代码、API 或协议；MultiRAG Python 后端没有可移植项。
  本仓 `AGENTS.md` 是后端协作规则唯一入口，`CLAUDE.md` 只映射它，
  不把独立前端的目录保护规则复制进后端指令。

| 上游 diff | 独立 `../web` 仓的评估交接 |
|---|---|
| `web/CLAUDE.md` 的 Shared UI Component Lock | 将整个 `src/components/ui/`（含子目录）视为共享组件库；常规需求不直接修改、重构或改样式。先在目录外的 `src/components/` 或功能目录包装、组合，通过 `className`、props 等定制。确需修改现有共享组件，应先取得当前会话用户明确许可；新增共享组件或用 shadcn CLI 升级原语，仅在用户明确要求时做。 |

后续链核对：上游 `cad7c46be` 把 `web/CLAUDE.md` 原样改名为
`web/AGENTS.md`，删除根目录单行 `CLAUDE.md`；当前主线的锁定条款仍在
`web/AGENTS.md`，不是行为回退。本地 `../web` HEAD 为 `0834bb9`，
`src/components/ui/` 确有共享原语和项目组件，但本地
`AGENTS.md`、`CLAUDE.md` 尚无此锁定条款。两份本地文件明定为同一规则集的
中英文版本：若前端任务决定采纳，应在**同一次前端提交**同步中文和英文，
并核对现有“`src/components/ui/` 仅原子组件”的分层措辞，避免相互矛盾。
按本次派工，前端会话仍在补 `a9e5724b` 的实际运行验收；本项不跨仓编辑或运行前端门禁，
也不将该工作状态作为本次实测结论。
下一指定提交 `0b46ab07` 不在本项范围。

验证：核对目标完整 diff、上游后续改名和当前条款、本地两个工作树状态、
目录及双语指令；仅更新本记录并检查文档 diff、路径与链接，不运行 Python 或前端测试。

## 0cf105da8da0cd4bb7dbed55a7f3d05220c4d9b6 · 数据库 schema 与迁移指南

按本轮并行授权独立评估完整上游三文件 68 增 1 删。栏目 JSON 和 backup 页面移动/slug
不适用于本仓 Markdown 导航；新增 [数据库迁移指南](../database-migration.md)，按当前
PostgreSQL/SQLAlchemy/Alembic 的空库建表后 stamp、存量库先迁移再补表、容器先行建表
及默认数据初始化行为适配。不复制 MySQL/Peewee 工具、初始化开关或版本数据迁移保证。

指南明确应用 DB engine 与 CLI `ALEMBIC_DATABASE_URL` 的边界、URL 的子进程导出、六卷
备份覆盖、SeekDB bind mount、空备份目录与旧包混用风险、恢复叠加解压及 downgrade
不恢复原模型 ID。升级、跨存储恢复、业务对账和回退各自需要实际证明。

本项仅文档及入库白名单、导航、映射整合。原交付 turn 的只读 Alembic heads/history/命令
help 与脚本语法检查均 exit=0，单 head `e1f3a5c7b9d0`；完整上游 diff、16 个本地链接及
锚点已根核，独立只读审查无阻断，四项操作说明微调后复核文档。未连接数据库或生产，
未运行升级、备份或恢复；纯文档范围无需 Python/Web 门禁。这是本项文档结论，固定批次
其他独立实现及相关 Web、旧入口退出仍需分别审结，不因队尾文档先完成而结束整批。

## f670913bb43585f7428b156bf4d6b52d026263f6 · Go 模型系列与能力类型

上游 remote 已核为 `infiniflow/ragflow`，按冻结
`519e7d98a5651564d4e35d6648f006cba4baaf4f` 核对目标 14 文件完整 diff，未 fetch。
本项复用当前 Go 驱动与租户模型绑定，并与 Extra owner 确认无文件重叠；未扩展 Python 架构。

- 已等价：`Model.Class`、`Provider.Class`、`ChatConfig.ModelClass` 与服务默认赋值、
  Gitee/SiliconFlow 推理拆分调用已存在；八份指定系列配置中的七份已为 `class`。
  Aliyun 已无错误的 `series: deepseek`。保留本地显式 provider class 值和无连字符安全推导，
  不带入上游初版取 provider 名称或负索引截取的错误。
- 上游缺失迁移：Google 剩余的 `series: gemini` 改为 `class: gemini`；所有 Go provider
  配置现在一致。`ModelType`、`ModelTypes`、`ModelTypeMap`、租户 `model_type` 和旧
  `mdl_type` 继续表达 chat/embedding/rerank 等能力，类型筛选与能力校验不改名。
- 本地适配：尊重模型显式 class、其次 provider 默认、最后模型名推导；统一小写、
  去掉命名空间后取连字符前缀，保持原始模型 ID 不变。Gitee/SiliconFlow 的端点选择和
  qwen3 推理拆分使用同一系列；缺少 async suffix 时保持配置的 chat 端点。
  这补齐当前消费者的系列含义，不移植后续模型 solver、tokenizer 或 provider 名称变更。
- 消费者：相关 provider HTTP 回归覆盖显式覆盖、命名空间、大小写和异步 suffix；
  ChatModel 的生成参数转换保留可信 ModelClass。当前独立 Web 的 `src/api/llm.ts`
  和 `src/stores/model.ts` 消费 Python `/v1/llm/*` 及能力类型，未发现 Go class 消费，无需前端迁移。
- 验证：Go 1.25.14，`go build ./internal/...`、`go vet ./internal/...` 及三个独立
  main 构建成功；entity/models/service/handler/CLI 的 race 检查 67 个顶层测试通过。
  通用包检查中的 5 个 opt-in 集成未启用，不计为通过；本项另外实际运行新系列消费者和
  既有 Google HTTP/SQL 两组隔离验收，均通过且零 skip。新验收从 Go handler/service 到
  受控 Gitee/SiliconFlow HTTP，检查业务 code、完整模型 ID、推理/答案分离、ChatModel
  绑定系列及错误能力拒绝，并通过独立 SQL 连接读回实例。scratch 库和私有配置已清理，
  数据库不存在已读回确认。首次新 fixture 复用 APIKey 违反现有唯一约束，改为每 provider
  独立 fixture key 后通过，未调整生产约束。native 编译器/链接器有现存 warning，退出码均为 0。
- 限制：受控 provider 与 fixture 身份验收不代表远程供应商或完整 Web/检索 E2E；
  Gitee/SiliconFlow 既有多角色聊天与 channel-only streaming 仍明确不支持，未以本项
  系列迁移扩展这些后续能力。当前 Python 实现未改变，无需 Python 全库门禁。
  其他任务的未提交改动保留，提交限定本项路径与文档 hunk。

## e6e80041 · Agent 工具结果、参数 schema 与 DeepSeek 历史

2026-10-03 对照冻结 `519e7d98a5651564d4e35d6648f006cba4baaf4f` 中的
`e6e80041f549582fd0164afcd5d52c91b3fe861f`，覆盖其三个文件的功能足迹。
未刷新来源快照；冻结链中的 `8269fa01b` 非流式 tool call 无 `index` 修复作为必要后续适配。

- 本地工具 invoke 返回 `None` 后读取一次组件 output，优先非空 content，再使用整个输出；
  明确的空值返回保留。callback 同步接收最终结果。输出读取异常交给工具错误链，
  不吞异常返回假成功；MCP binding 的 canonical name、授权、超时及日志分派保持。
- Agent user_prompt 放在字符串参数 schema 的 default，保留 type/description/required。
- 正式 `chat.py::LiteLLMBase` 与包内同步 `DeepSeekChat` 均保全每轮 reasoning；
  Base 的异步兼容链也覆盖。流式同 delta 的 reasoning/tool_calls 不丢失，
  兼容 reasoning 字段别名和 non-stream SDK 对象。其他 provider 的既有推理展示保留，
  不照搬上游非流式展示仅限 DeepSeek 的副作用。
- 本地包结构适配共用 `chat_model/tool_history.py`；同步多工具与失败结果按一条
  assistant + 各 tool ID 写入，顺序执行/异步并发及既有同步 session 桥接保持。
  无前端合同变化、Go 变更或 DB/存储路径修改。当前行为见 [模块说明](../../core/llm/README.md)。

修改前新增回归 70 failed、10 passed；首次修后同组 80 passed，相关现有回归合计
97 passed。随后补充显式结果不读取 output 属性的回归，最终 `make verify` exit0：
完整格式、Ruff、import-linter、async DB 门禁与 mypy 通过，单测 **4588 passed**。
首次 make verify 曾被另一任务临时测试文件
的格式阻断，后续复跑该阻塞已消失；未修改该文件。新增 helper 单独 mypy 亦通过。
实际 OpenAI SDK 与 LiteLLM 对本机 HTTP/SSE fixture 的 12 组合、24 请求通过，
逐次检查下一轮 wire assistant reasoning、多工具结果与答案；fixture listener 已关闭。
这是本机协议验证，未调用远程模型或真实外部 MCP，也不代表完整 Agent UI/生产端到端验收。

## dcce864d4c9fc939e4a75bfdd1d8ffec64e31a4f · Go embedding Encode 接口

按冻结 `519e7d98a5651564d4e35d6648f006cba4baaf4f` 核对目标完整 14 文件 diff，
上游 remote 已核、未 fetch。该项在 Go 模型系列迁移提交之后串行开展，并与模型绑定及
Extra owner 确认范围；Extra 的租户模型字段和 DAO 改动不纳入本项。

- 已等价并复用：Go 已只有 `internal/entity/models` 一套工厂，模型服务绑定实际模型名、
  tenant 凭据、region 和请求上下文，保留新实例与旧 APIBase 兼容路径；批量 HTTP 编码已
  按 32 条分批、按索引归位并检查响应数量、维度和非有限值。未重写这些协议实现。
- 缺失迁移：`ModelDriver` 及所有具体驱动统一为四参数 `Encode`，移除重复的
  `EncodeToEmbedding`、三参数 driver `Encode` 和 driver `EncodeQuery`；VolcEngine
  通过既有 DummyModel 嵌入继承新签名，继续明确不支持 embedding。绑定模型与
  `entity.EmbeddingModel` 只保留批量 `Encode(texts)`，传递当前为空的 `EmbeddingConfig`。
- 消费者：全仓 Go 实现、接口、factory、ModelBundle、检索 GetVector、ChunkService
  及直接测试调用均已核对；检索将 query 作为单项批次。ModelBundle 保留两个公开入口和
  向量/token/error 返回格式，EncodeQuery 复用 Encode；token 数仍是逐文本字节长度
  `len(text)/4` 估算，失败返回 0，不冒称 provider usage。兄弟 SDK checkout 是同仓
  历史 worktree，不是本接口的外部 Go 消费者；当前独立 Web 消费 Python `/v1/llm/*`，
  Agent Hub 没有 import 此 Go internal 包，无前端或跨仓接口迁移。
- 本地安全适配：绑定模型和 ModelBundle 共用批量结果校验，拒绝非空输入的空结果、
  数量不符、空向量、维度不一致及非有限值；单项查询在校验后取首个向量，补齐上游检索
  直接索引空结果的防护。智谱保留逐条请求协议，补齐其聚合结果非空/维度检查。空输入保留空批次。
- Provider 覆盖：受控 HTTP 验证 OpenAI、OpenAI-API-Compatible、DeepSeek、Moonshot、
  Gitee、SiliconFlow、ZHIPU-AI 的已有编码入口、模型名、凭据、顺序和空输入；DeepSeek/
  Moonshot 的旧兼容路径不改变模型目录中的能力声明。Aliyun、Google、MiniMax、
  VolcEngine 和 Dummy fallback（含当前 xAI）仍返回不支持错误，不扩成假成功。
- 后续链：冻结快照中 `f3c232cf4` 移除 ModelBundle、`c55e23e7e` 再次拆分 embedding
  接口，均未回退目标的重复接口清理；其后模型 wrapper、tokenizer、维度元数据及使用量
  架构不属于本项。本地按当前消费者保留 ModelBundle 和 token 估算，不跳到快照的新架构。
- 验证：Go 1.25.14，gofmt、`go build ./internal/...`、`go vet ./internal/...` 和
  三个独立 main 构建通过；models/entity/service/nlp/handler/CLI race 检查 128 个顶层
  测试通过。通用检查 5 个 opt-in skip 不计通过；另实际运行 PostgreSQL scratch +
  provider HTTP 的模型绑定、ModelBundle query、检索向量表达式及旧 APIBase 回归，
  exit0、零 skip，独立 SQL 读回与 scratch 库删除后不存在断言通过。仅本项 Go 文件覆盖
  HEAD 的固定副本也通过相关检查；初次副本遗漏被忽略的 WordNet resource 导致 NLP
  环境失败，补齐本地只读资源后 NLP 全包通过，未改测试或共享配置。
- 通用集成：以 `bf379bd5` 为基准、仅覆盖本项 23 个 Go 文件的固定副本实际运行
  `make integration`，exit0，700 passed、零失败/skip，1757.33s。首次收集因缺少被忽略
  的 DeepDoc 模型缓存触发下载，主动中断时未运行测试，不计为通过；复制本机已有
  DeepDoc/tokenizer 缓存后按相同命令完整复跑。未调整测试、门禁或共享配置。
- 限制：受控 provider 验证不代表远程账号、完整检索存储或 Web E2E。当前 Python 模型
  实现未改变，未扩展目录未声明的 embedding 能力；token 统计仍为字节长度估算。
  其他任务改动保持，提交仅包含本项 Go 路径及文档 hunk。

## 85575259ac44b926d480ae92969795989e55a757 · Google Web OAuth PKCE

2026-10-03 按完整两文件 diff 核对，继续使用冻结的 `519e7d98`，未 fetch。
Gmail / Google Drive 在[共享 OAuth 服务](../../api/apps/services/connector_oauth_service.py)
保存授权 URL 生成时的 `code_verifier`，回调重建 Flow 后显式传入 token exchange。
新 REST 与旧回调入口共用修复，保留当前用户归属校验、按来源的 state、15 分钟 TTL、
scope、offline/consent、结果领取和回调渲染合同；Box 流程保持。

必要本地适配是显式开启 verifier 自动生成：本仓锁定的 google-auth-oauthlib 1.2.3
工厂以 `None` 覆盖构造器的生成默认值，直接保存会得到空值。[该版本 Flow 实现](https://github.com/googleapis/google-auth-library-python-oauthlib/blob/v1.2.3/google_auth_oauthlib/flow.py)
与实测一致。升级前缺少 verifier 的缓存沿用原交换尝试，不生成一个不匹配的新值。
冻结范围内没有后续 Python PKCE 修补；`af4651cce` 去除凭证 print 的行为本地已等价，
后续 Go OAuth 迁移与 Python 路由移除不适用于当前生产后端。Web 现有启动/轮询接口
无需变化，CLI 的同实例本地服务授权流程不在本项范围内。

本次 `make verify` exit=0（4594 passed）。[专项单测](../../tests/unit/test_connector_google_oauth_pkce.py)
与既有 connector API 合同共 35 passed，使用真实 Flow/OAuthLib 检查编码后的 token
请求。[隔离集成](../../tests/integration/test_connector_google_oauth_pkce.py) 6 passed，
覆盖真实 HTTP、正常 JWT/scratch SQL 用户、Redis、Gmail/Drive 新旧入口、跨用户领取拒绝、
state 来源错配、TTL、顺序重放、取消/过期/token 拒绝清理及最终凭证重新加载。
本机 token HTTP 服务验证实际 POST 中 verifier 与授权 challenge 一致；scratch 服务
`make smoke` exit=0，fixture 清理断言通过。全集成门禁以历史失败优先、首错停止运行
（`PYTEST_ADDOPTS="-x --ff" make integration`），4 passed、1 error、exit=2：
`test_infinity_available_filter` 的 fixture 在 `infinity.connect` 初始化时出现 Thrift
`TSocket read 0 bytes`，尚未进入业务测试。普通顺序的并行全集成在 332 passed 后
因此停止，不计为通过；
未修改该独立任务或环境配置。日志分别为 `/tmp/multirag-855-verify.log`、
`/tmp/multirag-855-integration-focused.log` 与 `/tmp/multirag-855-integration-priority.log`。

上述验收未访问真实 Google 认证或数据 API，不能证明客户端/回调注册、consent 与 scope
审核、Workspace 策略、refresh token 发放、真实 Gmail/Drive 访问或浏览器弹窗体验。
原有缓存消费不是原子的，本项仅验证顺序重放；未扩大为完整 OAuth 安全审计，未重启或
部署共享服务。当前使用合同见[数据连接器](../../common/data_source/README.md#google-web-oauth)。

## 7c25870923988a58cbe1fc99377bbcbbbfa2b51e · Go 租户模型 Extra 持久化

2026-10-03 作为本轮 Go 第一项，按完整双文件 diff 核对冻结的
`519e7d98a5651564d4e35d6648f006cba4baaf4f`；预期上游 remote 已确认，未 fetch。
已有 Go owner 确认无重叠；后续模型系列命名工作不修改本项实体。

[TenantModel](../../internal/entity/tenant_model.go) 补齐字符串 `Extra`，映射数据库
`extra` 和 JSON `extra`，长度 1024、默认 `{}`，与目标提交该文件逐字节一致。
[TenantModelInstance](../../internal/entity/tenant_model_instance.go) 已有长度 512、默认 `{}`
的同等字段，无需修改；当前实例创建把 region 编码为 JSON 字符串，查询和模型驱动
解码 region，空字符串或 `{}` 回退到 `default`，非法 JSON 仍报错。
模型状态写入继续经现有 DAO，由 GORM 填入 `{}`；现有按 ID、名称及实例查询直接
映射 `Extra`。本项不新增模型 Extra 的 HTTP 编辑接口或业务解释逻辑。

保留 `InitDB` 对两表的 `autoMigrateSafely`、PostgreSQL pooled search_path 与 MySQL
连接适配、字符串 `ModelType` 和既有实例索引。后续 `3bc5ed282` 的索引调整及
`330033d7c` 的整型模型类型属于独立行为，本项不提前移植；冻结快照中 Extra 未回退。

[独立 DAO 回归](../../internal/dao/tenant_model_extra_test.go) 通过
`MULTIRAG_GO_TENANT_MODEL_DSN` 指向新建且为空的 `multirag_go_tenant_model_*`
PostgreSQL scratch 库，沿用 `usr_ai,public`。真实验证已有模型表加列后原行、状态和
类型保留，物理长度及默认值、原生 SQL 和 DAO 的省略字段默认、显式 JSON 字符串
经 ID/名称/列表读回和 JSON 序列化；另用原生 SQL 读回，库在结束后删除并确认不存在。

验证副本固定为已提交 `45ebc3e1` 加本项两个 Go 文件，避开其他 owner 的未提交输入。
Go 1.25.14 下 DAO/实体及现有 region 专项全部通过，无 skip；`gofmt`、
`go build ./internal/...`、`go vet ./internal/...` 和三个独立 main 构建均通过。
固定副本 `make integration` exit=0，`REQUIRE_SERVICES=1` 下 700 passed、无 skip。
初次因副本缺少已忽略的本机 DeepDOC 权重缓存，在收集阶段中断且没有运行测试；
补齐真实缓存引用后完整复跑通过，未修改业务代码或削弱断言。
未修改 Python 可执行行为，未运行 `make verify`；本项没有路由/启动入口改动，
未重复 HTTP smoke。MySQL 未做真实数据库验收，未运行生产库迁移或真实模型调用。

## 35f6d81b730ff234a3b5a0d228cf647b812fe2ff · Dataset 检索和图谱 REST 迁移

2026-10-03 按完整 11 文件 diff 核对冻结的 `519e7d98`，未 fetch。
先接收 `7a70a0fd` 的 Infinity 零值过滤与 chunk/list 调用修复，再稳定 Python REST
契约、迁移独立 Web 和 Python 管理 CLI，最后退出两个旧 chunk 入口。

`POST /api/v1/datasets/{id}/search` 保留本地联合检索、embedding 一致性、搜索模式、
分页总数、文档聚合、高亮、元数据、rerank、跨语言、关键词和 KG 行为。
本地 `dataset_ids` 扩展必须包含路径 ID，全部选中数据集先授权；保存搜索配置沿用
有效状态、owner 状态和成员访问合同，未授权配置不参与检索。普通检索继续过滤禁用 chunk；
同时修复已有 Milvus sparse 分支漏传过滤表达式的问题，回归在旧实现上明确失败。

`GET /api/v1/datasets/{id}/graph` 不带 doc_id 时复用已有聚合图服务，带 doc_id 时保留
文档子图与思维导图（包括重复节点 ID 处理），并核对文档所属数据集。
隐藏图谱产物仍可读取，子图保留 removed 条件。Web 保留原页面 hook 的结果合同，
多数据集只执行一次联合检索；Python CLI 使用 REST 基址和完整 dataset_ids。
迁移验收后移除 `/v1/chunk/retrieval_test`、`/v1/chunk/knowledge_graph` 及专用模型，
其余 chunk 管理、SDK retrieval/searchbots、graph/search 和 REST knowledge_graph
兼容入口保留；旧图谱删除仅删产物，与新索引删除语义不同，不一并退役。

相关后修：采用 `c5a932ffb` 的关键词分隔符；高亮和总数保留已满足 `66113709c`、
`fd7cd3289` 的相应行为。`b7e577ce4` 更广的统一检索 API 和后续 Python 架构删除
不属于本项。没有迁移停滞 Go 后端或复制上游测试 harness。

本次 `make verify` exit=0（4697 passed），`make smoke` exit=0。
Web API 205 passed、完整 test:ci 1112 passed；build、lint、文件大小棘轮和测试 inventory
通过。隔离 HTTP 验证真实 JWT/API Key、PostgreSQL 元数据和成员状态、Milvus 四种搜索
模式、隐藏图谱、跨数据集拒绝及独立 SDK 读回；真实 Web APIClient 对该服务完成检索、
元数据、聚合/文档图谱与鉴权验证。旧入口实际 HTTP 404、OpenAPI 移除及前后存储快照
不变已验证。embedding 输出受控，未将本项称为真实模型或上传解析 E2E。

最终完整 `make integration` exit=0：734 passed，无 skip，包含真实 Milvus 四模式
检索和 Infinity 零值查询、更新、删除回归。此前因补齐新回归的 SDK 查询 limit，
主动停止过一份已加载旧测试代码的运行；以上为修正后的完整复跑结果。
自有 HTTP listener、scratch SQL/存储资源及临时 Infinity 容器均已清理。
常驻 8123 服务仍加载旧路由，未重启共享实例；需重启后才会加载本次 REST 入口。

## d532151be06b3fd102a56808a979d059ef8c787d / 926efbd29b9bd5a5fa4c464c45b476efc4c0fbf9 · PaddleOCR 四算法

2026-10-03 核对两项完整 diff，并确认 upstream remote；fetch 后对照快照为
`98b48a085786fb9e14be8753b5a9a9ea02230ccf`。两个提交作为一项可用功能推进，
配置、请求与结果适配和前端配置入口已验收；真实云推理三算法通过，VL-1.5 的
任务仍为 pending 并触发超时。2026-10-04 用户调整完成标准为 RAGFlow 代码对齐，
不再继续实际推理测试；本项按此标准收尾，VL-1.5 真实云验收保留为未完成。

[解析器](../../deepdoc/parser/paddleocr_parser.py) 与
[模型入口](../../core/llm/ocr_model.py) 支持完整的 PaddleOCR-VL、PaddleOCR-VL-1.5、
PP-OCRv5、PP-StructureV3 算法名。保留默认 VL、嵌套/扁平/环境变量配置、token 请求头、
同步 PDF Base64、四种 parse_method 返回形状及已有页号、坐标和 crop 合同。
远程解析器初始化不加载本地 DeepDOC 模型；这也是后续 `9aa81e7ca` 修复的必要前置。

上游 `926efbd2` 统一使用 VL 参数和布局返回，不能覆盖通用 OCR 的实际合同。
本地按算法校验字段类型与名称并构造请求：PP-OCRv5 使用 OCR 参数，读取
`ocrResults[].prunedResult` 的 `rec_texts`、`rec_boxes` 或 `rec_polys`；
PP-StructureV3 使用 OCR、布局、表格、公式和印章参数；两个 VL 使用 VL 参数及布局块。
新增 HTTP/HTTPS 地址、timeout 和响应信封校验；错误结果结构不会作为成功空结果。
用户提供真实 Token 后，当前官方入口实际使用异步 Job API，故补入后续
`1235da7093122e6ac1fe493541385e6dd55eafa5` 的云协议行为作为可用云验收的必要适配。
完整路径以 `/api/v2/ocr/jobs` 结尾时使用 Bearer、multipart、算法参数、任务轮询及
JSONL；保留网关前缀与 query，结果下载不带 Token。整个请求使用配置的 timeout，
严格检查业务码、状态及结果结构，不自动重复提交。其余 URL 继续使用同步协议。
PP-OCRv6/VL-1.6、图像 chunker 和 Python 退役不纳入本项。
当前配置与云服务获取步骤维护在 [PaddleOCR 使用说明](../../deepdoc/parser/PADDLEOCR.md)。

最新 `make verify` exit=0（4762 unit passed），Ruff、全库格式、mypy、8 项 import
契约及 async DB 门禁通过。[专项集成](../../tests/integration/test_paddleocr_service_contract.py)
在最终源码上 11 passed，覆盖四算法的同步和 Job 协议：真实 PostgreSQL scratch
保存、关闭 session 后原生 SQL 读回、已保存模型重新实例化、真实 HTTP 上传两页
PDF、页号/位置与 crop；错误算法、非法 URL 和云 Job 缺 Token 拒绝且不持久化。
算法及 Job 单测 107 passed；上述 HTTP 服务响应受控，不是实际模型推理。
此前完整 `make integration` exit=2（717 passed、8 errors）；全部错误在
`test_infinity_available_filter.py` 的连接 fixture，Thrift `TSocket read 0 bytes`，
未进入其业务断言。云协议适配后的完整门禁以 `-x --ff` 复跑仍在同一 fixture 报错（1 error，
exit=2）；本项未修改该测试或共享服务，完整集成门禁不计为通过。

2026-10-04 定位到本机没有 Infinity 服务，Docker 内部地址 `infinity:23817` 被
代理 DNS 解析，TCP 探测成功不代表 Thrift 服务存在。使用与 SDK 对应的
`infiniflow/infinity:v0.7.0-dev5` 隔离实例，通过 `INFINITY_TEST_URI` 选择本机端口，
真实 Infinity 专项 8 passed；未修改共享配置或该 owner 的代码。完整集成复跑按
用户停止测试的指令中止（283 passed、KeyboardInterrupt、exit=2），不计为完整门禁
通过。自有 Infinity 数据库、容器及端口已清理，独立查询 PostgreSQL scratch 库为 0。

独立 Web 模型设置 owner 已提交 `9f4ccba`：四算法选择、配置构造、中英文说明及表单
回归，默认值和后端保存合同保持一致。配置校验成功只表示本地参数有效，不表示云服务
或 token 可用。该提交 Web `test:ci` 1112 passed，lint、build、文件体积及 bundle
检查通过。原生浏览器挂载实际 ModelProvidersPage，经真实模型 API listener 保存四算法，
独立 PostgreSQL 连接读回；非法 URL 前端零请求、非零业务码保留草稿与已有模型行。
中英文、明暗主题、键盘选择及 Token 掩码已核，根任务复查标注 contact sheet。
此环境使用注入的 scratch 授权 principal，生产登录未验收，OCR 推理请求为零。
后续 `395e778cca5f5ee670abcc1c83663e57b08f5078` 补齐官方 Job 地址提示、必填 Token
与同步免 Token 兼容；定向 Node 13、实际弹窗 7、product UI 33 项通过，lint/build/
size/bundle 通过，未重跑全量 CI。原生页面经真实 API 保存官方、网关 Job 和同步
配置，独立 SQL 精确读回完整 URL；空 Job Token 前端零请求，verify 不落库。
根任务再次复查六文件 diff、证据和标注 contact sheet。自有 SQL 行、listener、容器、
端口和测试入口已清理，浏览器清理入口确认存储 0/0。

真实 Token 与官方 Job API 的云验收独立执行，没有修改业务模型配置。四算法经实际
add_llm handler 保存到自有 PostgreSQL，关闭 session 后独立 SQL 读回并实例化。
PaddleOCR-VL、PP-OCRv5、PP-StructureV3 均实际完成两页中英文表格 PDF 推理，得到
8、18、8 个 sections，中文、两页标识、页号、非零坐标及 crop 验证通过。
VL-1.5 首次云请求被 queue-full（10010）明确拒绝；后续两页解析触发默认 600 秒
超时。单页诊断实际提交 HTTP 200 / code 0，但 180 秒内一直 pending；超时后独立
查询仍为 pending。因此不能把 VL-1.5 或四算法整体记为云验收通过。真实推理保存链路
使用 scratch principal，未覆盖生产登录或完整后台文档入库。自有数据库已 DROP 并
独立确认不存在；凭据仅保存在本机私有配置，未入库或写入公开日志。

最后一次最小对照请求去掉所有可选参数，VL-1.5 仍返回 HTTP 400 / code 10010，
同一单页 PDF 的 VL 请求完成；与官方“任务提交队列已满”的错误码定义一致。
用户决定停止实际测试后，没有安装本地 OCR 模型，也不再继续云请求。再次复核两个
目标的完整 diff，四算法枚举、默认模型、模型设置选择入口及配置/请求/解析支持均已
覆盖；保留上文说明的算法专属合同、同步兼容和官方 Job 协议适配，不宣称逐行复制。

## 4e5a093ac53db931fe4e8d47b19ec6e0ffd15c8b · Go Moonshot 聊天与推理流

2026-10-03 按目标完整单文件 diff 跟进，预期 remote 已核为 `infiniflow/ragflow`，
fetch 后 `origin/main` 为 `98b48a085786fb9e14be8753b5a9a9ea02230ccf`。
目标新增普通聊天与 sender 流式聊天，未发现目标行为被撤回；沿当前 ModelDriver
接口适配 `APIKey` / `ReasoningContent`，复用已存在的工厂注册、模型目录、地域 URL、
端点后缀、模型 thinking 默认及显式 false 优先级。

[Moonshot driver](../../internal/entity/models/moonshot.go) 实现普通文本响应和推理
SSE；请求共用 max_tokens、temperature、top_p、do_sample、stop 与 thinking 配置，
普通调用固定 stream=false，sender 调用固定 stream=true。同步推理字段可选，
返回时移除首个前导换行；流式推理与答案同 delta 均转发。
本地历史聊天消费者还需要 `ChatWithMessages`，因此补齐完整 system/user/assistant
历史。该旧接口只接收 APIKey，地域仍由请求级 driver 绑定，未扩展其 context 合同。
单消息和 sender 接口使用现有 `APIConfig.Context`，客户端仍为 120 秒超时。

后修 `d63bd81d0` 的请求模式固定和 `04aa8d04e` 的大 SSE 事件修复纳入本次路径；
已有模型列表/余额的无正文 GET 与结构化解析保持。未引入后续统一 HTTP pipeline、
工具调用、token usage 或接口整体迁移。按照当前 VolcEngine 的本地流合同，坏 JSON、
供应商业务错误、空答案及提前断流显式失败；只有实际答案与 `[DONE]` 才发完成帧，
sender 错误原样返回，取消可由 `errors.Is` 识别，错误不回显供应商正文或记录流内容。
旧 channel-only streaming 保持明确不可用，embedding/rerank 不在本项扩展范围。
当前合同见 [Go Provider API](../references/http_api_reference.md#go-provider-api并行实现)。

本次 Go 1.25.14 下 Moonshot 专项 `go test -race -count=1 -run Moonshot`，覆盖
[driver](../../internal/entity/models/moonshot_test.go) 与
[实际目录和服务默认](../../internal/service/model_service_moonshot_test.go)，
44 个测试及子测试通过、无 skip。受控真实 HTTP 检查请求路径、认证、参数、响应、
大事件、双字段 delta、坏响应、sender 各阶段错误与响应读取中的真实连接取消。
使用 HEAD 原始 Moonshot 文件的 Go overlay 复跑正常聊天、流式与历史回归，
均在旧桩明确失败，证明测试覆盖本次缺口。

`go test -count=1 ./internal/entity/... ./internal/service ./internal/handler ./internal/cli`
209 个测试及子测试通过，5 个既有 opt-in live 用例未启用，不计为集成通过。
`gofmt`、`go build ./internal/...`、`go vet ./internal/...` 及三个独立 cmd main
构建均 exit=0；仅已有 go-m1cpu C 编译告警。日志为 `/tmp/multirag-moonshot-*`。
本项不改 Python、数据库、路由和启动流程，未运行 Python make verify/integration/smoke；
受控 provider HTTP 不是实际 Moonshot 账号、生产身份或 Web E2E 验收。

## b493a3331607dac3e254ff04e2638180e409f43f · Go 模型聊天路由与 CLI

按冻结快照 `519e7d98a5651564d4e35d6648f006cba4baaf4f` 核对完整三文件 diff。
模型聊天改为认证组内的 `POST /api/v1/chat/completions`；provider、instance、model 与
message 由 body 提供并校验，CLI 普通/流式与当前选中模型均同步，旧 models POST 不再注册。
保留现有业务码、JSON answer/reasoning、sender SSE、租户凭据与请求 context。
`stream` 和 `thinking` 用指针保留缺省/false；思考默认继续由受信模型目录决定。

后修核对：`265f92c83`/`12af73f2c`/`733591686` 的调用合并与多模态属于下一项
session 迁移的相关链；后期 model ID、新 OpenAI 兼容 API 和 Go 全量路由重构不扩纳。
未带入上游初版布尔值丢失或回显错误正文的行为。CLI 额外修正带空格的 SSE error 解析，
要求完成帧后才报告成功，避免提前断流产生成功结果，scanner 与现有 provider 上限一致。

验证：Go 1.25.14，CLI/handler/router race 回归通过；`go build ./internal/...`、
`go vet ./internal/...` 和三个独立 main 构建通过。Google SDK 和模型系列的专项真实
HTTP + 隔离 PostgreSQL 验收通过，含业务码、SQL 独立读回、默认/false、SSE 顺序、
错误、禁用模型及断开请求取消；两个自有 scratch 库已删除。认证组无凭据拒绝测试通过；
专项 provider HTTP 使用受控身份和响应，不代表真实账号或生产 JWT/API key E2E。
Go 工具链既有 C/linker 警告保留，未修改依赖。未改 Python；未运行 Python 门禁。

## f3c232cf47626c332d0aa7caee614715afeb214c · Go session 退出 ModelBundle

基准仍为冻结 `519e7d98a5651564d4e35d6648f006cba4baaf4f`，在前项模型聊天路由稳定后完成。
完整六文件 diff 已核；相关后修 `265f92c83` 合并聊天入口、`12af73f2c` 增加历史消息流式、
`733591686` 复用 GetChatModel 均已检查。仅吸收本项所需的调用合并和历史 sender 思路，
不扩展上游的图片/附件、多模态或后期 OpenAI/session/RAG 体系，也不修改 Python 模型体系。

- 复用前项已具备的租户 bound models、两段/三段模型名与默认模型解析、region 副本、
  旧 tenant_llm/APIBase 兼容、模型系列/能力区分；VolcEngine 与 Moonshot 注册和协议保持。
- session 直接使用 GetChatModel，不再经过 Bundle；stream/thinking 缺省与 false 保留，
  模型、dialog 与请求配置按优先级合并，ModelClass 仍只来自受信目录。普通历史接口接收
  APIConfig；五家已有历史能力的驱动复用完整 system/user/assistant 角色，sender 真正增量
  转发 reasoning 和答案，context 到 provider/SQL 写入。旧 channel 聚合适配已无消费者并删除。
- 不采用初版把历史拼成单条文本或异步 goroutine 吞错的做法。sender/provider/取消/真实
  SQL 写入失败返回调用方，不发送成功终帧；持久化检查错误并保存完整历史与助手答案。
  指定模型的临时调用仍不落库。保留 session SSE 的 conversation_id/message_id/reference。
- Zhipu 历史路径复用原协议并补 context、thinking、错误/提前 EOF；有答案的 finish_reason
  或 [DONE] 是有效终态。其他驱动原来未具备的历史能力保持明确不可用。
- 全仓 Go 搜索确认 Bundle 只剩上述消费者和测试，旧 entity 接口与 ModelConfig 无其他调用。
  消费者迁完后删除 Bundle、旧接口及死 helper；向量回归迁到 bound Encode，数量、非空、
  维度、有限值校验与实际检索链保留。token 估算仅测试在调用，无运行时消费者，随旧抽象退出。
- datasets 的显式实例模型名复用现有严格解析与租户绑定，不用无关旧默认模型凭据授权。
  两段名的既有 legacy/Builtin 检查保留，不另建模型抽象。

验证：Go 1.25.14，全 `internal/...` race 回归通过（176 个顶层通过、8 个 opt-in skip）；
随后补核 effort 别名与持久化 JSON 参数的模型专项 race 回归通过。build、vet 和三个独立 main 构建
通过。另实际启用 5 个相关 opt-in：Google SDK、模型系列、session HTTP/SQL、session 服务
SQL/HTTP、模型绑定/检索，均零 skip 通过；隔离 SQL 独立读回、失败时不保存部分答案、
PostgreSQL 触发器真实拒绝写入、HTTP 断开取消均验证。自有 scratch 库及私有配置清理完成。
先前 fixture 状态缺失和构造参数缺失的失败已修复并复跑，未放宽断言。

保留边界：受控身份与 provider HTTP 不等于远程账号或生产 JWT/API key E2E；Go session
仍不具备实际 KB/Tavily RAG、附件或多模态，本项未移植后续完整能力。三个无关 live 门禁
未启用。工具链既有 C/linker 警告保留，未改依赖、共享配置、Python；未 push。

## 0d18b293 / 74fa54f1 / 3b7a6eaa · 三连接器源端删除同步

2026-10-04 依次核对冻结 `519e7d98a5651564d4e35d6648f006cba4baaf4f` 中
Airtable、Google Drive、Bitbucket 三项完整 diff、相关后修和当前 Python/Web 消费者。
同一 agent 顺序适配，共享入口沿用 `42d0f153` 的可信完整快照、知识库/连接器身份隔离与
事务后存储清理；Google PKCE 沿用 `f8e45761`。未 fetch 或移植后续 Go 架构替换。

- `0d18b293`：Airtable 新增全量附件身份清单，复用入库来源 ID；直接校验 SDK 原始分页，
  避免缺失 records 被默认为空。清单不下载正文，附件下载失败阻断本轮成功。
- `74fa54f1`：Drive 使用身份字段清单，拒绝权限错误、incompleteSearch、异常分页和
  未覆盖的指定范围；清单缓存独立于内容检查点，补回 Shared Drive 续页 token 传递。
  已有内容窗口终点前置捕获和刷新凭据持久化等价复用。相关缓存后修 `e0b307001`、
  空清单修复 `5fd4579a2`/`911671cef` 及身份后修链均已核对。
- `3b7a6eaa`：Bitbucket 已有无时间窗口的轻量 PR 枚举等价于上游 connector 部分；
  接入 worker/Web 开关，并拒绝异常集合、来源身份和分页循环，内容映射失败不推进成功。

三个开关默认关闭；首次导入和重建跳过删除。当前模式、完整清单合同、配置范围与外部
存储清理限制统一见 [连接器 README](../../common/data_source/README.md)，不重复维护。
Web 提交 `02681e3` 接入开关及 Bitbucket 账号邮箱，`06e0039` 补充 Drive 服务账号/OAuth
范围提示；原有配置序列化沿用。Drive 邮箱范围为可选，OAuth 留空，指定邮箱使用服务账号。

验证：共享最终 `make verify` exit=0，4875 个 unit 通过；`make integration` exit=0，753 项通过、无 skip（2517.23 秒）。
门禁前后输入 hash 无漂移，日志 `/tmp/multirag-a7ce-final-gates/`。
默认 Infinity 实例与当前 SDK 的握手不兼容；最终集成仅用测试进程 `INFINITY_TEST_URI`
指向独立 loopback 兼容实例，未修改业务配置或依赖。连接器专项采用受控 SaaS 页响应，
真实隔离 SQL/Milvus/MinIO/Redis 存储验证；失败清单、取消、成功空清单、过期清理与
相邻连接器保留均覆盖，不代表真实 SaaS 凭据端到端验收。
Web 表单 13 项通过，build/lint/file-size exit=0（lint 保留既有告警）；三源明暗主题、
键盘开关与中英文 Drive 帮助已在当前页面验收，未创建真实数据源。未 push。

## 486ca463aadf1a5ff088e6879efa4ac1f54ae2b2 · Go 删除残留检索过滤

对照完整四文件 diff；上游 remote 已核为 `infiniflow/ragflow`，目标为冻结
`519e7d98a5651564d4e35d6648f006cba4baaf4f` 的祖先，未 fetch 或越过冻结上限。
已向 Go provider owner 发送接口边界交接；本项保持 `ChatConfig`、绑定模型和 provider 接口。

- 实际缺口：公共 Go `Retrieval` 在评分前批量查询 SQL 文档；缺少字段、缺少文档 ID、
  物理删除或不匹配 SQL 数据集的候选被剔除；SQL 错误传播，不让未验证正文进入 rerank。
  构造器绑定 DAO，现有 `ChunkService.RetrievalTest` 自动消费，无需新增旧的 ModelBundle 链。
- 本地生命周期适配：禁用文档不是删除。全库 RAPTOR 的 `graph_raptor_x` 与 `raptor_kwd`
  标记需要有效的所选 SQL 数据集；文件 RAPTOR 仍查文档。Infinity 固定投影补读标记。
  保持已接受的派生产物生命周期；未重复 Python `a7ce1b16` 的实现。
- 消费者补充：parent 展开在过滤后再次读索引，按文档/数据集分组并校验同源；旧 parent ID
  碰撞或跨文档残留保留已验证 child，避免重引入未验证正文。完整 `8afebbb67` 的存储查询、
  多租户和其他合并行为不随本项移植。
- 已等价：session 的 `buildGenConf → ChatModel → chatGenerationConfig` 用 typed JSON
  unmarshal 转换整数 `max_tokens` 和字符串数组 `stop`，覆盖存储设置、请求覆盖和流式请求。
  不恢复已删除的 `buildChatConfig`，不为等价部分新增生产代码提交。保留本地对小数/溢出
  token 数及数字 stop 元素的错误；上游截断/静默丢弃会削弱现有校验，建议继续保留本地契约。
- 相关后修：采用 `a78a3fdd4` 的空 IDs 查询短路；沿当前 session/模型重构和 context 适配。
  不复制 `5bb5ba221` 之后的全索引计数机制，不扩展到冻结末端的整套 Go pipeline。

当前行为、count 边界和专项运行方式见 [Go 检索说明](../go-retrieval.md)。
本次使用 Go 1.25.14 与实际 C++ tokenizer 库验证：

- `go test -count=1 -json ./internal/...`：12 个测试包通过，182 个顶层用例通过
  （含子用例 484 passed）；9 个 opt-in 用例跳过，未把 skip 计作验收。
- 本项检索与 session 两个 scratch 用例另行启用并通过，无 skip。真实 PostgreSQL 删除
  独立 SQL 回读、Infinity 删除残留索引、rerank 入参、parent 消费者、有效/失效全库 RAPTOR
  及存储/请求/流式 JSON 配置均验证；SQL/Infinity 自有库删除后回读不存在。
- `go build ./internal/...`、`go vet ./internal/...`、DAO/检索/session/Infinity
  race 检查、三个独立 cmd main 的构建均 exit=0，验证期间 Go 输入未漂移。
- 本次 `make integration`：755 passed、无 skip，exit=0；1697 个 Python/配置验证
  输入前后哈希一致，自有 Infinity 测试容器及监听端口已清理。

未做 MySQL、生产库、真实外部 provider 或 Elasticsearch/Milvus 完整检索验收；
provider 使用真实 HTTP 协议替身。Python `a7ce1b16` 生产实现未改动。

## b684c899 · 旧 API 兼容入口等价评估与文档校正

2026-10-04 按 `b684c899501ce6b7236d3027f66b75d1097e4873` 完整七文件 diff 与冻结
`519e7d98a5651564d4e35d6648f006cba4baaf4f` 内相关后修核对。上游 remote 正确；
本地 `origin/main` 已移动到 `98b48a08`，本项未 fetch、未改变来源边界。开工 HEAD
为 `bea3a899`，已复核旧 run、图片和 change_parser 的接受状态与实际注册。

| 原始行为 | 本次处置与依据 |
|---|---|
| 注册集中 Quart 兼容 Blueprint | 不复制；FastAPI 当前自动发现已加载 REST/SDK，不新增重复 router，避免同 path/method 抢占。 |
| 旧聊天补全与 related_questions 转发 | SDK `question/session_id` 及 `industry` 仍是公开本地合同；前者无 session 创建开场白，后者使用 API key。不能以 messages 转发或 search_id 推荐接口替换；现有行为保留。 |
| 旧 OpenAI 聊天路径 | 已等价：同一 handler 的 deprecated 别名、API key、默认流式与当前非流式合同保留。 |
| 会话 PUT → PATCH | 已等价；本地 `09442a59` 明确因无生产消费者而直接退出 PUT，路由测试也断言不存在。原 HTTP 参考漏更新，修正为 PATCH，不恢复 PUT。 |
| DELETE chats 的 chat_id body | 不恢复；当前 Web dialogAPI 用单项 path 或批量 ids。独立 SDK worktree 同样用 path。上游直接 update_by_id 的分支未核归属，不导入。 |
| file/get/list/ancestors/parent/root/create/upload/mv/rename/rm | 当前 Web、SDK、MCP、Agent Hub 未发现这些旧入口消费者；现有 files REST 和显式 files/root 提供替代合同。不上游 root → list 的形状变化或绕过 rename 校验。 |
| chunk PUT | Web、Agent Hub 及 SDK worktree 均用 PATCH，不恢复无消费者入口。 |
| 旧 file/upload_info | Web 使用 documents/upload，SDK files/upload_info 保留 files 字段；不恢复旧网关。 |
| file 三个同步 handler 改 async | 当前早已为 async，复用 AsyncSession/run_sync 及现有阻塞 IO 边界。 |
| 上游两处测试调整与文档 notices | 不复制 Quart 测试 harness；本仓追加完整 HTTP/SQL 回归，修正会话补全 URL、认证归属和 SDK 响应示例。 |

后修 `c11650bb4` 文件祖先归属、`0c93161a1` 会话认证身份与
`d3542463c` recommendation 拼写在现有实现中已有对应。其余后续 Agent/Graph/文档兼容扩展、
legacy stream 模式和 Python 架构退出不扩纳；尤其不恢复已接受退出的 run、图片、
change_parser、upload_info 等入口。本项的当前合同见
[REST README](../../api/apps/restful_apis/README.md#聊天会话兼容)，映射表已更新。

消费者核查为当前源码审计；两个独立 SDK 目录属于同仓未合并 worktree，其中旧的
session-scoped completion 路径与当前 production 路由不符。用户明确选择记录为 SDK
后续迁移；未据此恢复相邻接口，不声明这些分支已完成聊天端到端迁移。
Web/Agent Hub/MCP 没有本项新增客户端代码。

SDK 后续入口：两个 worktree 的 `sdk/python/src/multirag_sdk/_resources.py` 中
`SessionsManager.complete/stream`。迁移时优先核对现有 question 接口与 body 中的
session_id，再校验 Completion 模型、SSE 收尾、错误帧及历史读回；不能只更换 URL
便声明兼容。当前分支头分别为 `ed38de7f` 与 `d0d34818`，属本次核查时点。

复核纠正：首轮 `1e7ab115` 把滞后文档误判为兼容承诺并恢复 PUT；经用户指出、
核对本地 `09442a59` 的明确退出说明、路由 diff 与测试后，用户同意撤销别名。
保留 PATCH 请求校验、真实写入与独立 SQL 读回回归，补 PUT 实际 HTTP 405、
不进入业务层及会话读回零变化断言；其他等价评估和 SDK 后续迁移记录保留。

首轮历史验证：`make verify` 4886 unit、`make integration` 755 passed 无 skip，
1697 个源/配置输入无漂移；证据 `/tmp/multirag-b684-final-gates/`。
其中新增 PUT 的通过仅说明当时实现可运行，不构成应恢复该接口的依据，也不作为
撤销后的本次验证结果。

纠正验证：`make verify` exit=0（4969 unit）；
`make integration TESTS=tests/integration/test_chat_session_compat.py` exit=0（2 passed，无 skip）；
`make smoke` exit=0。1714 个源/配置输入前后 hash 无漂移，证据位于
`/tmp/multirag-b684-correction/gates/`。真实 JWT/API key 的 PATCH 两次更新、GET 与独立
SQL Session 读回通过，归属/保护字段/非法 body/无坏认证继续拒绝且零写入；PUT 对
有效、保护字段、非法 body 和无认证均返回 405，完整会话读回不变，OpenAPI 不注册 PUT。
旧 run/image/change_parser 与未恢复 rename 实际 HTTP 404，SQL 与隔离对象清单不变。
本轮未重跑此前完整集成套件；仅撤销路由别名，未修改 DB/事务/存储业务实现。
其他任务的 fixture 导入迁移保留，提交只包含本项逻辑与账本段落。未 push。

共享 `8123` listener 的首轮健康 smoke 通过，未重启它；只读 OpenAPI 仍列旧
change_parser，说明进程未重载相关退出提交。当前源码的退出路径由隔离 listener 验证；
不把共享健康结果当作相关退出已部署的证明。未做生产写入或真实远程模型验收。


## Dataset RAPTOR scope 持久化与扩展配置

2026-10-04，核对上游 `a0f9ae16d2d84660bc5ee7db8acf7a89a697c3e3`，
冻结上限 `519e7d98a5651564d4e35d6648f006cba4baaf4f`。本次行为合同见
[Dataset RAPTOR scope](dataset-raptor-scope.md)。

后端创建模型补齐 scope 与 parser/RAPTOR ext；Dataset PUT 对有效配置中的
显式 scope 做同一 Literal 校验，保留局部合并和旧 ext 覆盖顺序。Web 的
`scope` 字段、radio 控件及提交顶层字段已等价，本次 Web 提交
`8690b4e45ef1909d3e646d7edce3bcac92cfde26` 修复 Zod 剥离未知配置、ext
及 metadata 限制字段的问题。document PATCH 已由 DocumentRaptorPatch 声明
同一 scope，并使用 exclude_unset 的严格局部合同，本次未改它。现有详情读取
透传 parser_config，RAPTOR 消费者读取顶层 scope、缺省使用 file，均无需重复实现。

上游该 diff 的 query ext JSON 失败日志只影响诊断；当前 Dataset 保存走
FastAPI body 校验，不经过该 query helper，本项保留其现有合同，不复制格式改动。
冻结范围内的后续 `6ec9c6a73` 已移除上游 Python Dataset RAPTOR 设置；本次按
明确目标保留本地 Dataset、document PATCH 和索引语义。建议知识编译/API 迁移
作为独立兼容性评估，不在本项撤掉现有用户设置。

浏览器使用生产 RAPTOR 控件、schema 和 Dataset API，实际保存 file/dataset，
包括显式关闭和启用状态，GET 重载及整页刷新后选择保持；独立 SQL 读回核对
扩展字段及自动元数据限制。raptor_task_id 为空、chunk_num=0，配置保存不代表
解析或索引执行完成。专用资源与证据位于本机 `/tmp/multirag-a0f9ae16/`；
不修改共享配置，不 push，不把其他 owner 的输入、提交或验证结果计为本项证明。

本次固定输入的隔离副本 `make verify` 通过（4909 unit）；`make integration`
退出 0（764 passed、1 skipped）。唯一 skip 为真实 Web client 测试，隔离副本
缺少相邻 Web 目录；显式指定 `/Users/xldu/project/web` 后该项独立补跑 1 passed。
执行前后输入哈希无漂移，本项真实 HTTP/SQL 集成 10 项通过。Web 相关表单测试
9 项及 API 测试 205 项通过，build、lint、文件体积棘轮和提交钩子通过；lint
有 1454 条既有 warning、0 error，build 仍提示大 bundle。`make smoke` 通过，
fixture 的 storage 健康项为可选 NOK，其他必需健康项及 ping 正常。

验收使用生产表单控件、schema 和真实 API 的隔离页，不是完整生产设置页 E2E；
数据库保存使用隔离 PostgreSQL，未验 MySQL、生产数据或真实模型的 RAPTOR
解析执行。上述限制不影响本项配置保存与重载证明。

共享验证窗口释放后按原始差异应用，当前仓库 Ruff 与 format check 通过，
23 项 scope unit、10 项真实 HTTP/SQL 集成再次通过；相关输入无漂移且与隔离
验证副本一致。Dataset service、document PATCH 配置模型及执行消费者未改动。

## e0b3070012b7f9cda16e06812ac165bef1f5bea0 · Gmail 删除同步与 Google discovery

冻结上限为 `519e7d98a5651564d4e35d6648f006cba4baaf4f`，目标提交在其祖先链中。
Python 后续只见 validation/格式化与最终 `670e68872` 移除 Python；没有把冻结树中移除
Python 的架构迁移带入本仓现行生产后端。

| 上游差量 | 本地结果与依据 |
|---|---|
| Gmail driver 收集 slim 清单并返回 tuple | 接入现有 `SyncBase` 的可信完整清单合同，不新增第二次清单收集或 tuple 分支。默认关闭；首次导入/重建不删除。清单无时间窗口且正常耗尽后才交付；完整空清单可删除最后一份。分页/目录/邮箱读取及本轮入库失败阻断删除，失败保留开始检查点。 |
| Drive ID 缓存、凭据加载失效和返回副本 | 已由 `b31fa75c2` 等价交付；本次没有重写缓存。现有清单前后缓存失效也保留。Google driver 仍按原路径保存刷新凭据；新增 Gmail driver 配置保留回归。 |
| Drive 空 shared-drive 日志 | 按指定 Drive / 全 shared-drive 配置及凭据类型区分告警和 info，移除未请求时的误报警。上游同步器删除的额外开头日志在本地共享枚举入口没有对应重复项。 |
| Google service build 关闭 discovery cache | OAuth 与服务账号两条构建分支均显式 `cache_discovery=False`；Gmail、Drive、Docs、Admin 公用工厂回归。 |
| Web Gmail 删除开关 | 独立 Web 复用可信来源 registry、默认值合并及现有中英文案；布尔值创建/编辑保存、未知配置及凭据保留。提交 `b5e0df815f06230ad92f541384c5067afdc9db3b`，未 push。 |

Gmail 特有的适配：OAuth 仅自身邮箱，Workspace 服务账号完整枚举域用户且须包含主邮箱。
使用 Gmail 的 `resultSizeEstimate` 形状确认明确空响应，不套 Drive 的 `kind`，不以估算
数量证明总数。循环 token、异常空响应、权限/禁用邮箱错误及正文读取错误都中断删除。
关闭开关时保留历史内容读取行为。源 ID、Spam/Trash 范围及时间查询合同继续沿用，
长期兼容性、重建办法和 History API 建议统一见 [连接器说明](../../common/data_source/README.md)。

验证：共享窗口实际源码 `make verify` 退出 0，lint/import-linter/async DB/mypy 通过，unit `4955 passed in 45.93s`，输入无漂移；候选源码同样通过（4932 unit）；不可变候选的 `make integration` 退出 0，`763 passed, 1 skipped in 2605.85s`，输入无漂移；其中唯一 skip 是隔离目录默认路径找不到独立 Web checkout，显式 `WEB_DATASET_CHECKOUT=/Users/xldu/project/web` 补跑该项 `1 passed`，未将 skip 计作通过。
当前 SDK 对 Gmail、Drive、Docs、Admin 的 OAuth 和主体代理服务账号两种凭据均完成
离线 Resource 构建（8 项）；未发出 token 或 Google API 请求。
仓外候选受控回归 `9 passed`，无 skip：真实 Google discovery Resource/httplib2 对本机
Gmail HTTP 服务，全分页/完整空、后页权限失败、异常空响应、正文失败与入库失败；
真实 scratch SQL、Milvus、MinIO 独立读回，配置 HTTP 业务 retcode=0 并 SQL 读回。
9 份资源记录确认自有 SQL 行、collection、对象桶、缓存/队列和 listener 清理，scratch
库关闭后不存在。完整集成使用自有 PostgreSQL/Valkey/MinIO/Infinity 容器和 Milvus
自有 collection；结束后四容器及两卷清理，183 个 collection 独立读回均不存在。
初次集成 fixture 缺少 connector-KB 绑定导致 2 个删除断言失败，补齐
真实绑定后回归通过；没有放宽删除断言或改业务成功判据。

Web 两份 datasource 测试 `15 passed`，product UI `3 + 30 passed`；lint、file-size、build
及提交 hook 正常通过（lint 保留现有 warning）。浏览器实际检查中英、明暗主题、默认
关闭及键盘 Space 切换，未保存测试连接器到业务库；截图见本机
`/tmp/web-e0b30700-ui/contact-sheet.png`。

完整门禁的源码、资源与退出证据见本机 `/tmp/multirag-e0b30700-full-integration/`、
`/tmp/multirag-e0b30700-shared-verify/`。受控服务不是 Google 真凭据验收：未验证实际
Google consent、Workspace 域委派、权限策略、token 刷新或真实 Gmail 邮箱。未部署，
未 push，保留其他 owner 的改动。

## 96909235 / 6afb1957 / 3991bdfa / 1b84892e · 索引类型与图谱删除调用链

2026-10-04 按顺序核对四个目标的完整 diff，上游 remote 为 `infiniflow/ragflow`，
冻结上限 `519e7d98a5651564d4e35d6648f006cba4baaf4f`，四项均在上限内，未 fetch。
目标为 Python 后端及独立 Web，统一维护共享调用链。

| 目标 | 本次结论 |
|---|---|
| `96909235167edc0d1a9b5ed4cc104efea92f522b` | 已等价。Web 既有 `75db9786` 的 `DatasetIndexType` 只允许 `graph/raptor/mindmap`，生成任务 hook 显式映射 UI 类型，删除统一调用 `/index?type=`；后端 `_delete_index` 已转小写，不恢复任意类型路径。 |
| `6afb1957d88d8473334d0f994dd93d3e3d4fa2af` | 补齐 run/trace 网关的 `type.lower()`，与现有 DELETE 一致。service 的严格合法集合、JWT/API Key 与数据集成员权限不变；空、未知、GraphRAG、空白或 NUL 类型仍拒绝。 |
| `3991bdfaf57dafcca398295f399d88f8dc78aad2` | 已等价。UI 标签为 `GraphRAG`，请求为 `graph`，真实 queue/Task/trace 为 `graphrag`，绑定列为 `graphrag_task_id`，worker 日志映射为 `PipelineTaskType.GRAPH_RAG`（值 `GraphRAG`）；前端日志枚举与当前生产者匹配，不机械改为 Graph。 |
| `1b84892e3ab5be550381530b574afbae57a2f11b` | 删除地址已等价。前端生成任务删除复用统一 index DELETE，另一个 graph API 只负责读取。本次增加真实删除及独立读回，保留取消信号、任务解绑、特定产物和日志的生命周期。 |

后续链：`ff685d313` 移除上游重复 DELETE graph 路由并添加 query index DELETE，
支持复用本地统一契约；本地明确的三个路径别名不改。`e8f19aa33` 带来的
`wipe=false` 与阶段缓存是独立恢复能力，本地当前没有阶段缓存生产者，不在本次追加。
`670e68872` 整体移除上游 Python 后端不作为本地撤回。

实际证据：四文件相关 unit **60 passed**；隔离副本 `make verify` **4894 passed**，
Ruff、8 条 import contracts、async DB gate、mypy **132 files** 均 exit=0。
独立 worktree 最终 `make verify` exit=0，**4849 passed**（不含其他任务尚未入库的新增测试）。
[真实 HTTP 回归](../../tests/integration/test_dataset_index_http.py) 在最终 worktree **4 passed、零 skip**：
JWT/API Key 各执行三类索引、三种大小写的 run/trace/delete，检查业务码、SQL Task 类型与
KB 绑定、独立 Redis queue/取消键，以及独立 Milvus 读回的完整夹具行（含向量）。
同物理索引的兄弟数据集、跨租户索引、普通块、mind_map、Document 与摄取日志保全；
重复运行仍拒绝，删除后 task/绑定/完成时间清空、trace 为空。非法类型、未认证及跨租户
拒绝零写入；自有 listener 上 `make smoke` exit=0；夹具 SQL/Redis/collection/端口清理读回通过。
专项结束后另开新连接确认 scratch 库、8 个 Milvus collection、测试 Redis 键及 4 个监听端口均已消失。
首次夹具缺 File→Document 关系，第二次把生产字符串 `raptor_kwd` 错建为数组，修正夹具后
按真实生产字段重跑通过，没有放宽断言或替换生产服务。Web 现有 API 合同 **205 passed**、
生成任务生命周期 **6 passed**，没有 Web 源码改动或人为等价提交。
最终 worktree 指定 `WEB_DATASET_CHECKOUT=/Users/xldu/project/web` 的实际 Web→隔离 HTTP 消费者回归 **1 passed**。
完整 `make integration` 在开工隔离快照执行（含当时其他任务 WIP），exit=0，
**758 passed、1 skipped**，2649.12 秒；唯一 skip 是快照兄弟目录没有 Web checkout，
已由上述最终 worktree 的指定路径消费者回归补跑通过，skip 本身不计通过。
完整门禁前后输入 hash 无漂移；索引入口/service、鉴权、Task/queue、存储适配器、夹具和依赖
共 16 个相关文件与最终 worktree 对照一致，最终 worktree 另有完整 unit 和专项真实 HTTP 验收。
默认 Infinity 实例握手不兼容，完整门禁仅通过测试进程 `INFINITY_TEST_URI=127.0.0.1:63316`
选择既有隔离兼容实例，未改共享配置或依赖。日志与读回账本在 `/tmp/multirag-index-port-evidence/`。

### 单列后续修复

- **Graph 社区报告残留**：本地 `with_community` 生成 `knowledge_graph_kwd=community_report`，
  GraphRAG 检索会消费；现有 index DELETE 的四类过滤未覆盖它，上游后修已包含该类。
  用户明确选择保持本批范围，本次不扩大删除集合。后续应把社区报告纳入 graph 删除，
  验证目标报告消失、兄弟/跨租户报告保全，并独立读回检索行为；本次正常删除验收只覆盖现有四类。
- **存储失败/取消失败反馈**：现有 Milvus `delete()` 发生后端错误可返回 0，取消 `set()`
  也可返回 False，index service 未核对这些返回值，Task 删除与产物删除不是跨存储事务。
  本次错误 schema 夹具实际观察到存储错误未传播为非零业务结果，产物残留，正常生产 schema 读回通过。
  后续应先让存储/取消异常进入明确失败状态，并定义可重试的成功判据，保留任务/恢复材料和故障注入；
  不能把删除计数 0 一律判错（也可能已无产物），需区分后端错误与幂等空删除。本次不改变该兼容行为。

未运行真实 GraphRAG/RAPTOR worker、LLM 或生产数据验收；本项生命周期验收使用本机
PostgreSQL、Redis、Milvus 与隔离 HTTP，其他索引存储后端及完整社区报告删除未验收。共享 Python freeze 期间使用隔离副本
和独立 worktree，不修改主检出的门禁输入；最终范围提交位于 `codex/dataset-index-types`，
精确暂存五个本任务文件并使用短 index 锁，主检出和其他任务 WIP 保留，未 push。

## decf6730 · Go Ark thinking 默认与 effort

冻结基准为 `519e7d98a5651564d4e35d6648f006cba4baaf4f`。已有 Ark 普通聊天、完整角色
历史、正文/推理 sender、模型发现、取消与安全失败实现复用；仅补模型级 thinking /
clear_thinking 默认。已有 effort 映射等价：未指定时 enabled/medium，
none/minimal 关闭，现有 xhigh 兼容保留。模型目录与两种请求模式的 effort 回归见 Go entity/models 测试。
未迁移 Python 模型体系，未执行远程 Ark 账号或生产验收。

验证：Go 1.25.14，相关 entity/models、service、handler 的 race 回归通过
（208 个通过，7 个既有 opt-in 未配置而 skip；未把这些 skip 记为验收）。
`go build ./internal/...`、`go vet ./internal/...` 与三个独立 main 构建均 exit 0。

## bb05a8bd · Go 实例 URL 与自定义模型声明

冻结基准仍为 `519e7d98a5651564d4e35d6648f006cba4baaf4f`。完整 diff 涉及的 CLI
语法/lexer/执行/300 秒 timeout、实例 API、自定义模型 API/路由、Extra、模型列表与
实例 driver 已覆盖。已有 TenantModel.Extra、ModelType 与绑定模型接口复用；目录
features 保留。普通/历史/sender 调用均使用实例 driver，禁用、数据库错误、取消和
租户归属继续失败关闭。自定义启停保留元数据；重复模型声明在实例行锁事务内只产生一个赢家，
实例名重复通过 provider 行锁事务拒绝，避免相同名称绑定到不同 URL/凭据。

vLLM 的必要后修仅取标准端点、真实发现/流式和实例 URL 隔离，未复制返回 nil 的
NewInstance 或未实现成功桩；其他 provider 由本地 factory 构造独立实例。只补 vLLM
文本/历史/SSE/发现，不扩到其他本地 provider、多模态、embedding、rerank 或语音批次。
本地上游对照：93f3b9012 的动态 URL/标准端点及流式修复，94f82acd0 的全局污染修复；
未刷新冻结上限之外的历史。能力别名、状态值与既有 api_key 唯一索引限制见
[Go Provider API](../references/http_api_reference.md#go-provider-api并行实现)。

验证：Go 1.25.14，相关 CLI/entity/models/service/handler 的最终 race 回归为
245 个通过、8 个既有/新增 opt-in skip；随后补核 CLI 的空 URL/region 组合并复跑
CLI race 通过。其中本项实例 SQL/HTTP 与五组既有 Google、
模型系列、模型绑定、session SQL/HTTP 专项另启用真实 scratch，均零 skip 通过。
`go build ./internal/...`、`go vet ./internal/...`、三个独立 main 构建 exit 0。
并发模型声明只有一个 SQL 赢家、Extra 独立读回、跨租户/无认证拒绝、角色历史与
中途取消无成功终帧通过；所有自有 scratch 数据库和含凭据临时配置已移除。
本项独立工作树 `make integration` exit 0，750 项通过、1 项 Web checkout 缺失而 skip
（2577.65 秒）；显式指定 `WEB_DATASET_CHECKOUT` 后，该真实 Web 客户端/scratch HTTP
用例另跑 1 项通过、零 skip（46.68 秒），补齐缺失验收。两轮源码输入 hash 均无漂移，
记录在 `/tmp/multirag-provider-python-integration/`；未使用其他任务门禁作为本项结果。
自有临时 Infinity 容器和私有覆盖配置已清理。
合入 main 前加跑全 `internal/...` race：507 项通过、10 项未配置 opt-in skip；六组
相关 SQL/HTTP 专项另启用均零 skip，build、vet 与三个独立 main 构建 exit 0。
初跑暴露旧路由测试把模型声明路径当作聊天入口，以及存储测试缺少私有 MinIO 配置；
现核验该路径实际绑定 AddCustomModel、两个入口的无认证 401，以及旧聊天载荷拒绝、
零 provider 调用和独立 SQL 零声明。私有 MinIO 提供真实存储验证，测试桶、容器与配置已清理。
未改 Python；未执行 MySQL、生产认证、远程 Ark/vLLM 或 Web 页面验收。
