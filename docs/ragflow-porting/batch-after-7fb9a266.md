# 连续批次 after-7fb9a266

范围为 `7fb9a26623b0ed67b20c34d00b037579d71d2cb4` 之后连续 20 个 first-parent 提交，
截至 `b7d88f0b0904e427b5e31cdf0dabbae0f8cd7279`；直接后修检查上限
`519e7d98a5651564d4e35d6648f006cba4baaf4f`。本批尚在实施，候选和单线完成不代表主干交付。

## 文档项评估

| 项 | 完整 diff 与本地质量比较 | 处置 |
|---|---|---|
| #4 `129e1e31` | HTTP 参考给普通/OpenAI Agent 模式增加 `chat_template_kwargs` 字段及示例；另修发布文字 `wer` 拼写。本地已有同一路由、严格 object/null 校验、每轮隔离、SDK extra_body 与 false/0 语义的实现和文档；上游示例仍保留已退出的旧 Agent 路径标题。字段存在不等于所有模型能力一致。 | 保留本地严格语义；在[HTTP 参考](../references/http_api_reference.md#执行-agent)补普通/OpenAI 的可复制示例、各模式默认 stream 与模型能力边界，不恢复旧路径。本地无对应 release_notes，拼写修订无承接文件。 |
| #5 `0071e98c` | 发布说明重新解释 RAPTOR AHC、补已修问题的触发场景及 issue/PR 链接，增加 Agent 参数文档链接和法语约 1400 keys 声明，删除旧概括性条目；没有可执行行为。上游发布版本、性能对比与翻译数量均属于 RAGFlow 的发布证据。 | 本地没有 `docs/release_notes.md`，不创建冒称 MultiRAG v0.25.6 的发布说明，不复制未复测的 Recall/F1/性能及翻译数量。当前功能与运行合同仍由模块参考和本批逐项验收维护；#4 承接可操作的参数示例收益。 |
| #16 `bea80920` | 开发启动命令将位置参数 `1` 改为 `-i 1`，以匹配上游执行器 argparse。 | 按 #6 已测试并合入的本地 CLI 更新开发命令为 `-i 1`（`--index` 同义，默认 0），仅支持 `-t common`；容器唯一活动位置参数调用已迁移，consumer/metrics 命名保持，未承诺不存在的任务类型队列隔离。 |

## 资源门禁

统一入口及覆盖边界见[事前登记验证命令](../development.md#事前登记验证命令)。
门禁实现已合入 `4ea9f965a7e7323cfed92939c415915bfb82113b`；本地定向回归为 46 项及
3 个 subtests，另有实际 Go helper 独立编译/HTTP 监听器读回。它不是完整模型包或 native 验证。

修正后的最终回归保留成功、失败、取消各子命令的原始 journal/output；初期门禁自测曾移除
嵌套收据，原外层退出码和输出仍保留。首个 bootstrap 的 Python 3.9 注解解析失败及中间
lint/type/收集失败均保留。上批 strictEvidenceContractSatisfied=false 与这些历史缺口不倒填。
本批严格证据应按每个实际命令和动态 case 判定，不能由门禁存在推导。

## 当前交付边界

本批其余项仍待各线完整评估、修复及串行集成；尚未声明 20 项完成。
最终按冻结源码、配置、锁文件、后端及 Web 消费者 pin 绑定定向组合证据。
未 push、未部署；远程 ci-required 待运行。真实供应商、常驻 worker、生产及 Linux native
证据需分别说明，不能用受控边界或静态结果代替。
