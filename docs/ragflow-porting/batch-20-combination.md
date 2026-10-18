# 20 项串行合入与组合验收

本批对应冻结上游 `519e7d98a5651564d4e35d6648f006cba4baaf4f` 内的 20 个目标，
目标为 MultiRAG `main` 与独立 Web `master`。`15dcdd7b5b1cb7c2433a8cf667d749ae6f5eb8c6` 是上游队列游标，
不是 MultiRAG 基线；各 owner 的 `9582bdb80f1a1be4505f4061d1abe71aa4cac63a` 隔离候选可以在当前主干按依赖集成。
旧批正式交付 `3bd0bfc77e0c114c2f505a0e40aef7f645f95112` 和其额外 `f2dc7ba2d7818e529c1f48a4381cc47a71e99439` 复验保持独立，不能替代本批组合门禁。

## 范围与提交映射

所有候选按完整 diff、owner 原始证据和实际调用方审查；共享账本与测试分组保留双方新增项。
以下 SHA 是实际主干中的交付；原始候选映射保留在各 cherry-pick 提交信息和本机证据中。

| 编号 | 上游目标 | 本地主干交付 | 处置与边界 |
| --- | --- | --- | --- |
| 1 | `1d0519d02507dbf87b11d7101d35c0b64ba55ead` | `6a98e2db80d7ad44c48dc4644130824011103cfb` → `c670b8392b493e3b882c7c434a0ab97f6d070cd7` | 共享 signer 初始化及固定密钥/Redis 竞争合同；前置于 13。 |
| 2 | `53a4edfdedf70b020f0b74ec3cd83b7c84bced1e` | `4be81de8e50606ef4d2cb149e094ec18c3562110` | FastAPI 显式依赖与 AsyncSession 集中访问检查，嵌套 session/version 绑定路径资源，开流前拒绝。 |
| 3 | `12f80f170cc732b19f54a5ff4e75ce368694bcf8` | `1d698a2e3b668e20f803d1d0f256e8e09daf6c4e` | SDK/锁/镜像同步到 dev6；不追其他依赖升级，真实后端专项另列。 |
| 4 | `86fe78c73f523e95d129a1f7f1668366b6a14817` | `775d4e46037288a9213a76ae4dfd92974e058ab4` | GroupId 进入最终 URL query，适配 SDK 路径拼接、重复参数和关闭客户端；不按标题误改 header。 |
| 5 | `b8b741555f9e4fb536b79cbd76928ccd3209abdc` | `259a19947859b75a160dfa5f5e61616432c43283`（此前已合入） | Go OpenAI 聊天、模型发现与实例配置，沿既有受控 HTTP 管线。 |
| 6 | `911671cef0810835207de4e4a7c68157c2c476c5` | `bb8e7fb0d3aebbb6a4c5cf0f2d9cb5fce9aeb28b`；Web `77658aeae279835dac84934155caa0de38b941dd` | RDBMS 参数化读取、持久化游标和完整删除快照；失败不删不推进，删除默认关闭。 |
| 7 | `078ea3bf4a773d973472136aa4ceb843d712b061` | `fd346b7d3fd92a89346621180172527313e19b1a`（此前已合入） | Go NVIDIA 与 CLI EOF/timeout；保留有效后修恢复的 OpenAI case。 |
| 8 | `1d114f034bf8783ae52f048e5ee5c1053d711a22` | `7ac41937e6f6b47474c44e2c661174a6afce4377`（此前已合入） | 有界任务日志配置、UTF-8 容量和事务并发追加；不改变取消/恢复语义。 |
| 9 | `c50028b1f332f6332965bdec95b2ca29b655f40c` | `e1f492cd57b1c8b23ad3cb69ed5c117e5e266795` → `e7c4a3e3761d4c4a3d22ba75aff299bd774780c3` | 用户明确保留 owner 管理与显式创建会话、input-form 边界；已有团队 debug/reset 保留，不采纳扩权，不称完全等价。 |
| 10 | `5b162a0c4693d78e49e591d94d00e2c61184c8e2` | `892810deb083a08e3c7aaa867fd33e112650ec0a`；Web `5f918e004d82fcd12c27561c94df5465be6b7ce2` | DocGenerator 默认关闭的正文下载信息选项，经 Message 保留元数据；字面量替换及日志脱敏属于同链必要适配。 |
| 11 | `05011348205f16973e8baa00e579bee9b7eab0a6` | `29eccd7374308ed815167dc083d58a3ef8a96fa8`；Web `d222a68c0aad63ee849668c3aaa7ea1ed5f80d7c` | 显式 is_tools 保存/重载/执行，保留缺省凭据及 GroupId；视觉层仅在绑定工具时复用既有聊天工具循环。 |
| 12 | `5c9124c3ef6e8c7fb8e54edc85d70f99b6b1e7ae` | `6b12e486044c9af1a32eda6fecddf51eb488d670` | Azure 路径以逻辑 bucket 隔离，读写删一致；不增加平铺旧路径回退。 |
| 13 | `94324afee91acb39939efe88b2d4d4e70827df91` | Python `348fac258750e171aef5635210f171d73f85345d`；Go `625cd756dd81153441b95c4cc9c69e074b081b70` | 沿现行协议对齐 Go/Python 主 API JWT；SQLAdmin/Go Admin 保留各自现有准入，不新增后台 Cookie 交叉登录权限。 |
| 14 | `59bb184e63e47b7cb706a104f8cc3096bca02821` | `f2dc7ba2d7818e529c1f48a4381cc47a71e99439`；Web `711eba0302d7bbe1d480346941c71a5d85aa7e99` | Moodle 完整课程/module 快照和删除同步；任一读取失败阻止删除，成功空快照可清最后一条。 |
| 15 | `bb10b83e6114cc271c3563194464b19297dc5c25` | `3bd0bfc77e0c114c2f505a0e40aef7f645f95112`（此前已合入） | Go 智谱 rerank 按索引还原，拒绝缺失/重复/非有限分数，保留取消与实例隔离。 |
| 16 | `057806d7f116506a0f184604591e9427047bead7` | `02bf325667cc796a43b2654a2a485b1dd18e8c29` | 真实 Azure SDK 的 SPN delegation key/file SAS；SAS-only 无独立对象签名能力时明确失败，不泄露容器凭据。 |
| 17 | `c29335cbff6aabca0148172c67c730740ed6f766` | `b1532b0ef5f136aaa23197d82af7bd32e715f47c` | 显式启用的 LocalProvider、共享 Python/JS 结果协议与错误传播；子进程执行不是安全沙箱，不默认启用。 |
| 18 | `805a2daac21096219ed2d5cf17443edb61fb7f2d` | 等价保留，无新增提交 | 本地 knowledge documents/search/logs/settings 活动路由语义及实际导航已覆盖；避免复制上游重复 slash，不造无消费者 alias。 |
| 19 | `59c35100c56ab8954f1c4066cac512eb51bbf194` | `59baa958ddf58b67717e64d630c8da2f728c6299` | 延迟读取与流式过滤，保留类型、数组否定、Unicode 和缺字段精确语义；SQL/ES 按已授权 KB 范围，其他引擎明确回退。 |
| 20 | `5d28bb0701da9f248deabc183e41aa82bf5e638b` | 不适用，无新增提交 | 完整 diff 仅 Turkish locale；本地仅注册中英文，不借此增加产品语言。 |

