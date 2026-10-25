# 4776bfa8 后二十项本地集成

冻结 RAGFlow first-parent 范围：排除 `4776bfa8a2ead7bdd45e6d0aa860044f9095a344`，
连续至 `0fb85a66bc03e0ec95bc99cb1a13c438f4587e13` 共 20 项；直接后修上限
`519e7d98a5651564d4e35d6648f006cba4baaf4f`。完整 patch、路径、hash 和后修评估均保留。
不扩展下一批。Python 仍是生产后端；本批 Go 变更限现有并行实现及其真实消费者。

后端执行基线 `ffd675038cafb34a6c71ccec5574a803436c61b0`，Web 基线
`8ae122d3e6180805c5bcefc4d45ae4c21e8966a7`。应用源码组合实际验收于
`ffe549eb78658d3a941fc356ba924cb4849601b6`；随后 `ecffd5030cd22295fda17838e78a9db90e253392`
仅补 CI 专用 PostgreSQL，后续收口只改文档。实际 Web master 与
[consumer pin](../../.github/consumer-web.json) 均为 `2d3bdf757361d3eabd5d5339e609850e65b83d23`。
最终 main SHA 与清理结果由外部 `delivery-complete.json` 绑定，避免文档自引用提交 SHA。

## 逐项处置

每项核对完整 diff、当前实现、活动消费者与质量价值。三组数据集测试共 52 个新增上游函数
足迹（19/13/20），逐项映射到本地合同，未照搬旧 SDK/Quart 形状、xfail 或弱断言。

| 项 | 完整上游 SHA | 处置与质量依据 | 实际本地提交（短 SHA） |
|---|---|---|---|
| 1 | `0f92353bd99bb605b526e3de314dc4167528d772` | 适配当前 PDF 库；app 自有焦点属性与主题 accent，和 #8 合并最终样式。 | `0d456f7c` |
| 2 | `4783ce9951a7d411f07ac3c0c0dac3cb08d305b9` | 原生 Ollama embed 批处理；保留严格 tags/chat、取消、超时和实例隔离。 | `0b0843ab` |
| 3 | `f4d36f708272586b6f38576922a377bedb6c6a90` | 补 run[]/run_status[]、去空白；非法非空状态拒绝，0 合法。 | `0f438d43` |
| 4 | `c7c75c0a8796fd5b212115988ea54bd8e9936508` | 保留实际流式/历史图片链，增加 raster data-image 守卫；保留 sanitizer。 | `b8069d4a` |
| 5 | `7d200d5bd7557b8ba2f4634ef613a672ae0c3f6a` | 迁移 19 个创建测试函数足迹及参数矩阵；采用本地 REST 合同。 | `0f438d43` |
| 6 | `af85aa9c7b5967e4cc6c54e8cb5eb80c67ea700e` | 适配 CLI/HTTP/ES/Infinity 元数据键及结果聚合；保留类型化授权作用域和失败语义。 | `cd6d21fa`, `0cf96074`, `3003026f` |
| 7 | `67e43e7df766950f8c557cd8314d5fe54c210816` | 有依据保留 >=3.12,<3.14、CI 3.12/3.13 和 Docker 3.12.10；不重建缺失翻译/旧发布说明。 | 保留，无空提交 |
| 8 | `ed179ce684e3d965e9ec4d088cc9ecbec820a32a` | 移除活动 blur 保存；修复旧保存响应覆盖新变量输入的竞态，真实自动保存/重开验证。 | `0d456f7c`, `2fb58a81` |
| 9 | `4b36801b53ee5a38c4bf9e54e82cacd8c2462fdf` | webhook 任务强引用与 drain、自有 Session、流关闭；LocalLLM 事件循环与客户端清理、邮件异常隔离。 | `38da164c` |
| 10 | `54c3d23513a0ff5adfec6f117ff7c20b824403dd` | 允许空 avatar；保留 null/省略合同，独立读回。 | `0f438d43` |
| 11 | `c3b38d397f136414cbaaad46f7ff4bd73f3d875d` | 迁移 13 个更新测试函数足迹；补有效 ext 的解析配置及服务端身份保护。 | `0f438d43`, `2dc0d142`, `355d55f3` |
| 12 | `b961810e79ec15a5a2fe34bc3e558123127703d6` | 已有 Zhipu OCR 实现更完整，保留并补前置拒绝回归；未照搬删能力目录。 | `0b0843ab` |
| 13 | `806414df4370e669f01a1679f1b53e048b905379` | 已有 Baidu OCR 输入校验，补零 HTTP 前置拒绝回归，保留有效 layout parsing。 | `0b0843ab` |
| 14 | `67f7d87dff12bab2ea9f1ecf5992c080896ff049` | 增加 FuturMix 目录/factory/严格 chat；动态列表和 credential check 明确 unsupported。 | `0b0843ab` |
| 15 | `3dbd874a7946db8dac74af0eafe02e121450b1f8` | 增加 DeepInfra 原生 rerank，严格 score/index/数量校验；保留隔离 CI，另补必需 Go 测试专用 PostgreSQL。 | `0b0843ab`, `ecffd503` |
| 16 | `0d2a17254cb5e4622c4dbf9310a1b957e7feb63c` | canvas_type 作为显示标签，与 canvas_category 分离；Web 实际 PUT、任意标签、空值/省略合同对齐。 | `38da164c`, `2d3bdf75` |
| 17 | `619b9717854160738e109a2a9169e898ffa36fb8` | None 缺对象明确失败，b-empty 保留；普通 ingestion/recovery 不误报成功或先调用 provider。 | `38da164c` |
| 18 | `bce11527c3595dc74e15cc8d450fea5e53033485` | 默认显示 built_in_metadata:[]，保留 dirty-only、省略不清空及显式 [] 清空语义。 | `1644a7ba` |
| 19 | `036ed5b236b1bcde9cd70e29c51ea29a9011dec3` | 迁移 20 个列表/删除测试函数足迹；保护外租户文件夹、pagerank 作用域及失败副作用。 | `0f438d43` |
| 20 | `0fb85a66bc03e0ec95bc99cb1a13c438f4587e13` | 增加 Bedrock Converse/list/eventstream、签名和实例认证/区域隔离；受控协议验证，不访问 AWS/IMDS。 | `0b0843ab` |

