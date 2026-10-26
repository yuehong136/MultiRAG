# 连续批次 after-b7d88f0b

冻结范围排除 `b7d88f0b0904e427b5e31cdf0dabbae0f8cd7279`，至
`53bb2bd9e8ad52acf04833136dac6f648174e35d`，共 20 个 first-parent 提交；
直接后修上限为 `519e7d98a5651564d4e35d6648f006cba4baaf4f`。
业务源码组合为 `26977a0a5fa0da2461009d3ee5c06b0cdd0c605e`；独立 Web master
与后端 consumer pin 均为 `c46150ec3f873e5d4ef89c5395e83c3d1624fb8f`。
随后 `f1b574597966b5024e51e43070ebd869ccb4eb75` 仅修两处 Go 验收测试的旧假设，
产品源码不变；其后收口仅修改文档。Python 是生产后端；Go 为独立并行实现，证据分别记录。

## 逐项处置

| 项 / 上游 | 本地行为与质量取舍 | 实现或保留依据 |
|---|---|---|
| #1 `98bc9ca6` | Teams 接入现有同步 worker；读取分页、旧父消息的新回复，检查最后响应后的停止边界。权限错误不吞掉；来源可见性不构成权威删除清单。Web 使用真实配置与已保存连接测试接口。 | `e5911e1b`；Web `513d6f85`、后续补修 |
| #2 `f80ec17f` | 保留已有六类 Go connector 生命周期及更强的成员/KB 授权。拒绝移植“凭据非空即成功”的连接测试 stub；Go 尚无等价真实探测。Python 继续实际探测路径。 | `7c3e248e`；[Go connector](go-connectors.md) |
| #3 `0a7662cf` | Go Agent 列表严格分页与筛选、显式空 owner 集合、稳定 ID 次序和正确 total；只返回已授权资源。 | `7c3e248e` |
| #4 `7fc909ac` | 现有 ModelScope 共享 transport 已避免不安全断言；保留自定义 RoundTripper 和池配置，以实际请求、代理、取消及实例隔离补证，不丢弃 wrapper。 | `65654fa3` |
| #5 `e023c165` | Go metadata 更新在同一事务中验证有效成员与 private/team KB 可见性，保留空 metadata、false 和无关配置；拒绝后独立 SQL 无写入。 | `7c3e248e` |
| #6 `58eb957c` | JieKouAI 规范化 key/model/query 副本，方法决定 stream wire 模式；保留严格向量/分数/SSE 原始响应校验、取消和 idle deadline。 | `65654fa3` |
| #7 `834236a3` | Go system status 返回实际依赖探测与有界 worker heartbeat，遥测错误显式显示，不用配置存在代替健康。 | `7c3e248e` |
| #8 `550bdf21` | Go 邀请、接受、移除在 owner 锁和事务中完成；invite 关系可显示但不能用于资源鉴权。Chat/Search/Chunk 使用 active 关系；owner 不可移除。 | `7c3e248e` |
| #9 `ea3a5dba` | Go 自定义模型在 DB 前严格校验名称、能力、token 和实例路径，保持结构化凭据与多能力。 | `82900a44` |
| #10 `b79f79d9` | Novita 保留现有安全共享 transport；实际 custom/default/proxy 请求与实例、取消、期限合同覆盖上游目标。 | `65654fa3` |
| #11 `dc4b8252` | 完整核对 148 路径，采用 provider/instance/model 三表及 OCR 默认值；保留既有整数模型身份与计量。迁移、回填、状态、默认值、删除引用、凭据隔离和活动 Python/Web 消费者均适配；无活动消费者的 group 两表不移植。 | `8cd1b015`；[模型合同](../../api/db/services/MODEL_PROVIDERS.md) |
| #12 `bd6251f4` | canonical OpenAI completion 默认非流式，显式 stream 保持。无实际调用者的 `/chats_openai/` 旧别名删除，并验证 404、OpenAPI 及零业务副作用。 | `6386181f`、`8cd1b015`；Web 删除旧规范路径 |
| #13 `bda2117a` | OneDrive 显式 drive_ids；`[]` 为零来源，测试按钮明确拒绝空 scope，不能伪成功。递归/分页、同名跨 drive 身份、最后 delta 生效、真实零字节内容和 folder/cursor 绑定；worker 沿现有时间检查点，不宣称持久化跨任务 opaque delta。 | `e5911e1b`；Web `513d6f85`、后续补修 |
| #14 `658ff06c` | Python 与 Go 各自补四个 SiliconFlow Pro 模型，能力以各自目录为准；Web 读取后端目录，不维护第三份静态模型清单。并修复旧目录中 reranker/rerank 类型不一致。 | `51e49384` |
| #15 `09e91a8e` | Go 注册原子初始化 user/tenant/owner/root/default；实例身份改为 provider+instance_name，先建立新唯一约束再移除全局 key 唯一性，不合并旧数据。catalog 零/省略 token 不生成禁用 override；显式自定义声明按本批统一合同要求字段存在且非负，缺失/null/非法数值拒绝。 | `7c3e248e`、`9a76ecaa`、`82900a44`；组合补证 `f1b57459` |
| #16 `faa9c546` | Go MCP 删除仅移除自有 SQL 配置；保存的 Canvas/version 引用和 SQL 失败阻止删除，远端 MCP 数据与桶不归配置所有。 | `7c3e248e` |
| #17 `d2f0a18f` | logout 将当前用户 access_token invalidation 写入并提交，失败回滚；JWT 在 logout 后至重新登录前被拒绝，不宣称旧 JWT 永久撤销，API key 不受影响。 | `8cd1b015` |
| #18 `d766e491` | Go stats 绑定租户/Agent 来源和日期，configs 输出安全白名单；log 更新实际共享 logger，保留真实 CLI 使用的路径和 token 选择。 | `7c3e248e` |
| #19 `2d229dd8` | Go 空 region 使用已配置实例 endpoint；同 raw model 的两实例保留各自 key/base/extra，真实受控 HTTP 检查最终请求。 | `82900a44` |
| #20 `53bb2bd9` | 现有 metadata matcher 已使用 None 初态，AND 空交集不会被后续条件重置；保留生产实现，新增真实 DocMetadataService/apply/Dealer 消费链与显式空 ID 合同回归。 | `776bc134` |