逐操作权限依据见 [Agent 权限](AGENT_ACCESS.md)；各实现细节与原始 owner 验收仍由相应专题维护。
18/20 的完整只读审计及浏览器证据保存在本机 `.test-results/port-18-20/`。

## 组合验证

最终执行源码锁定 MultiRAG `3997f604149655ae8ae00b6574d8c2eb350712ee`、
Web `6ad9577e3d76bdd91d2df9915b6e0c21e447f5ac`；随后的记录提交仅改本文与账本索引。
首轮失败已修复，最终 verify、完整默认 core 与 consumer 均已完成并核对原始报告。

| 检查 | 实际结果 |
| --- | --- |
| `make verify` | exit 0；6566 unit passed，3 条既有 transformers warnings；Ruff、8 条依赖契约、async DB、mypy 146 文件通过 |
| 完整 `make integration` 默认 core | exit 0；1110 passed，0 failed/error/skip，14 项独立 consumer 用例按既有规则排除 |
| `make integration-consumer` | exit 0；14 passed，0 skip，52 项非 consumer 用例按分组排除；包含新增模型与文档客户端 |
| `make integration-infinity` | exit 0；真实 dev6 22 passed，0 skip |
| 两副本隔离 API `make smoke` | 两次 exit 0，全部健康项 ok；实际资源 bootstrap，关闭后台 worker lifespan |
| Web `test:ci` / lint / build | 均 exit 0；1463 passed；lint 0 errors、1366 既有 warnings；构建含完整类型检查 |
| Web 专项 | Agent strict、typed lint、文件体积、bundle size、消费者脚本类型/格式检查通过 |
| Go 受影响包扩展 race | 804 个测试/子测试 passed，0 failed，14 个显式 opt-in skip；鉴权 live opt-in 已另行实际执行 |
| Go build / vet / 三个独立 cmd 构建 | 全部 exit 0；Go 1.25.14，readonly modules |

