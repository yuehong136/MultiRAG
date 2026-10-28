# 67c3e73d 后二十项本地交付

冻结范围为排除 `67c3e73d70ffad2287bbdae3c9154c30a14c8c7e`，截至
`98f2a2e60b6a6da3906628a157c1751ee439e5ca` 的连续 20 个 first-parent 项；直接后修仅核至
`519e7d98a5651564d4e35d6648f006cba4baaf4f`。未 fetch，未开启下一批。后端基线
`005e79a631b8c1b7ed57d2fb090b47a5862fa2c7`，Web 基线
`e72d9730f17654b0b5ed6f6f4c011c940a35545e`。

业务代码及回归夹具已接收到后端 main `4785b0fb561aa522fc7050182ae4742fe9f9c65f`，与隔离
`4439e2c1` 同 tree；Go 集中组合和实际 Router 验证快照为 `4744e0dc403a5d35e48ebb4e9359d3178dfb0aae`。Web master 与
`.github/consumer-web.json` 均为 `fcf2a372ba7ab545626ec20b267740a71c051004`。Python/FastAPI/
SQLAlchemy 仍是生产后端；Go 是停滞并行实现，Web 默认 `/api`、`/v1` 仍进入 Python。

后续仅补本文档；最终文档提交、输入等价映射、资源读回与完成状态统一见本机
[交付回执][delivery]，不以中间快照代替最终 SHA。

## 逐项处置与质量比较

每条上游路径按主责仅计一次，共 20 项、133 条足迹；次级 Web 审查和本地补件不重复计数。
逐路径处置、冻结补丁 hash、当前本地路径/hash 和接受依据见 [完整足迹账本][footprints]。

