# 7dd40309 后二十项本地交付

冻结 `(7dd40309863ba8c0f9654e4f9f32a2f51f49d421,298a23f74c41cdcf6dbaca6ff574e69a3a2a8e80]`，
后修参考上限 `519e7d98a5651564d4e35d6648f006cba4baaf4f`。未 fetch，未使用更晚工作树。
20 编号、80 条逐提交路径；第 16 项为零 diff，保留编号、不补第 21 项。
详细 scope、逐路径处置、原始验证、接收映射和恢复/清理读回在本地
`20261029-batch-after-7dd40309` 证据根；实际最终 SHA 以 `delivery-complete.json` 为准。
Web/master 与 consumer pin 固定 `bb49639a87b0f4c22aabe73a707083c64fbafada`。

| 编号 | 行为与质量处置 |
|---|---|
| 1 | 等待消息已有 running/终态状态机；保留失败、EOF与最终替换合同，补真实 UI 回归。 |
| 2 | LMStudio 既有 await/取消排空保留，补实际 SDK transport 回归。 |
| 3 | OpenRouter 统一严格 raw/envelope helper，API/chat/CV/discovery 同合同。 |
| 4 | Moonshot 保留严格请求/响应，补模型名称 trim。 |
| 5 | Xiaomi 补四媒体入口与目录，保留较强 chat、超时和取消能力。 |
| 6 | suffix 局部严格解码含大小写外层字段，保留顶层扩展；CoHere/xAI 目录已正确。 |
| 7 | MiniMax Models probe、有界响应与 trim；保留成熟流式边界。 |
| 8 | Qwen reasoning 当前机制与家族边界已覆盖，无收益的重写不采纳。 |
| 9 | 无不支持的假元数据，保留实际可用 FishAudio/MinerU 分层。 |
| 10 | 保留更强 CI，补 GPUStack keyless/Ollama 空白 key 和真实 WordNet 测试；48 个遗漏 manual-live 精确纳管，另加本批 2 个独占 SQL 测试。CI 已供服务的 2 项及真实 CLI 子项必须通过。其余逐路径保留更合适的本地实现。 |
| 11 | Volc 官方 URL、严格 metadata、raw Ark/endpoint 解耦及真实 Web/SQL 消费。 |
| 12 | is_english 重复 token 匹配补 list 缺口，保留 string 字符比例与阈值语义。 |
| 13 | 保留现有安全 transport、token 与 reasoning 合同，不回退后再修。 |
| 14 | 显式全局目录、alias 冲突拒绝与深拷贝、分页、真实 user/admin 鉴权及严格 CLI 响应。 |
| 15 | Search 既有 SQLAlchemy 缺行 guard 保留，补无行/失效 owner/鉴权/事务失败实际回归。 |
| 16 | 第一父 tree 完全相同，0 path，无额外实现。 |
| 17 | 已有 reranker 优先、bge embedding 与 0 上下文合同，保留。 |
| 18 | OceanBase 启动协调 wrapper、只读 health、生命周期与凭据保护；Compose 精确 hunk，不改 MinIO 外部改动。 |
| 19 | 已有 final-only 最终替换和更强终态/EOF处理，补真实消费者回归。 |
| 20 | 保留 typed candidate/manual-picker、同候选 verify-save、0/false/multicap 与帮助链接；适配 Volc 发现。 |

Python 生产后端复用精确候选输入的相关单测、lint/import-linter 与 PostgreSQL scratch 证据。
固定 Web 经真实 JWT→Python→受控官方 HTTP→原子创建和独立 SQL 读回，发现/校验及失败
均零注册写入；实例精确删除，父 provider 保留至自有 DB DROP。

Go 是并行实现。当前组合完成 build、vet、三个独立 main 的真实 native 链接及受影响 race。
MiMo 四个实际 service 入口由 SQL 选择实例/凭据，验证 JSON/SSE、格式、取消、失败与排空；
真实 user/admin 路由与 CLI parser/Execute 消费验证权限和 SQL 无写入。C++ 从当前源码实际
重建，旧 archive 的 tracked 源码相同不足以证明编译来源。范围外四条既有 entity/models
测试失败单列，未削弱断言，不声称完整 Go 包全绿。

独立 Web 完成等待/失败/EOF/final-only、typed 表单/发现、Volc 选择的相关测试、build、lint、
文件体积和真实浏览器回归；多图 contact sheet 审阅，原始 UI 记录单列。主树其他任务 WIP
增量保存并保护，未计为本批成果。

原始首败/取消、输入版本与登记偏差均保留，严格协议不宣称通过。Web 借用 cache 的真实写
目标初始登记不足、N Ruff cache 登记偏差、O 首轮输入漂移，以及 I 首次媒体通过引用旧
suffix 输入清单均据实记录；I 已按当前输入补验，不能用后来通过改写历史。旧批偏差维持原结论。

受控供应商/SDK 不代表付费账号、真实库存或生产权限。未执行完整生产、常驻 worker 或
真实 OceanBase 启动/存储验收。远程 ci-required 待运行；未 push、未部署、不自动下一批。

上述四条 Go 遗留失败由后续[模型目录回归收尾](go-catalog-regressions.md)专项处理；
原始批次失败记录继续保留，专项验证与本批历史证据分开记录。