A/B 候选分别为 `67077412ac467ebb2eff5ac17d85f4f149387cf3` /
`0307ae74a4745611d43220fd8e1e4e1fa93caf1c`，经审查 cherry-pick；D 原提交直接快进。
G 两个候选 `0655320057965a1e25639950c3cc5e3eac28bffc`、
`d19971bf0423df6a71f3cdcfc4baedef09d7cb7d` 分别进入上表实际提交；G 树中的 D 临时
cherry-pick `79ae7708daa1bfd31c69117cc4678b6a11dbce2e` 未重复集成。Web 五个原提交直接快进。

## 组合验证与修复

所有应用测试绑定同一 `ffe549eb` 源码和上述 Web；2955 个源码/执行输入的冻结摘要为
`08c0cc78c0aa4aa8dc04f5bff689c089e45ecb81af74a47e72eff7864a49ce2d`。
锁文件、私有配置摘要、原始命令退出码、JUnit 与 stdout/stderr 在外部凭据中逐项记录。

- Python 定向单元 **675 passed、0 skipped**；真实 HTTP/SQL/对象/索引合同 **16 passed、0 skipped**；
  固定 Web 实际 API adapter 对 Python 后端 **2 passed、0 skipped**。验证标签/类别、空 avatar、
  元数据默认/省略/显式空数组，以及拒绝请求没有 SQL 写入。不是完整 core 四分片。
- Ruff、format、相关 mypy、import-linter、async DB 检查通过。
- Go `./internal` race 原命令退出 0；skip 审计首次发现旧 legacy embedding 测试缺 PostgreSQL，
  保留失败，提供专用 scratch DB 后同源码重跑整个 skills 包。组合报告明确以该包实跑替换，
  **3628 passed、43 个已声明 opt-in skips、5 个无测试包**，组合 skip 审计通过；未放宽 allowlist。
