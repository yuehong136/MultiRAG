# Go 模型目录回归收尾

本次修正四条过期测试合同，不改变模型目录或生产运行行为。Go 是并行实现，
本次验收不代表 Python 生产服务、真实供应商或完整 CI 验收。

| 回归 | 当前合同与检查 |
|---|---|
| Aliyun 模型列表错误 | HTTP 失败或业务错误均拒绝返回模型；即使响应同时含模型数据也不能发布部分结果。错误保持脱敏，不回传可能含凭据的供应商正文。ListModels 和 CheckConnection 均检查实际请求与失败结果。 |
| OpenAI 模型类别 | 用现行目录中的 gpt-5.4-mini 检查 gpt 类别与 chat 能力；保留现有目录测试对已退役或不兼容条目的拒绝，不恢复 gpt-5.2-pro。 |
| 模型级 thinking | SiliconFlow 四个显式声明的模型必须保留 default_value/clear_thinking；Gitee 和 SiliconFlow 未声明的模型不得继承旧 provider 默认，provider 级旧声明也不得复活。 |
| Voyage 目录 | 保留原有八个和新增五个模型，逐个验证能力、上下文限制、类别及列表/过滤结果，检查重复身份、具体工厂驱动和该驱动的端点配置。新增有效模型不受旧“恰好八个”断言限制。 |

Aliyun 错误回归使用现有 provider 资源 helper，在监听器创建前登记精确地址，
关闭后独立读回；受控 HTTP 使用虚构凭据，不调用真实供应商。
Voyage 实际 Embed/Rerank HTTP 合同由同包受控请求回归覆盖。

本地检查包括两个受影响包的完整 race 回归，以及内部包 build/vet、三个独立入口
的 native 链接和模型目录/供应商链路的定向 CLI race。详细命令、原始首败、逐文件输入哈希和清理读回保存在
本地 `20261030-go-catalog-regressions` 证据目录；不能用后来通过覆盖首次失败。
两个包共 559 个顶层测试、CLI 相关 7 个顶层测试通过，均无失败或 skip；build/vet
与三个入口链接也通过。native 静态库由当前源码重建；构建缺少的锁定版本
`golang.org/x/image` 在自有缓存补齐并核对 go.sum，未修改依赖锁。

额外执行完整 CLI 包时，未改动的 `TestDocParseModelDeclaration` 仍按旧的顶层
`model_type/model_types` 参数断言，而 parser 已返回 `models` 声明集合。这条范围外
首败保留、未修改，不计为本次四项失败，也不声称完整 CLI 包通过。

远程 `ci-required` 待运行；未 push、未部署。已有 legacy 测试的资源登记边界仍单列，
本次不宣称完整严格资源协议通过。