## 组合验证

本机批次目录为 `20261027-batch-after-b7d88f0b`。`scope.json` 保存完整 SHA、patch hash
和冻结上限；`integration/accepted-*.json`、原始 journal/output/JUnit 与
`delivery-complete.json` 绑定执行输入、实际 main/Web/pin、恢复包及清理读回。
候选通过数、当前组合、真实外部服务、远程 CI 和生产部署是独立证据层。

- 当前 Python 改动 65 文件 Ruff lint/format 通过（`dc1adc6402b94f1ea0ce5fc0f87d169d`）；
  模型、连接器、SSE 和 metadata 17 文件合并定向单元 352 项通过
  （`4d03776832154af29227f5612fd837e9`），零 skip。
- 当前模型生命周期与 Teams/OneDrive API/worker 三文件 38 项真实 PostgreSQL、
  MinIO、Redis、Milvus 定向集成通过（`42a24aff7bed41a49945c2b39e6c2346`），零 skip。
  模型请求使用受控端点；供应商读边界受控，持久化与独立读回真实。
- 两个固定 Web 实际 domain client 用例分别运行 owner/foreign/cleanup 三阶段，使用完整
  Python 应用（fixture lifespan 关闭）、实际 JWT 和隔离 SQL/对象/队列/索引资源。
  模型测试验证双实例、同 raw name、数值 ID、凭据 envelope、合法零、默认值与外租户五项
  精确 404；Microsoft 测试验证真实 form-values/client 保存、空 scope、探测、未知字段、
  合法零调度与外租户八项精确业务 109，并独立核对无 SQL/object/Graph/auth 副作用。
  模型独立 raw0 为 `68cc6c28dd9341ffb2ac56ba69a6ce77`，Microsoft 为
  `90da882136ec43e9ae57f57d605c69c8`；六条嵌套 raw0 与重定位后的实际日志路径见
  `integration/accepted-consumers-final.json`。
- Go 当前组合 build/vet 和三个 cmd fresh link 逐命令 raw0；native preparation 实际核对
  135 源文件、24 字典、工具链与归档，复用只到 native 准备，不复用旧程序。
  build/vet/三个链接在 `e19aaeb06f914b4181ec64e96acbbad5` 中各自 raw0，该外层运行
  后续因测试失败而非整体通过。最终相关 race/JWT/SQL/CLI 外层
  `130b30890eff40118984272ea29079b4` raw0，85 个含父子记录、零 skip；
  `2e67ff34ee094168a11d2dcec213ab3d` 另补当前 custom-extra 严格解码 race 一项。
  新旧 catalog/custom 零值均有实际 SQL 和受控 multipart 请求，不推导 chat 生成预算语义。
