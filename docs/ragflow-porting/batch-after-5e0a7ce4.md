# 5e0a7ce4 后二十项本地集成

冻结排除 `5e0a7ce408c1fd4b85d8734b7f3b3da27ec4f6ab`，first-parent 连续 20 项至
`7dd40309863ba8c0f9654e4f9f32a2f51f49d421`；直接后修参考上限
`519e7d98a5651564d4e35d6648f006cba4baaf4f`。未 fetch、未采用更晚上游 checkout。
20 个编号、101 条逐提交路径逐条审结；等价或不适用部分保留质量比较，不制造重复代码。

本批可执行后端组合为 `c069868fb6e7b488b8f3f0b970aaa9fe6a1f3af2`，Web master/consumer pin 为
`8eb6e2019fe06413b0b75172d9c3c2a8b0e78d9e`；文档提交只改变说明。后端独立 provider 修复 `091c1565` 与 Web 独立
模型身份提交 `b07b4652`、`cbcd1351`、`da33454e` 保留在实际组合，不计作本批成果。
验证使用登记的干净已提交 checkout；主树原 28 项 WIP 未作为覆盖或测试输入。

## 逐项处置

| # | 上游 | 处置与质量依据 |
|---|---|---|
| 1 | `e81bca73` | 兼容有效 primitive chunk ID，保留数字 0、深拷贝、去 prompt 与稀疏引用原始 citation_id；不把无效布尔/容器当引用。 |
| 2 | `9c32b73c` | 保留按消息携带 reference 的共享 Widget renderer，明确 citation_id 查找不落回错误数组位置；实际 UI 与历史消费者分层验证。 |
| 3 | `8f4809d1` | Go Searchbot 新入口复用现有授权检索；严格 string/list kb_id、精确整数、saved 配置、空集合与取消；保留活动 datasets/search。 |
| 4 | `e2b0da9e` | 保留本地 bool 双路候选融合。真实 OpenSearch 比较表明 normalization 的分页深度、rank feature 与成本不适合直接替换现有合同；不新增无收益 pipeline 配置。 |
| 5 | `67ce0c89` | Go 会话 list/detail/delete 接线，source+agent+session 绑定、稳定分页、日期、活动 owner/TEAM 权限与独立 SQL；不包含 Go create/run。 |
| 6 | `653d4bdb` | 当前 Python CLI 全分页读取 chat/session，解析轮询保留目标完整性并报告失败。真实 CLI 105 项跨页与精确删除经 HTTP/SQL 验证；不冒称实际 Infinity p3。 |
| 7 | `4bbd5982` | 新增 OpenRouter/compatible/VLLM/LM-Studio metadata；严格凭据和完整 inventory；raw OpenRouter key 统一到 chat/vision 运行 transport，不改变存储输入。 |
| 8 | `144abbe2` | Web 统一 typed 字段/候选/动作，保留原子创建、同 candidate 验证、密钥生命周期与多能力；修复 native dialog 内 Select Portal 的真实交互。 |
| 9 | `c8c890b0` | 保留已有更强思考流状态机与及时 reasoning 输出；补实际 async_ask 的 DeepSeek/MiniMax 消费者。 |
| 10 | `04209ffc` | Go 授权范围内批量 hydration，显式失败 fallback、维度/有限数值/取消校验；尚无 Go ask/citation insertion 活动入口。 |
| 11 | `2bd89006` | 有界可搜索、跨页模型选择与明确 100 项创建上限；只启用真实 discovery 能力，保留已实现 Fish/MinerU。 |
| 12 | `68b93605` | paragraph 按空白消费已有词项，避免逐字符切词；覆盖真实 tag_content 调用。 |
| 13 | `14c460a5` | Excel HTML 分块使用正确上界并去除样式空尾行，保留内部空行、0/False 与实际 naive/one 消费者。 |
| 14 | `453ade28` | 保留已正确保存的 OpenSearch _source.id 和深拷贝，补真实 metadata 生命周期与回归。 |
| 15 | `86b320e7` | provider 按能力去重计数，与本地多标签 AND 和搜索语义一致，中英可访问文案同步。 |
| 16 | `8abe627e` | MSSQL 注册/配置/worker/cursor 状态链贯通，默认 1433；要求显式单 SELECT 与只读主体。上游旧 Italian 键空间不适用本地分域资源，未注册不完整语言。 |
| 17 | `17f27b9d` | 保留 Browser resolved typed input 日志；补 DeepSeek 默认 URL，真实 SDK 构造验证默认/显式优先和 schema 设置，第三方浏览行为受控。 |
| 18 | `2c64febc` | Xinference/LocalAI 真实 metadata 协议与能力映射；不将 audio-only 误宣告 chat；保留 Baidu 现行静态目录，不引入无运行基础的 SDK embedding/Tencent 占位。 |
| 19 | `d9a04ef7` | 保留更强 typed raw metadata carrier/所有权写回；补 auto/default 的真实 build→SQL commit→独立读回及失败回滚。 |
| 20 | `7dd40309` | 缺名/空扩展 Pipeline 安全读取，ICO 与明确不支持的 TIFF 分开处理；保留安全 SVG 与受保护资源生命周期。 |

