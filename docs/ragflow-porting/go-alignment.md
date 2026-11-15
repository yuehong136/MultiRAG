# Go 结构与合同收敛

冻结目标为 `fcebcebe1e0e798b42c18e5593747cd20eb4c757`，专项起始版本为
`1f154bea3e60b378caecb3b2fdf9e1e672ca7f3d`。函数职责、文件、目录、依赖、配置、
默认值和删除项一起收敛。Go 尚未部署，本次删除旧 Go 门面和别名，并迁移源码消费者。
生产后端继续使用 Python；本专项不切换生产队列或共享数据。

## 当前实现

| 范围 | 当前合同与入口 |
| --- | --- |
| 配置与数据库 | 配置统一使用本仓 `configs/`：Go 模型在 `configs/models/` 与 `configs/all_models.json`，公共映射复用已有文件；仅 Go 的服务配置和私钥放在 `configs/go/`。Go 使用 MySQL，Python 配置与数据保持现有合同；删除旧 Go PostgreSQL 路径及私有实例绑定包装。 |
| 模型 | 原 provider 文件、构造器和 `ModelDriver` 统一使用 messages、typed inventory 与原媒体接口；service、handler、CLI 直接调用对应原函数。旧单消息/channel 入口、numeric TenantLLM 绑定、配置投影和自适应 URL 别名已移除。 |
| Agent | 使用原 canvas、组件、版本和 session 路径；CRUD、DSL 和执行入口分开验证。未实现的执行组件明确返回错误，不以日志、占位答案或空结果表示成功。 |
| 检索与存储 | 使用原 Elasticsearch、Infinity、NATS、Redis 和对象存储接口；dataset/tenant 范围进入实际查询和写入。保留原过滤、结果解析和存储生命周期路线。 |
| 解析与 ingestion | Office 使用 native binding；单独的 `cmd/ingestor.go` 消费 NATS。operator 按声明顺序执行，重复 stage 保留。未实现的文档处理明确写入失败状态，提交失败不 ACK。PDF 能力仍按原入口明确报未实现。 |
| API、admin、CLI | 路由按原注册函数装配，使用原 MySQL schema、鉴权和请求结构；CLI 使用 `internal/cli/` 及其 `filesystem/`，分别构建四个 main。原目标仍存在的 `/v1` 路由属于当前合同。 |
| 构建与 CI | 默认准备对应源码的 native 库、Office oxide、词典与锁定模块；服务测试不接受 opt-in skip。CI 要求实际 SQL/NATS/ES/Infinity/Redis/MinIO、HTTP、CLI 和 native 用例通过。 |

项目名映射为 `ragflow/` → `multirag/`、小写项目/命令前缀和 CLI 文件名。
`RAGFlowClient` 等原类型与函数名保留。除测试登记设施外，生产 Go 文件可按原路径直接定位。
具体问题修复和提前采用的后续改进分别记在[例外记录](go-alignment-exceptions.md)。

## 本仓配置路径与依赖版本

按用户本次指令，冻结上游 `conf/` 只作仓路径映射，不保留第二个顶层配置目录。
原 Go 专用 `models/` 和 `all_models.json` 继续位于本仓已有 `configs/` 路径；公共 JSON
复用当前文件并保留其原字节，其中 Go 原 `mapping.json` 对应已有 `configs/es_mapping.json`。
未被 Go 读取的 `llm_factories.json`、`system_settings.json`、`os_mapping.json` 不再复制副本。
Go 与 Python 的服务配置结构不同，前者单独放在 `configs/go/service_conf.yaml`，
Go RSA 默认路径随其放在 `configs/go/`，不覆盖 Python 的服务配置，也不增加旧路径 fallback。
所有 Go 读取位置、CLI、测试夹具和对应目录测试直接迁移，原函数和文件保持对应。

Go 模块版本保持冻结目标。相对主仓合并前版本有 23 项下降：22 项精确采用冻结 `go.mod`；
`x/image v0.25.0` 来自冻结 `go.sum` 和 Excelize 的传递依赖，仅额外显式补入间接 require。
`go.sum` 与冻结目标逐字节相同。全部降级、升级及原生工具链关系见
[依赖版本对照](go-dependencies.md)。

## 当前分支合并与验收

2026-11-15 已将 `5097f2897cfa972ca8b80901acd86f485109f93a` 合入当前分支
`codex/memory-background-recovery`，合并提交为
`3649f88e305f4b579490fa0c862c5e0cc2ef54a7`。第一父提交为同时推进的记忆任务
`07f8ae58c48dc83c5d2c1b83badfbd0c53db242a`；其新增提交完整保留。
最初 56 项 WIP 先行快照，其中部分随后由原任务提交；实际合并瞬间剩余 29 项 WIP
无同路径覆盖，合并后逐字节读回一致，未加入本次提交，索引保持清洁。

