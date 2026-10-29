# aab01af6 后二十项本地集成

冻结排除 `aab01af6f298ac175a2a3b0b5636d47c385f091e`，连续 20 项至
`5e0a7ce408c1fd4b85d8734b7f3b3da27ec4f6ab`；直接后修上限
`519e7d98a5651564d4e35d6648f006cba4baaf4f`，未 fetch、未采用更晚 checkout HEAD。
20 个编号、154 条逐提交路径（其中 #11 为 70 条）按冻结清单审结，不因有据保留制造提交。

可执行源码组合为 `c5585e678271f1a01cbaa8812dc2c812edad05b7`；后续只补本批说明。
Web master 与后端 consumer pin 均为 `5afb4fbc5787158dfed9785d437c568a145c3b20`。
验证在与该已提交组合一致的独占干净 checkout 进行；主工作树原 28 项 WIP 另行保留，不作为已验输入。

## 逐项处置

| 编号 | 上游 | 处置与质量依据摘要 |
|---|---|---|
| 1 | `6cba5a54` | 空 Switch 条件不命中；保留有效零值与 ELSE。 |
| 2 | `214ee319` | 保留活动 owner/tenant 授权；显式空 owner 选择不扩为默认范围。 |
| 3 | `aa9545e4` | 保留本地 Doc/Task 锁、nonce 与取消排空，不复制已撤销 CAS。 |
| 4 | `ea79d65d` | KG 收敛至 service/kg；真实 Python producer 写 rank/N-hop/community，旧节点刷新复用向量；取消排空。 |
| 5 | `9c14e3f3` | 模型加载状态、数值/实例选择、memory 全分页；取消停止续页，dataset 模式不观察 memory。 |
| 6 | `1deb1313` | CLI 一次批量请求；dimension 省略与 0 区分，完整验证后原子处理。 |
| 7 | `5a04ac08` | Go Dify POST 与公开 health；API key 保留原 tenant，JWT 保留用户身份；空 conditions 零下游 IO。 |
| 8 | `b0a45809` | 保留更强 OneDrive 路径、编码、folder ID 与 scope-bound checkpoint；受控 Graph 验证。 |
| 9 | `b05d5a52` | 安全 POST discovery、有界 Ollama tags/show、惰性 metadata；typed 多模型原子创建与 Web 实际消费。 |
| 10 | `074c331c` | Web/SDK 文档预览、下载、artifact 的独立鉴权；legacy metadata/list 补 actor/context；合法空 bytes 保留。 |
| 11 | `c960dc2a` | 完整 70 路径评估；活动 datasets/search、metadata/tag 授权、精确整数、PIT、模型 ID、native POS 修复与 CLI 贯通。 |
| 12 | `91983106` | Python 已有页宽对齐保留；Go 同一窗口用于 fetch 与页内 slice，真实 page 7 验证。 |
| 13 | `3d7adf21` | 登录用户读取嵌入式 plugin metadata；不伪造动态 Python plugin 执行能力。 |
| 14 | `38f9ea5f` | 统一实际 reranker 入口，保留校准 [0,1] 与 0，拒绝非有限值；HF 跨批次共同归一。 |
| 15 | `5e25e260` | Go Xiaomi driver/catalog 与受控 HTTP/SSE；不把 Go 目录当成 Python 已支持。 |
| 16 | `b1a2210d` | 保留既有更大且有界的 JieKouAI SSE buffer，补长帧/超限回归。 |
| 17 | `220ee9db` | 统一 reasoning family/namespace/case，保留 Qwen3 think/answer 边界。 |
| 18 | `c935f305` | 时区原始值显示与精确往返，保留无密码入 profile 的更强合同。 |
| 19 | `6bf70564` | 支持的 Spark 静态候选、discovery features 与 Fish owned voice 稳定 ID；候选不伪造 numeric ID。 |
| 20 | `5e0a7ce4` | Go 先归一 rerank 分数再混合，稳定排序、词项边界、失败和取消明确传播。 |

## 本地证据与边界

- I 组合 Python：改动文件 lint/format，13 个相关单测文件 557 passed；共享 integration harness 47 passed。旧 Search test-to-test import 的基线失败保留原始报告，本批实际使用该消费者链后把 helper 收至 `tests/support`；新增 external consumer module 同时纳入 suite，不削弱断言。
- 固定最终 Web 的真实 HTTP/SQL/Milvus Search 与 rerank 消费者 2 passed；provider discovery 的实际 Web→JWT→Python→受控 Ollama→两 numeric ID→独立 SQL→精确 instance 清理另有最终单例证明。父 provider 行由独占 scratch DB DROP 清理，通用消费者不级联删除它。
- I 组合 Go：`go build ./internal/...`、`go vet ./internal/...`、三独立 main 的实际 native 链接全部 raw 0；10 个受影响包 race 188 顶层、923 含子项通过，无 skip。实际 Router/JWT/PostgreSQL/ES/CLI、Python producer→ES→KG、文档 SQL/MinIO/Redis/Web、Dify 身份与失败零副作用按各自运行输入独立留证。
- POS 修复以当前生产 CMake target 重建 archive，14 项 ASan/UBSan 与当前组合 3 项 tokenizer race 通过；三入口链接使用该 archive。此为本机平台验证，不是所有 glibc 位级一致性保证。
- Web 初始冻结版本完整测试 2,240 项；后修分别做分页取消/模式卸载、cleanup 边界及 API 定向回归，最终 build/lint/类型与文件清单通过。明暗、中文、时区和实际组件交互按带路由/视口的 contact sheet 审阅。不同版本的通过数不拼成一次完整最终测试。

原始退出码、JUnit、源码/配置/锁/消费者 hash、首败、取消、最小复跑、只读等价比较、资源登记、独立读回、候选 bundle verify 与 base-only/no-alternates/先缺失 candidate 的实际恢复，统一在外部
`20261029-batch-after-aab01af6/delivery-complete.json`、`acceptance.json`、`validation.json` 与各 owner handoff。
当前状态和历史 captured 字节分别保存；恢复与清理不恢复已删除凭据 payload。

未 push、未部署；远程 `ci-required` 待运行。没有真实付费供应商/微软租户、生产、常驻 worker、完整后端全套或干净重装验证。
Go 保持停滞的并行实现边界，未替换 Python 生产后端；无 standing Infinity 服务，SDK/受控边界不冒称实际 Infinity 查询，Go Milvus Search 仍明确 unsupported。
自有普通 worktree/ref/admin、DB/bucket/collection/Redis/listener/cache 等按确切身份清理，借用模型、服务、环境、node_modules 与其他任务 WIP 保留。不自动开始下一批。