组合收尾修复分别提交，完整门禁成功后回灌实际 main：

- `823002c0082043585f667441e5a872f08532d971`：保留 MinerU/PaddleOCR/OpenDataLoader 的对象凭据封装。
  对照目标 `05011348205f16973e8baa00e579bee9b7eab0a6` 及其凭据后修链核对后，
  恢复既有 `api_key` 对象合同，保留 capability-only 编辑、轮换及未知字段处理。
  八个 PaddleOCR 真 SQL/受控 HTTP/PDF 合同的原断言全部保留，另补三工厂创建/轮换/编辑回归。
- `a5d6ef970bef6c27eaac74b381a88607fd766df4`：sandbox 集成复用正式 schema bootstrap 的 scratch fixture，
  移除与其他用例冲突的直接建表，并清理本用例创建的两项设置；不改生产 schema。
- `59ae948baf3163946c89b388e2922f3eaecce191`：metadata ES 在收集前准备真实 Elasticsearch 8；
  使用显式可丢弃 listener 或由 Testcontainers 创建、回收，不隐式选用应用 ES。
  显式 listener 不可用/版本不符会失败，原 ES SQL/scroll 断言不变。
- `3997f604149655ae8ae00b6574d8c2eb350712ee`：将本次新增且已通过的两个实际 Web 客户端用例登记到
  独立 consumer 必跑集合；默认 core 避免要求相邻 Web checkout，未移动任何失败用例。

首轮 `625cd756` 的原始结果为 1009 passed、91 failed、13 errors；其中时效失败及
MinIO `RequestTimeTooSkewed` 与本机/Docker 时间偏差相符。恢复稳定服务后，保持原 TTL、
权限和业务断言的定向组合 **150 passed、0 failed/error/skip**，覆盖全部原失败组及上述修复。
中途一次复跑被 Docker 生命周期事件中断，六容器同时退出；恢复精确 owned 项目后按动态
新端口更新私有 overlay 并复核服务时钟。另一次 verify 调用误带 integration overlay，
配置优先级和无 DB 单测失败；纠正启动环境后以完整 verify 通过，无断言放宽。
这些失败/中断报告均保留，不计为通过。最终自动读回证明首轮 **103 个失败/错误节点全部留在 core 并通过**；
仅两个此前已绿的模型/文档用例移入 consumer，前后完整集合均为 **1124 个不同用例**。
PaddleOCR 使用原有本地受控协议，不要求真实云端 key。

Web `6ad9577` 源码未变；Go、Infinity 及鉴权 smoke 的生产输入也未受这九个修复路径影响，
相应已绿检查复用 `625cd756` 的明确记录；最终 verify/core/consumer 则在修复 SHA 重跑。
输入 hash 对照与适用依据保存于 `repair-validation-scope.json`。

