# 29718497 批次的凭证、目录与安装复验

2026-11-06 收尾前批报告中的两条旧失败，并独立复验 Web 的干净依赖安装。
基线为 `3067bd930e9ed2820e870f50cb5fcf304321e3b5`；可执行组合为
`75a1a3994cb1ac8707b0fc5eed1e8fe86e12b549`，后续仅补本记录与前批入口链接。
独立 Web/master 和消费者 pin 均为 `5b577777ca8298ed916ecddff75388d5aeb6b321`，
本轮没有 Web 源码或 pin 变更。未扩展上游批次或参考上限。

## 合同与修改

OpenRouter 对照上游 `8e4fba6cd2e89f8cb66f83b9542c4435ac5cf2fd` 的原始 key/JSON
兼容，以及 `ce3402cbb99a61541539b8bcf750b2164335b929` 的编辑时保存 key 回退。
这些提交没有承诺任意额外凭证字段可进入运行时。本地 discovery、transport 与实际模型适配器
共用严格已知字段解码；旧测试注入 `opaque` 后仍要求成功，与当前合同冲突。

提交 `e3a40dd4e4bd1919bf727381acc594d49fe28c6a` 修正该测试：有效 OpenRouter
凭证继续经过探测、保存、无 key 编辑、独立 SQL 读回及解析；探测假件也使用真实解码器。
Bedrock、Azure 和 VolcEngine 原有额外字段保留断言不变。新增未知字段、错误 order 类型、
重复 key 的持久化负例，经 ID/name resolver 和 runtime 入口拒绝，供应商构造次数为零，
独立读回凭证及用量未改写；新增行事前登记并精确清理。未放宽生产解码器。

Replicate 对照 `17b5b33574e06aeafb1e0d42edcd112d487207a9` 的显式 rerank 驱动，
以及 `9871a7e0b615c235eb2d5e7a01b37c615ab01ef1` 的目录调整。当前目录与后者逐字一致，
为四项 chat、两项 embedding；目录未列 rerank 不表示显式驱动已删除。冻结上限的六个模型 ID
与能力一致，但后来的 rank/context/output/batch 元数据有差异，本轮不扩展移植或宣称文件一致。

候选 `8fc3c7c9a849e5f04831eb615ad1875959c61f91` 串行接收为
`75a1a3994cb1ac8707b0fc5eed1e8fe86e12b549`。替换旧目录断言，逐项核对六个原生 ID、
能力及拒绝 rerank 宣传；通过真实工厂验证显式 rerank 的合法零分与非法凭证/模型/端点零请求，
并用实际 CLI 消费真实目录经受控 HTTP 返回的结果。运行到的旧 HTTP 假件改用已登记监听器。
只修改四个 Go 测试文件，生产目录、驱动及锁文件均未改动。

## 本地验证与证据复用

证据根：`/Users/xldu/.local/state/multirag/validation/20261106-after297-provider-web-followup`。
`handoffs/`、`intake.json`、`validation.json`、`cleanup.json` 与 `delivery-complete.json`
索引原始命令、输入哈希、首败、JUnit、恢复验证和清理读回。

- Python：31 项实际 OpenRouter 适配器/transport 单测，11 项 SQL 集成测试全部通过，
  零失败/错误/跳过；修改文件 Ruff 与 format check 通过。run 分别为
  `01b346a2eccb4bb898d07a91612ffc67`、`97df837a9f074305b6a1c4ddc1a35f25`。
  PostgreSQL 在收集前准备，`REQUIRE_SERVICES=1`；两个 scratch 数据库及临时 overlay
  均在独立进程读回不存在。借用现有 Python 环境与本机资产，只读使用。
- Go：entity/models/CLI 的相关 unit 与 race 各 95 条（含子测试）通过，零失败/跳过；
  run 为 `c2e11ad0d57243bbba011ea81ab0707a` 和 `000bcc07cadd443bb9b90c6cf7294b2a`。
  CLI 的真实链接需要 native，因此按当前 131 个输入新构建归档，绑定 Go 1.25.14、
  Apple Clang、PCRE2、SIMDe 与锁文件；未复用来源不符的主树归档。字典仅按实际字节登记。
  缺 module、测试编译错误和缺 native 的中间失败保留，修复环境或测试后再验证。
- Web：独立 Git clone，Node 24.4.1、npm 11.9.0，从没有 node_modules 的状态实际
  `npm ci` 成功；patch-package 的两个补丁与 husky prepare 保留，源码锁文件不变。
  生产 build、143 项 Vitest、两个 Node API 文件的 7 项测试、改动 ESLint、file-size、
  i18n-types 和 bundle-size 均 raw exit 0。补验受影响的搜索、mindmap、profile、语言、
  390px 设置与 DOCX 浏览器消费者；多图按两张标注 contact sheet 审阅。

组合接收时独立恢复候选 bundle 和 Web 源码包，核对实际 commit/blob；1009 个 Go 范围输入、
131 个 native 源输入、2694 个 Web 跟踪文件与组合一致。Go 测试提交没有改 Python 输入，
Python 提交没有改 Go 输入，Web 源码/锁/pin 固定，因此复用上述同范围通过证据，未重复跑全量。
主工作区的 28 项其他 WIP 不属于干净组合的验收输入，内容与状态独立保护。

## 退役与验收边界

候选接收后只清理本轮自有 worktree/ref/git admin、独立 Web clone、安装工具、Go 模块缓存、
native 构建和临时恢复目录；保留 bundle、Web 源码包及全部原始证据。最终路径、进程组、
监听器、数据库与借用资源读回见 `cleanup.json`。未操作用户浏览器或借用服务。

Web 首轮浏览器功能验收未保存 detached Chrome 的实际 PID/启动身份，不能追认为严格资源
协议通过；原记录与 helper 保留，按唯一自有 profile 前缀和端口读回未发现残留。
随后两次专用浏览器验收补齐实际 PID/PGID、自有 WebSocket 端口与退出读回，结果通过。
本轮仍如实标记 `strictProtocolPassed=false`。前批共享 `node_modules/.package-lock.json`
改写因原始副本缺失仍不能恢复；本轮仅确认这次主 Web 的六项隐藏入口与 Git 配置前后未变。
fresh install 成功补齐可复现安装证据，不倒填旧记录。

本地验证仅覆盖上述范围。Electron 二进制下载显式关闭，未验收桌面构建/安装包、真实供应商、
部署或生产。完整远程 `ci-required` **待运行**；已 commit，未 push、未部署。
韩语完整翻译/母语审校，以及独立 Go ingestion/NATS/local parse/chunk/EXPLAIN 仍是另外的
未完成能力，本轮不将其改标为实现通过，也未派发下一批二十项。
