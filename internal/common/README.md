# Go 公共组件

`internal/common` 为 Go 独立实现提供错误码、任务消息、元数据处理和 Zap 日志。
Python 生产后端使用自己的公共组件。

程序在读配置前调用 `Init(level, logFile)`，文件日志位于当前工作目录的 `logs/`，
冷启动会创建该目录。`Init` 重新绑定 Logger/Sugar，保留同一 AtomicLevel 控制器；
`SetLevel` 与读取级别可以并发，已有 logger 引用也随该控制器调级。
默认 AtomicLevel 为 info；Init 前的日志函数没有 logger 时直接返回，Fatal 会报错。
`GetLevel` 和 `IsDebugEnabled` 可在初始化前调用。`Sync` 保留无返回值合同。

元数据过滤、引用设置和 KG 响应解析均使用对应原函数；无旧 CSV/多返回值 facade。
畸形谓词和 KG JSON 不当作空成功结果，元数据数值比较保留大整数精度。
具体运行与验证入口见 [开发说明](../../docs/development.md#go-并行实现的本地验证)。