Go 扩展回归首轮因隔离工作树缺少 WordNet 资产失败；只读链接本机现有 18 个资产文件、
核对 hash 后，同一命令通过。没有修改 Go 业务代码、断言或 skip 策略。
独立 consumer 的 20 张本次明暗截图先拼为 5 张标注文件名、路由、视口的 contact sheet，
再审阅：保存后的删除开关、字段布局、凭据遮罩及明暗展示正常，自动溢出/键盘操作断言通过。

混合鉴权的实际 HTTP/SQL 验收包含：两套 Python SQLAdmin Cookie 双向使用、Go/Python
加密密码登录 JWT 双向解析为相同 SQL 身份、owner/member/invite/外部用户会话范围、
管理员与普通主 API 边界、拒绝不执行下游 SQL（有真实 mutation 阳性对照）、停用/注销、
关闭注册与密码登录后的 SQL/自有 Redis 状态不变。后台之间没有新增 Cookie 互认权限。

新增窄范围消费者合同由后端 `6410b8444c67b377fdd8fbead789603b07468d89` 与 Web `6ad9577e3d76bdd91d2df9915b6e0c21e447f5ac` 交付：
实际 Web helper/client 保存 Ollama 工具开关并经 HTTP/SQL 重载；Agent 客户端保存生成器参数，
从 SQL 读回后由 DocGenerator/Message 生成下载元数据，再由实际 Web 下载客户端读取 MinIO 字节，
校验哈希及匿名拒绝。仅模型响应、文档转换器和浏览器下载 sink 受控；不把该合同称为完整浏览器 E2E。

本机证据根为 `~/.local/state/multirag/validation/20261017-batch20-combination/`，
包含主干初始状态与 WIP 哈希、上游 20 项 SHA/路径清单、原候选到主干映射、
各 owner 外置证据以及分支删除前的代码/独有提交核对。
首轮失败报告为 `integration-final-first-report/`；最终原始报告与全用例覆盖读回见
`core-final-r3-report/`、`consumer-final-r3-report/`、`verify-final-r4.log` 和 `final-gate-audit.json`。
所有 2881 个冻结输入在最终门禁前后 hash 一致，raw JUnit 与 runner exit 均独立检查。

本批专用 `mrag-batch20-20261017` 的 **7 个容器、5 个卷和专用网络**已按精确 owned
清单删除，私有 overlay 已移除；其他常驻/用户新启服务的 ID、运行状态和启动时间保持不变，
读回见 `cleanup-owned.json`。所有本工作树测试报告与 owner 证据均已外置。
10 个候选分支已核对主干交付后删除；唯一组合工作树及 `codex/batch20-combination`
在 main 读回后归档/删除，收尾证据保存为同目录的 `delivery-complete.json`。
28 项其他任务 WIP 保留；三个共享文件以 diff payload 核对，其余 25 个逐文件 hash 核对。
不相关 SDK/工作分支保留，本批未 push。

## 剩余边界

- 测试全部结束后的解释器退出阶段，LiteLLM logging worker 仍有销毁异常输出；首轮与最终原始日志均保留。JUnit 各阶段及 runner 退出码已核对，本批未改第三方依赖或吞异常，该日志生命周期问题仍待另行处理。
- 未部署或切换生产；未验证生产旧 Infinity 卷的升级/降级、多副本压测和独立 JWT 会话撤销/密钥轮换。
- 模型、Moodle、部分 RDBMS/Azure 供应商边界受控；真实远程模型、Azure 账户、Moodle SaaS 和 MySQL 实源未验收。PG 实源、真实对象/向量服务及 HTTP/SQL 读回按各专项记录。
- DocGenerator 真实 Pandoc 四格式证据复用对应 owner 的同源码验收；XeLaTeX/PDF 渲染、完整在线 Agent/分享页浏览器 E2E 未验收。
- 默认 core 排除独立 Infinity/consumer；本批以对应独立专项补充，不把 deselected 或 Go opt-in skip 当作通过。Go 其他未选 live 场景不声称已验收。
- LocalProvider 保持显式启用门槛；它是子进程执行，不提供操作系统安全沙箱。SAS-only 独立对象签名不支持，旧平铺 Azure 对象不做无证据回退。
- 28 项其他任务 WIP 保留且未纳入本批提交快照的门禁；本批没有替这些未提交改动出具通过结论。