## 当前合同与验收边界

Python 是生产后端，Go 是停滞的并行实现。Go 新 Searchbot 路由没有活动 Web 调用；Web
知识检索仍用 `/datasets/search`。Go Agent 会话通过生产 Router 的实际 JWT/API key/SQL
验证；Web 历史消费者实际连 Python，删除结果为 Python 的 1，不混作 Go 的 true。

远端完整目录上限 2,000、单响应 1 MiB；API 每页和单次原子注册仍为 100。
Ollama/Fish/LocalAI 多请求 inventory 保留各自更小的 I/O 上限，超限显式失败，不截断成功。
MSSQL 配置 CRUD 的真实 Web/SQL 验证不等于真实 SQL Server/TDS 验收；只读完整快照
不能由权限过滤后的 INFORMATION_SCHEMA 自动推断，因此本批要求显式 query。

最终定向组合实际通过：

- Python：19 个单测文件 810 passed；PostgreSQL/真实 Web 消费者 13 passed，API/CLI/存储 6 passed，均零 skip。最终 Web Select 修复后又按 c069868f/8eb6e201 实际复跑六 provider、Agent history、MSSQL 的 8 项消费者，全部通过。40 个改动 Python 文件 lint/format、定向 mypy、import-linter、async DB 与锁文件检查通过；复用其他 2,227 个未变 source hash 的证据。
- Go 并行实现：build/vet、三个入口实际 native 链接与相关 race 85 项（含子测试）通过；真实 PG/ES 的 Searchbot 与 Agent 合同另跑，Agent 最终通过实际 Production Router 的 JWT/API-key/SQL 请求。生产 Go 源码及 native/lock 输入未变时复用既有报告，后续测试接线变更定向复验。
- 独立 Web：8eb6e201 上原 `test:ci` 入口实际 2,304 passed，零 skip；包含 Node 1,005、Vitest 1,185、Desktop 93、tooling 21，14 项静态、类型、build、bundle 与测试门禁全部通过。原生 Chrome 的 Bedrock 鼠标/键盘、Paddle 中英文/明暗七项交互在 W 7b307f35 上通过；两处 Select 源码与最终 Web 逐 blob 相同。

S 的 Browser 日志沿用既有 `fetchTrace` → `JsonViewer item.inputs` 合同；本批只验证后端 typed log、真实 SDK 构造与 reasoning 消费，未执行实际 Web trace 场景。测试使用 PostgreSQL 17.11、适用的 Elasticsearch/OpenSearch/Milvus/MinIO/Redis 本地服务；供应商 HTTP 是受控边界。初次失败和修复后的通过分别保留，不计作同一轮全绿。

原始 exit/JUnit、source/config/lock/consumer hash、逐 101 路径质量结论、首败、取消、
失败恢复、原始 manifest 版本、候选到实际 main/master 映射及 base-only/no-alternates/
先缺失 candidate 的恢复与精确清理，集中保存在外部
`20261029-batch-after-5e0a7ce4/{item-ledger,validation,acceptance,delivery-complete}.json`
和各 owner handoff。A/I 初期派生 cache 登记偏差、Q 历史 manifest 缺口/输入漂移及
其他首败永久保留，`strictProtocolPassed=false`，不回填绿色或重建已删除凭据 payload。

未 push、未部署，远程 `ci-required` 待运行。没有真实付费供应商、SQL Server、生产、
常驻 worker、完整后端全套或全新依赖重装验收；受控 HTTP/SDK、真实本地服务与实际
外部供应商分别记证。原 28 项 WIP、借用服务/模型/环境/node_modules 保留，不开启下一批。
