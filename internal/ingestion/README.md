# Go ingestion

`chunk_engine.go` 定义 ChunkPlan，`chunk/` 保留各原始 operator 和 expression 文件。
DSL 的 `pipeline` 按声明顺序编译全部 operator；允许重复、任意阶段顺序与空 plan。
`ChunkPlan.Operators` 可直接查阅，Explain 不读取文件。Execute 使用实际输入并传递错误；
字符大小与 overlap 按 Unicode code point 计算。无兼容 stage 数量或顺序限制。

`parser/` 的各 Office parser 默认调用 office_oxide 0.1.2 native binding，原 Parse 方法打印
解析结果并返回 error；GetParser 的 `lib_type` 为 `office_oxide`。没有 builtin OOXML
替代路线、统一 ParseResult facade 或可选 build tag。
支持 DOC、DOCX、PPT、PPTX、XLS、XLSX，真实格式需通过 native fixture 验证。
PDF 原方法目前明确返回未实现错误。native 内部分配不受 Go 上层内存限制保证。

`ingestion_service.go` 保留 Ingestor、executeTask 和 executeTasklet 原实现入口；任务业务处理
仍未由冻结目标完成。收到任务后原位置写入 FAILED checkpoint/status，在事务提交后 ACK，
写入失败 NACK，不产生虚假的成功或进度。NATS 使用实际配置和 subject；消息畸形会终止，
队列错误传播到启动入口。Stop 等待自有 worker，停止期间不会新增 WaitGroup 任务。

运行依赖独占 MySQL、NATS、Redis、对象存储及 native 字典；使用 `cmd/ingestor.go` 独立
入口。配置、四个构建入口和 registered 验证见 [开发说明](../../docs/development.md#go-并行实现的本地验证)。