本次证据根为
`/Users/xldu/.local/state/multirag/validation/20261115-go-alignment-merge-42a6627fb9/`。
以下检查在合并后的实际主工作树执行，配置路径为当前 `configs/` 布局。

| 检查 | 实际结果 |
| --- | --- |
| 生产包、四入口 build 与带 integration 的完整 vet | `961a38751e294072a87e58f20c6525cb`，raw 0；原生 C++ 源与已验证归档逐文件一致，使用对应归档，主仓旧构建归档另行备份。 |
| 单次完整 Go race | `bcc35126ec9c4028a85bcf33e2453afa`，raw 0；执行 `go test -tags=integration -p 1 -race -count=1 -timeout 600s -json ./internal/...`。35 个有测试的包、2,278 个顶层用例、7,258 个通过事件，0 失败、0 测试跳过；1,230 项输入无漂移。 |
| 51 项 CI 必需消费者检查 | `f2d75b7ce5ca458194ebd319848d7270`，raw 0；使用本次单次完整输出，无包级拼接。 |
| 当前二进制、配置默认路径与真实 CLI | `632db2d8807040e7938227089736b888`，raw 0；admin/API/ingestor 冷启动、MySQL 独立读回、鉴权、CLI、停止及端口重新绑定；临时配置与 RSA 放在独有的 `configs/go/` 下。六种 Office 的真实 CLI 由完整 race 覆盖。 |
| Python 相关检查 | `c5db6d78910e49eab35f3effe08d5036`：96 通过、1 失败。该 SiliconFlow 静态目录断言在准确合并前提交 `07f8ae58` 的原测试与两份原数据上复现：`78a0a71feac64ae8b1b43f2e620e3d91`，raw 1；Python 目录缺少 `Pro/deepseek-ai/DeepSeek-V4-Pro`，原断言保留，未修无关数据或改为 skip。三个目录路径测试 Ruff check/format check 均通过。 |
| 配置与清理读回 | 72 个受保护 Python 配置字节不变，顶层 `conf/` 不存在；1,028 份 case 日志全部结束且无失败。六容器、六卷、十端口及私密服务配置独立确认已清理；四个运行时私密文件和七十个自有链接已删除，主仓配置及借用原生资源保留。 |

模块版本未另行升降，仍采用[冻结依赖对照](go-dependencies.md)中的版本。
远程 CI、真实付费供应商和生产部署仍未执行；上述 Go 验收不表示全部 Python 检查通过。

### Python 目录缺项后续修复

2026-11-15 已修复上述 Python 目录失败：在 `configs/llm_factories.json` 的
`SILICONFLOW` 中补齐 `Pro/deepseek-ai/DeepSeek-V4-Pro` 和同批缺少的
`Pro/deepseek-ai/DeepSeek-V4-Flash`。两个定义直接取自上游
`658ff06ca42d242e0c418f2d13105ec88dc4ec89`，仅将 `model_type` 映射为 `mdl_type`；
Python 保持 1,000,000 token、chat 和工具能力，冻结 Go 定义保持 1,048,576 token。
这是一处明确的本地目录补齐：上游 `dcf623d6` 后的冻结 Python 目录仅列出不带 `Pro/`
前缀的两个名称，而冻结 Go 目录仍列出 Pro 名称。现有非 Pro 条目和其他目录数据保持不变。

Kimi-K2.6 保持 `dcf623d6` 已有的 `image2text + chat` 声明，原回归测试改为精确校验
这两个能力；新增真实 catalog/discovery 路由回归，验证四个 Pro 型号的唯一性、上下文和工具能力。
相关七个测试文件共 104 项全部通过，0 失败、0 跳过（`ee7cb0e96bc1489d8dc4aade7a64dce3`，raw 0）；
Ruff check 与 format check 均通过。证据目录为
`/Users/xldu/.local/state/multirag/validation/20261115-python-siliconflow-pro-catalog-7e01d6a4/`。
此前 96 通过、1 失败的结果保留为合并时的历史证据，本次相关检查已通过。

## 源分支历史验收与边界

专项在独立 worktree 执行；主仓的其他提交与未提交改动持续保留。本地完整证据根为
`/Users/xldu/.local/state/multirag/validation/20261109-go-full-alignment/`。
该目录保存冻结归档、逐路径/符号/历史删除账本、旧测试源与逐项处置、输入哈希、
原始退出、测试事件、首败和独立资源清理读回。逐路径或 AST 计数用于查漏，不能替代语义验收。