| # | 上游 | 本地结果及质量依据 |
|---|---|---|
| 1 | `9a9d3ddf` | 未注册 TEI 默认只提供 Builtin/default/精确模型名的只读展示，model_id 为 null；已注册数值行优先，不产生可选择的虚构模型或实例。运行时仍走严格租户及默认解析。 |
| 2 | `2196f226` | 保留现有 preview 授权先于 I/O 的 SQL/provenance 链；真实 FastAPI、SQL、MinIO 与 Web 客户端覆盖外租户、失效成员、缺失对象及原始 bytes，比替换整模块的单元假件覆盖更完整。 |
| 3 | `d26d7994` | 同一权威服务检查已覆盖，不增加重复 accessible 查询，也不混淆 user 与 tenant 身份。 |
| 4 | `bbacb312` | 保留共享 think 流解析器，扩展尾部、分帧和最终回答回归；活动 Dialog/Agent 消费者继续共用，未复制局部状态机。 |
| 5 | `597ac1e9` | Search 缺失聊天模型时解析真实租户默认；非字符串保存引用拒绝，合法 min_tokens/max_tokens/权重 0 保留。实例校验和生命周期保持严格。 |
| 6 | `c70f19e1` | preview 继续只授权一次；独立 source provenance 校验仍负责存储地址边界，不随上游去重一起删除。 |
| 7 | `01a5598a` | Markdown naive 预表格处理与提取/自定义分隔符共用 fence scanner，保护长 fence、波浪线、CRLF 和未闭合 EOF；保留 fence 外表格语法。后修的普通非 fence 表格分隔符扩展不纳入本批。 |
| 8 | `5db1b296` | Docling 只接受实际可用 native 文本；空或坏形状从同一次响应回退，仍无正文则失败。保留请求兼容顺序、原生块边界及逐文档 fallback，补远端无页图裁剪保护。 |
| 9 | `c6eee09e` | Go stop 在全部 Doc→Task 锁及身份/运行状态重验后取消；SQL 失败只补偿本次 nonce，COMMIT 不确定性保留取消 fence。disabled 但仍运行的文档可停止，update_time 单调；不删除索引、对象或队列。 |
| 10 | `02d163a1` | Go Agent versions 按有效 owner/tenant、own/TEAM/release 授权，稳定排序与安全 summary 投影；保留测试，不照搬上游整片删除测试。实际 Router 的 JWT/API 鉴权另行验证。 |
| 11 | `b65b18ba` | 保留既有 provider/instance/model 生命周期和真实消费者合同；回归覆盖模型解析与 think 输出，未以宽松默认或数值 ID 占位替代能力校验。 |
| 12 | `23aae198` | Go runtime multipart 先整批预检，再按可信 actor namespace 分配对象与描述符，写后严格 HEAD/读回；失败补偿整批。真实 S3 构造配置进入 namespace resolver，畸形/裸 query 在存储前拒绝。Python public beta、URL/PDF 流程不由此替换。 |
| 13 | `04dc3bb1` | Web canonical Search ask 必传实际 search ID；保留 Python 路由和默认 8000，不切换到 Go 开发端口。 |
| 14 | `baeb0c04` | 65 条 provider 足迹逐核；Go 请求配置及 endpoint map 使用副本，DeepSeek/Gitee/SiliconFlow/Aliyun 保留严格有界 JSON/SSE、取消与错误合同及合法 0/false/[]。保留既有接口，避免无消费者的 BaseModel/receiver 形式重写。 |
| 15 | `423fb6fa` | 上游重复 ingest guard 已被后修撤销；保留本地较强的并发/取消/源版本协议。仅修测试夹具分别登记和清理 task-image Redis set，不将 set 交给 string GET。 |
| 16 | `6143205b` | Go version detail 绑定已授权 canvas 和准确 version，返回原 release/DSL 合同；错误 version、失效 owner/tenant 与越权读取在 SQL/存储副作用前拒绝。 |
| 17 | `e627f5d8` | Go related questions 绑定 saved Search、活动 owner/tenant 和数值模型；Python 实际 `/api/v1/chat/recommendation` 同步补活动 tenant 检查。零值、取消及安全错误保留。OpenAI-API-Compatible 的 Go 工厂仍仅 embedding。 |
| 18 | `461c190c` | Go 检索真实调用共享 metadata matcher，所有模式与基础 doc IDs 相交；[] 不拓宽，畸形/错误/取消不回退全库。semi_auto 生成条件同时校验字段和已选 operator；既有首 tenant index 限制仍在。 |
| 19 | `a78a3fdd` | Go GetByIDs 对 nil/[] 已有更强零 DB 行为；补真实 projection/context/disabled parent 合同，不机械加入多余 guard。构建继续保留 CGO 实际链接的 `.a`。 |
| 20 | `98f2a2e6` | Azure Blob 适配 Python 同步、身份与 checkpoint 链；显式三认证模式、保存零 SDK I/O、官方 HTTPS endpoint/无 redirect、完整分页、If-Match 与账户/容器范围 ETag 指纹。拒绝 dot segment；取消排空并关闭客户端。Web 表单只序列化当前模式，不启用 prune 或测试连接。见 [Azure 配置](../../common/data_source/azure_blob.md)。 |

## 串行接受映射

I 独占主干 index、router/cmd/shared wiring、依赖、consumer pin 和文档；其余 owner 在隔离树提交。
下表为主要映射，补件与逐路径 blob 对照完整保存在 [接受记录][acceptance]。

| owner | 最终候选 | 主干接受 |
|---|---|---|
| D | `02683d3f` | `111989ff` |
| G | `2747b2e6` | `ad03c6b1`；I wiring `217ee556` |
| P | `95ed31f8` | `9ad69c3d` |
| M | `f58a925a` | `13989ef3` |
| F | `af85b52d` | `f261b270` |
| A | `e9a8c8ca` + `2d7d395d` | `7ee15605` + `9c5f8b7e`；I actual-route fixture `bbaf635f`、`6422a34b` |
| C | `e6ca5488`（含三笔补件） | `17d24fb7` → `318a0c64` → `da49c6f4` → `ab168e51` |
| S | `41566bf5`（含 Go/Python 复核修复） | `32248b66` → `5ffd2ebc` → `7931b745` → `3cc6d2ff`；I wiring `b920cccb` |
| W | `29550154` | Web `fcf2a372`，与候选同 tree |
| I | Azure 直接依赖、未知 region 拒绝、共享验证和 pin | `85517ee6`、`59317c29`、`ac88572e`、`6422a34b`、`4785b0fb` 等，详见接受记录 |

## 分层验证