- 当前新链接 CLI/server/admin 三个程序、build/vet 通过。当前 CLI/HTTP/JWT/真实 SQL + ES 8.11.3
  与 Infinity 0.7.0 各 **12 passed、0 skipped**；当前 provider facade/HTTP/CLI/SQL
  **9 passed、0 skipped**，供应商网络是受控协议端点。
- Web 候选 79 个定向 Node 测试、38 个 Vitest、lint/build 通过。真实浏览器以产品组件和受控 API
  检查三页 PDF 定位/滚动/明暗主题、流式/历史 raster 图片、变量插入/删除/重开、20 秒自动保存和
  延迟保存响应竞态。先审阅带文件名、路由、视口标注的 contact sheet；浏览器错误数组为空。
  构建使用记录过的 Excel/pdfjs 依赖 overlay，已单独归档，不称干净 `npm ci` 构建。
- native archive 仅复用构建准备：135 个源码输入、工具链/编译器/PCRE2 和固定字典 24 个文件
  hash 核验后，新 Go 源码重新链接；未拿旧二进制替代本次消费者。

集成中保留首错并修根因：有效 `parser_config.ext` 展开后重新验证；阻止 ext 覆盖服务端
`id/tenant_id/created_by/status`；ES 普通字段聚合保留文件名中的逗号/空白，历史 tag 拆分不变。
新增失败用例分别先复现 9、8、1 项失败后通过，最终组合覆盖修复。

CI 改动提供 job 自有 PostgreSQL 16、动态宿主端口和 tmpfs 给原有必需 Go 测试。
本机同等 scratch 服务实跑及工作流 YAML/定向检查通过；未运行远程 Actions，actionlint 本机不可用。
[CI 分层规则](../../tests/README.md#ci-分层与证据) 与远程 `ci-required` 仍是最终组合门禁。

## 清理、恢复与证据边界

原始 28 项后端 WIP 有逐文件快照和 SHA；共享 PROGRESS 仅独立暂存本批追加块。
A/B/C/D/G 自有运行资源经 I 独立读回；I 的 scratch SQL、ES 索引、Infinity namespace、Minio
测试前缀、精确 Redis/集合、监听器、进程和私有 staging 再读回。自有容器按原 ID 删除；
九个借用服务保持原 ID。清理前剩余测试数据如实记录，不伪称清理前为空。

恢复 bundle 覆盖实际主干、各候选原提交和 Web；验证 bundle，并从 bundle 独立恢复到临时
mirror 核对 refs/对象后，才精确移除本批六个普通工作树和分支。七个历史 managed worktree、
SDK 树、借用 venv/模型/字典保留。恢复归档、清理读回与最终 SHA 见以下外部凭据。

本地证据目录：`/Users/xldu/.local/state/multirag/validation/20261025-batch-after-4776bfa8/`。
入口为 `delivery-complete.json`；细节为 `scope.json`、`integration/final-item-mapping.json`、
`integration/frozen-inputs.json`、`integration/combination-summary.json`、各 `accepted-*.json`、
原始检查回执/JUnit、`resources/` 及 recovery/cleanup 读回。此目录不入库。

限制：没有完整 Python/远程 CI/生产验收；真实 AWS/IAM/IMDS、付费 provider、Jina/邮件与常驻
worker 未验收。Infinity 元数据更新仍是 SDK 读改写，不声称多写原子性或逐调用取消；Go race
不证明 native C 无竞态。B 的历史 Redis 只保留最新持久身份快照与逐用例断言，Go Minio helper
无完整逐桶历史日志，最终独立验证测试前缀不存在；不扩大这些清理证据的覆盖声明。
候选静态/单测与真实消费者报告各保留原 SHA，不混称最终组合实跑。

仅本地提交，未 push、未部署；未发布 Web pin 在远程暂不可取，远程 `ci-required` 待运行。
不创建自动化、不继续下一批。
