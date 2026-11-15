# Go 模型驱动

本目录是 Go 并行实现，Python 仍是生产后端。Go 的 provider 配置位于
`configs/models/` 与 `configs/all_models.json`；本目录、驱动原函数和调用路线按冻结目标维护。对齐范围与具体问题修复见
[Go 对齐记录](../../../docs/ragflow-porting/go-alignment.md) 和
[例外记录](../../../docs/ragflow-porting/go-alignment-exceptions.md)。

`ModelFactory` 创建实现 `ModelDriver` 的具体驱动。驱动使用 `Message`、
`ChatConfig`、`EmbeddingConfig`、`RerankConfig`、`ASRConfig`、`TTSConfig` 等类型；
`ChatResponse`、`EmbeddingData`、`RerankResponse` 和媒体/任务响应保留各自含义。
`NewChatModel`、`NewEmbeddingModel`、`NewRerankModel` 绑定模型名称与凭据，
Eino 通过 `NewEinoChatModel` 调用已授权的 `ChatModel`。

`ModelProviderService` 在调用驱动前解析租户、provider instance、模型 ID、状态和能力。
HTTP handler 使用请求上下文；`APIConfig.Context` 将取消传到原驱动请求方法。
ASR HTTP 请求中的文件是上传内容，服务负责创建并清理临时文件，驱动读取该文件。
调用者不能借模型接口读取任意服务器文件。

## 各驱动的合同

配置中的路径和供应商原方法决定请求，不存在统一的 URL 改写、自动分批、
参数白名单或全局重排排序层。例如：

- SiliconFlow 的单次嵌入输入最多 32 条；其他驱动的请求数量按各自原方法执行。
- Ollama 的嵌入响应是原生 `embeddings` 数组；多数其他驱动使用带 `index` 的 `data`。
- `ParseListModel` 返回类型化目录记录，保留原来的 `owned_by` 后缀、空值过滤及元数据查询规则。
- 重排的 `top_n` / `top_k`、原始响应字段、排序和截断行为由具体驱动决定。
- `ModelDriver` 是完整接口；未实现的方法明确返回错误，不能用接口类型断言推断实际能力。
- `*string` 流式 sender 不构成任意二进制音频传输协议；具体媒体方法的支持范围需单独检查。

响应校验区分缺失/null 与合法的 `0`、`false`、显式空转录或空目录。
明确发现的无结果成功、索引丢失、错误信封、错误终止、取消与资源泄漏问题，
在相应原函数修复并单独记录，不恢复旧 Go 兼容入口。

## 验证边界

模型单测使用登记的本地 HTTP、STS/SigV4/eventstream 等受控端点。
provider handler、服务生命周期、模型绑定及 ASR payload 测试再验证真实 HTTP/JWT、
CLI 和隔离 MySQL 调用链。需要真实服务的测试使用项目注册运行入口及私有配置，
不使用业务数据库。监听器、文件和数据库资源在分配前登记，并独立读回清理结果。

这些测试不等于真实付费供应商、生产 IAM 权限、部署或线上验收；
目录声明也不证明驱动已经支持该能力。完整通过结论以对应源码输入哈希、
原始退出码及本次对齐记录为准。