业务执行经当前 registered runner，借用已有 venv、模型/native 和 node_modules；无干净重装或模型重下载声明。
原命令、原始退出码、JUnit、源码/锁/配置/后端及固定 Web 输入分别封存；候选结果不累计冒充当前组合结果。

| 层 | 实际结果与边界 |
|---|---|
| Python 静态 | 32 个改动 Python 文件 Ruff/format、mypy 156 文件、8 个 import 合同、async DB 检查 exit 0；同次增加 Agent router test 的 Go vet 通过。run `854d06b626ea4d89be1c3f5b3c53f166`。 |
| Python unit | 当前组合定向 618 passed，0 skip；run `b503b6ea5a6f4b1ab8b5fb2192333b3f`。JUnit 为 `integration/I/python-final-unit-freeze.xml`；生产输入与最终组合逐字节对照，非消费的 fixture 路径修正单列，见交付回执。 |
| Python integration | provider/Azure/preview 47 passed，0 skip，含 5 个固定 Web 消费用例；run `e50f3deeb22848e8b89ccab5653fa205`。真实 TCP PostgreSQL/MinIO 与受控 provider/SDK 边界分别留证。报告/JUnit 为 `integration/I/python-final-integration-freeze-report/`，冻结输入与当前 SHA 的映射见交付回执。 |
| Python ingest | 当前组合 28 passed，0 skip；run `07b90eb1129646a385329b57859af529`，真实 PostgreSQL/Redis/MinIO/Milvus 在应用收集前准备。复用 F 原字节适配器/观察器，hash 封存在 `integration/I/final-replay-inputs.json`；JUnit 为 `integration/I/F-replay/ingest.xml`。 |
| Go 最终集中组合 | 在 `4744e0dc`：internal build/vet、server/admin/CLI 三次独立链接 exit 0；定向 race 79 顶层、323 含子例全部通过，0 skip，源码前后无漂移。run `fbfc1109b7214750b8e6c04ed966e9d6`，见 [Go 组合结果][go-final]。 |
| Go 实际 Router 鉴权 | stop 使用实际 Router/JWT/API、SQL/Redis；Agent 在同一 `4744e0dc` 使用实际 Router.Setup、签名 JWT/API、SQL/MinIO，1 顶层/10 鉴权子例全部通过、0 skip，758 输入前后相同。list/upload 六身份正向，detail 四身份正向及业务拒绝分别留证。run `909b7eb852824f03acf307155b0c49cc`，同字节 fixture 已持久化，见 [Agent 最终路由][agent-router]。 |
| 固定 Web→Python | `fcf2a372` 的实际 Search/Azure Node 客户端经真实 JWT/HTTP/SQL：1 passed，0 skip；覆盖默认与数值模型、三模式 create/update、禁止 prune 和外租户拒绝的逐次提交后独立读回。生成/检索受控，不等同真实模型或 Azure。首次通过 `030a84e9a154415dbecd2bf6a35259b3`；将同字节 TS 脚本入库为默认路径后，最小复跑 `41210c8cf8de43d5b1137cd442c0141b` 再次 1 passed/0 skip，见 [consumer JUnit][web-consumer]。 |
| Web 候选证据复用 | W stable test:ci 为 Node 990、Vitest 1104、desktop 93、tooling 21，0 skip；build 与 24 focused 通过，固定 production UI 的 EN/ZH×light/dark contact sheet 已审。2197 源码输入与最终候选相符，同 tree 接收到 `fcf2a372`；I 另在 master 跑 i18n/size exit 0，不宣称重跑整个 test:ci。 |

候选验收仍单列保留：P 181 unit+5 HTTP/SQL；D 77 focused+25 consumer regression+9 受控 Docling HTTP；
F 147 unit、17 preview（含 5 固定 Web）及 28 ingest/5 受影响夹具参数；M 115 race 含子例；
C 88 unit+2 真实 SDK transport/HTTP/SQL/MinIO。各自冻结输入、复用条件和范围见 owner handoff，
不与最终组合重复求和。P 全源码清单的一项差异为未被该 integration 消费的 unit KG fixture，另有输入差异记录。