已取得的定向证据包括真实 native 重建与 ASan/UBSan、词典/NLP/KG、六种 Office 的真实 CLI、
四个 main 的冷启动和停止、真实 NATS/SQL 失败重投，以及引擎、鉴权、文件快照、模型和消费者
的 SQL/HTTP/race 回归。有效业务断言迁入原函数消费者；仅依赖已退役门面、旧 schema 或旧协议
的断言单独记为退休，不用它们阻止结构收敛。

本地 Go 收敛与适用验收于 2026-11-15 完成，交付在独立分支
`codex/go-full-alignment`。425 个原生产 Go 文件均可直接对应；CLI 文件名按项目名映射。
原 6,418 个函数/类型符号无缺失、无挪动。新增四个测试登记文件不进入生产依赖图；
新增生产符号和必要签名差异逐项列入例外，不以 AST 数量替代业务验收。

| 本地证据 | 结果与原始回执 |
| --- | --- |
| 四个 main、生产包 build/vet | `8361b08763694a08a4522939488e3f5d`，raw 0。带 integration 标签的两个受影响包 vet：`9e3780e931704638befd987f2fa5467c`，raw 0。 |
| 全部 `internal` 默认包 race | `ea7cc49d6fb54a4181a56004ab158382`，raw 0；1,152 项输入和四个候选二进制哈希无漂移。 |
| 原 integration 标签验收 | `66de7a2f108944cd9a9d38927d98e669`，完整 admin/handler 包 race，raw 0；1,153 项输入无漂移。该标签只增加两个原测试文件，生产依赖源码图比对无差异：`21070be37c2b4e3e8e59fb5ec6013d1d`。 |
| 最终包级组合与 CI 清单 | 用带标签的 admin/handler 完整结果替换默认组合中的同名两包，其余包复用相同源码、模块和配置的结果；保留两份原始输出与 composition。共 35 个有测试的包、2,278 个顶层测试、7,258 个通过事件，0 失败、0 测试跳过。51 项必需消费者核对 `708de0acddf24ae38c2a0962f28a6b89`，raw 0。结果按包组合，原始输出独立保留。 |
| 最终二进制冷启动与 CLI | `60a53226bd32499cb6f0f8a330f1564b`，raw 0；真实 admin/API/ingestor、鉴权、SQL 读回、CLI、停止和端口重新绑定。六种 Office 的实际 CLI 在完整 race 中执行。 |
| native 与构建生命周期复用 | ASan/UBSan `a20b98a27f174911be92c8aa5705c1db`、双进程首退出回收 `fc10d2888a1a49758e3978bde7256fe2` 均 raw 0，输入哈希重新核对一致。NLP 测试有改动，使用本轮实际整包结果，不复用旧测试哈希。 |
| Python 支撑文件 | CI 策略单测 40 项通过；八个支撑/目录测试文件 Ruff check 与 format check 通过。保留生产目录断言和已独立复现的既有基线失败。 |
| 清理与读回 | 默认包 1,022 份、integration 包 532 份登记日志全部结束且无失败；保留逐项 SQL/对象/监听器清理证据。六服务及旧登记遗留卷清理后，三个登记组容器、卷和端口独立读回均为空；12 个自有借用链接、28 个临时私密文件已删除，借用模型原文件保留。 |

CI 的 vet/test 现明确启用原 integration 标签。首轮的错误库存断言、OTel 校验/schema
冲突、冷启动留下的测试任务流，以及 CI 必需项漏纳标签文件的首次失败均保留，随后按根因闭合。
最终暂存检查删除两个恢复文件中的纯空白；已核对规范 Go 格式及解码 JSON 完全一致，
单列非行为差异并复用上述行为验收。
561 个旧测试文件、55 项模型服务断言和 25 项旧 router 测试均有逐项去向；
退役旧协议的断言与迁移到实际消费者的有效断言分别记录。

受控供应商 HTTP 验证不代表付费供应商实测；本地通过不代表远程 `ci-required` 或生产通过。
一次旧 Bedrock 夹具曾越过受控边界并收到真实 AWS 403，按验证协议偏差单列保存，不算供应商验收。
Python catalog 定向测试的一项 SiliconFlow 缺失模型断言已在未修改基线独立复现，保留该生产断言。
冻结目标尚未接通的公开 Agent 执行、部分组件及 PDF 等能力按原入口明确失败，
对应真实能力边界见各模块 README 与例外记录。未 push、部署或切换生产；
远程 CI 与真实付费供应商验收未执行。主仓其他活动分支及其 WIP 保留。