- Web 旧完整 CI 1,948 项仅对应其原执行输入；后续改动分别有定向用例、静态检查、build
  和受控浏览器收据。I/C 以带文件名、路由、视口的 contact sheet 审阅 en/zh、light/dark
  截图。真实 domain client/HTTP 验收不等同浏览器交互，受控浏览器也不等同真实 Graph 租户。

类型与 async DB 门禁按改动范围复用候选的原始通过证据，后续变动补定向验证；不把早期
174 单元、18 SQL 或旧 Web 1,948 项冒充最终源码一次全量执行。
当前纳管的 constants/api-utils/tenant-utils 三文件 mypy（cache=/dev/null）
`bbd4583448424bf7b2a8fc4613af5e21` 和 async DB 门禁
`ffebf5bfcc254c029f4d5bda248383cb` 均 raw0。

## 首错、运行边界与清理

首错和取消记录保留。I 首次未显式开启 external consumer 导致 2 deselected/exit5；
随后模型单例通过但 Microsoft 误将每次 Graph 读取的 token cache API 调用数按实例计数而失败，
按两页 Teams 与两个 drive 的实际机制修正，定向复跑。Go 首轮在 Docker 建库、TCP 却连接
同端口 native PostgreSQL；库在原实例清除，harness 改为同一 TCP 端点创建、执行与独立读回。
这些失败结果不充当最终通过依据；通过的同输入 build/vet/link 子命令保留其明确范围。
同端点复跑又暴露 CLI 测试将空数据库密码作为 substring 恒真误报；只为非空密码执行
泄露匹配，CLI 非零退出与非空泄露仍失败。组合时 G 的旧 custom-positive 断言与 M 已明确的
present/nonnegative 合同冲突；保留合法零，补实际 AddCustomModel、独立 numeric-zero SQL、
resolver/受控 ASR 和负值零副作用，以及缺失/null/负数/小数/bool 拒绝矩阵。
这属于本地统一合同选择，上游 #9 本身没有要求零值；不靠删除断言关闭组合失败。

严格历史登记合同仍为 false：A 早期 Ruff cache、I 初始 Git admin 路径假设、I 静态命令
误建空 uv 环境、M 未事前登记的 `.pytest_cache`，以及 W 两次仅拼贴命令借用解释器清单不完整，均保留原记录和精确清理，
后续修正不倒填成事前登记。所有测试、import 和编译仍从仓库 canonical runner 进入；
本机命令组成脚本不替代该入口。

Teams 后页失败可能保留较早已提交批次，但不得提交成功 checkpoint 或执行删除同步；
不宣称任务级全部回滚。HTTP/auth/body deadline 在受控边界有证据，未证明真实 DNS/TLS
与厂商所有阶段均可中断。实际 Microsoft 权限/供应商资格、常驻 worker、Linux native、
生产迁移和部署未验证。Go 全应用 MySQL 仍受既有 jsonb entity tags 限制；独立 MySQL
identity migration/Stats 证据不代表整个应用启动通过，也不推导一般 DDL 可事务回滚。
Go whole-provider 删除仍有旧 child/orphan 风险，未借用 Python 生命周期证据关闭。

I 独立检查 167 个精确临时数据库名（同时核对 native TCP 与 Docker 实例）、80 个桶、
80 个队列、160 个集合、385 个监听地址、253 个进程组与 251 个 runner 自有目录均无残留
（`f2dcf55037d44de9a7ab0d09796ac4b3`）；包含创建前即失败的预留身份。
后续仅元数据检查和拼贴命令的进程/目录也按各自原始 finished 收据独立核对。
本批运行资源、私有配置/模型/native 副本和普通候选 worktree/ref 精确退休，独立缺席读回与
可恢复 bundle 见最终外部报告；借用服务、缓存原件、历史 managed 树和 SDK 保留。
原始 28 项 WIP 不入本批提交，PROGRESS 仅暂存本批新增块。没有 push 或部署，
远程 `ci-required` 待运行，未发布的 Web pin 远程不可用；不继续下一批或创建自动化。