native 的 133 个输入及借用 archive hash 已核，当前 Go 入口重新链接；未重建 native。
`uv lock --check --offline` exit 0，Azure SDK 仍是锁定的 12.22；CI 配置提交不代表远程工作流已运行。
基线已有 14 个 `DocumentDeleteLive` 未列入默认 Go skip 分类，属本批之前的 CI 分类缺口，未通过增加豁免换绿。
本批显式 live 用例按 `scripts/ci_checks.py` 分类并以 REQUIRE_SERVICES=1 实际运行，不能据此声称默认完整 Go/远程 CI 全绿。

## 原始失败与历史协议边界

I 首个 Go run 因 owned cache 缺锁定 x/image v0.38.0 失败，只补齐自有 cache 后复跑；
Web→Python 首次因 fixture 未初始化 FastAPI 惰性 middleware stack 失败，修夹具后最小复跑通过。
A 的真实构造配置和 query 边界、S 的活动 owner/tenant 与 operator 约束、C 的 dot segment 及跨容器同 ETag
反例均先保留失败，再修根因。原始输出和最小复跑未被覆盖。

历史登记不能由最终清理倒补合规：D/F/C/S 有事前登记命令失败后创建 tree 的窗口；F 首轮 observer
输入变更及 Redis set 遗留、G 接受窗口前提前清 candidate、C 未预登记 tokenizer cache 创建均有记录。
C 旧 cache 的原下载/生成路径未被观测，不能声明其历史零下载。原文件归档后精确移除；I 后续 runtime
资产在导入前按登记复制。manifest 审计保留 12 个旧版缺失原始快照（D 4、A 2、F 1、S 4、W 1），
不凭当前 JSON 重建旧字节。见 [manifest 审计][manifest-audit]。
因此 `strictProtocolPassed=false`，`historicalRegistrationConformance=false`；实际行为通过、当前 absence 与历史协议合规分别报告。

## 恢复、清理与原 WIP

九个 owner 的 tree/ref/admin 已由 I 独立确认 absent，精确身份及 recovery bundle hash 见
`integration/I/final-cleanup-owner-audit.json`；A detached 验证树按与历史主干完整 tree 等价接收。
I 的 delta bundle 在仅取冻结 base 的独立对象库中验证，fetch 前 candidate 不存在、无 alternates；
恢复 tree 与逐路径 hash、独立 fsck、恢复目录清理见 `integration/I/I-final-recovery.json`。
短 runner root 的原始输出、journal、case 目录和空目录先逐字节归档再删除；runtime 日志另行归档。
精确 SQL/Redis/bucket/index/listener、进程组、缓存/native/runtime/树/ref/admin 最终读回及借用输入 hash
由 [交付回执][delivery] 汇总，历史缺口保持为 false。

原后端 28 项 WIP 不纳入验收或暂存；27 项按原文件 hash 验证，PROGRESS 仅剔除本批顶部索引后
与初始副本逐字节对照，原中部 WIP 保留。候选接受、恢复和清理不使用 reset/stash/broad clean。

这是本地定向交付；未验证全量 unit/core、真实 Azure/付费模型/远端 Docling、常驻 worker、干净 native
重建、部署或生产。metadata 既有首 tenant index 限制仍在。未 push、未部署，远程 ci-required 待运行；
固定 Web 提交发布后，远程 consumer 才能 checkout，不能用旧 pin 或不同 SHA 的结果替代。

[footprints]: /Users/xldu/.local/state/multirag/validation/20261029-batch-after-67c3e73d/integration/full-footprints.json
[acceptance]: /Users/xldu/.local/state/multirag/validation/20261029-batch-after-67c3e73d/integration/acceptance.json
[go-final]: /Users/xldu/.local/state/multirag/validation/20261029-batch-after-67c3e73d/integration/I/go-final-result.json
[agent-router]: /Users/xldu/.local/state/multirag/validation/20261029-batch-after-67c3e73d/handoffs/A-router-auth.json
[web-consumer]: /Users/xldu/.local/state/multirag/validation/20261029-batch-after-67c3e73d/integration/I/web-consumer-durable-freeze-report/junit.xml
[manifest-audit]: /Users/xldu/.local/state/multirag/validation/20261029-batch-after-67c3e73d/integration/I/manifest-history-audit.json
[delivery]: /Users/xldu/.local/state/multirag/validation/20261029-batch-after-67c3e73d/delivery-complete.json
