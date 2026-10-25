# 0fb85a66 后二十项本地集成

冻结 RAGFlow first-parent 范围：排除 `0fb85a66bc03e0ec95bc99cb1a13c438f4587e13`，
连续至 `7fb9a26623b0ed67b20c34d00b037579d71d2cb4` 共 20 项；直接后修上限
`519e7d98a5651564d4e35d6648f006cba4baaf4f`。后端基线
`202464430fb3d1a66e9fe642d797cbe2cef0ab8d`，Web 基线
`2d3bdf757361d3eabd5d5339e609850e65b83d23`。Python 仍为生产后端，Go 范围限本批明确
涉及的并行实现和消费者。完整 patch/hash、后修和逐路径审计在本批外部凭据中保留。

A/B/D/G 已串行合入实际 main，Web master 与 consumer pin 均为
`26a1fcf2a15bb2922a26e99f2d7e5b45b1916335`。Go 组合验收源码
`83e32581ad28c9cd545df0dd302fab982939544e`；随后仅格式化 CI 精确 opt-in 清单，
Python/静态复验源码 `50d711a5c7e13c4d9c91113d69ea601eab13118a`，CI AST 与所有 Go
执行输入均未改变。后续仅补本文与 PROGRESS，最终 actual main SHA 由外部
`delivery-complete.json` 绑定，避免文档自引用。

## 逐项处置

每项比较完整差异、活动消费者、失败语义和实现质量。#13 的 23 个新增函数足迹逐一映射；
已有成功 parse/index/chunk 生命周期仅复核源代码，不把历史执行重新计为本批运行证明。

| 项 | 完整上游 SHA | 处置与质量依据 | 实际本地提交 |
|---|---|---|---|
| 1 | `a7d25391dcd6beee83bf2ac1828b22d813596589` | 保留已有严格 chat/reasoning/indexed embed；规范发现 suffix，并证明非法输入在 HTTP 前拒绝、显式 0 保留。 | `50d1525d` |
| 2 | `84add43208af0b1906273f3d22fc3d6216ab069f` | 新增 HuaweiCloud 目录/factory/实例；保留 DeepSeek-V3 V1 与其他模型 V2、thinking 差异和严格向量/score。 | `e410eac5` |
| 3 | `a48bcf814db368077b9b44b2b16076280d0ade4a` | ModelScope 贯通可选 key、租户 URL、loader/SQL 准入和自定义模型列表；复用安全 transport 与 reasoning/thinking fallback。 | `c3611901`, `e410eac5` |
| 4 | `3619ceca01c82a4a540b8937e64a0654f82da5ba` | 新增 OrcaRouter 严格 chat/list 与二进制 TTS；未实现能力明确失败，实例 map 隔离。 | `e410eac5` |
| 5 | `0639dba89acf3fec49cb473bdbab6bbd60d56756` | 完整 32 路径质量核对后保留 MultiRAG 自有版本/镜像/锁与 Python 范围；不创建缺失树。 | 有据保留，无实现提交 |
| 6 | `af48a22ff48bc8652748c1596e319c18f01b0a20` | 发布初稿按本地交付账本处置；不复制上游发布日期、版本或性能宣传。 | 有据保留，无实现提交 |
| 7 | `557024e7d4097317bd6bb7726e43e872d75d5e52` | xAI 规范 suffix 一次，严格 bounded raw model list，保留真实实例/鉴权调用链。 | `50d1525d` |
| 8 | `22a3b8cdf968f16e1381ced98d59f9cfcc8f12f1` | LongCat 新增实际认证发现；显式空列表与缺失/null/坏类型分开，保留聊天限制和取消。 | `50d1525d` |
| 9 | `303221c1f409e8e848492eced14ee149cb64a74d` | Web 两种 tag 形状统一规范化，query key 带知识库 ID 集合，实际弹层隔离迟到响应且显示 0。 | `1a286264` |
| 10 | `5ae41dc1ebebcaad8daf40982d626dbdf73282fe` | 保留现有 hosted/local 身份与正确 factory 选择；新增实际 catalog 路由与 OCR/parse 消费验证，不照搬重命名。 | `50d1525d` |
| 11 | `8f0632c8d9efacbcd11aaf6e0f4cb634169bfea4` | 与 #6 连续评估，保留本地证据边界；草稿不作为发布或生产验收证明。 | 有据保留，无实现提交 |
| 12 | `187dc8a1e61c4854bc00d50d2b10c2629ac5afff` | Studio/Explore 真实保存/重开 parameter 元数据；保留合法 0、启用掩码、显式 Custom 和未知样式。 | `26a1fcf2` |
| 13 | `1f34a18242b176ff93b97a4bb53b4fdf6d7f9ec9` | 映射 23 个新增上游函数及完整矩阵；补并发/鉴权/对象读回及 StrictBool delete_all，保留本地 PATCH、parse 与去重合同。 | `c57163af` |
| 14 | `a3c6e075f6ab4f5709ce95e2e282b664f9e48c6b` | VolcEngine suffix 与 bounded strict list；保留 Ark 原 ID，不将 owned_by 拼成新身份，验证取消和实例隔离。 | `50d1525d` |
| 15 | `47626bbe6369591b7a84755357098a046b9b5c4e` | 新增 Qiniu chat/vision/discovery 与模型专属 thinking；embedding/rerank/媒体不作为已支持能力。 | `e410eac5` |
| 16 | `ae5f48f233ac9106f235eb788c503666022e2bdc` | Gitee default/china/deprecated 三 endpoint；保留已有多能力、用户自定义 URL 和实例 map 隔离。 | `50d1525d` |
| 17 | `17b5b33574e06aeafb1e0d42edcd112d487207a9` | Replicate 原生预测/轮询 rerank；修复 version 只发送 64 位 ID，完整 score 数量/finite/0/索引与终态校验。 | `50d1525d` |
| 18 | `0b000b833ebb66d06b8b0abcbb54faed1d1ef613` | 新增已鉴权 connector 详情，SQL 投影不读取 Config；真实 owner/active membership、未知/外租户统一拒绝、零写入/外连。 | `bdc5101d` |
| 19 | `ae88578451e2e445124c2f63317ce9b743cd23b6` | xAI 原生 multipart ASR、二进制 TTS/sender 与应用 SSE；保留四个 OCR 旁支正确身份，关闭/取消/错误不伪成功。 | `50d1525d` |
| 20 | `7fb9a26623b0ed67b20c34d00b037579d71d2cb4` | 保留当前 validatedHostedChatBody 的强合同；补 system/messages/类型/stream/默认温度与非法请求零 HTTP 回归。 | `50d1525d` |

B 原候选 `b67eff5e`、D 原候选 `ade0ef14`、A 原候选 `68329444`、G 原候选 `80ec72c0`
均经 I 独立完整 diff/原始证据审阅后串行 cherry-pick。共享 helper 原提交 `c3611901`
只集成一次，B 的依赖副本 `7ada913a` 未重复集成。Web 两个提交直接快进，实际 pin 固定到
完整 SHA，不使用移动 master。

## 版本与发布文字

#5 的 32 路径逐一核对：本地存在 README、admin CLI README/项目/锁、Docker 配置/说明、
主项目与锁文件共 8 路径，其余 24 路径不存在。上游主要是自身 0.25.5→0.25.6 替换，
没有依赖或运行时收益。本地保留 MultiRAG/CLI `0.9.9`、自有镜像引用和主项目 Python
3.12/3.13；不重建翻译、Helm、旧 SDK、迁移脚本或旧文档树，不同步无关锁变化。
部署时代码与镜像匹配的思路已由现有说明覆盖，机械替换会破坏项目身份。

#6/#11 为同一发布草稿的连续演进，还重写较早 IMAP/Langfuse 文字、删除旧表格条目并补充
Browser、`@tool`、base64 图片、Doc Generator 和 `chat_template_kwargs` 引用。
本地没有该发布页；保留本批逐项账本，不复制上游发布日期/版本、RAPTOR 性能宣传或把
既往能力作为本次新交付。此项不新增上游发布页，也不宣称生产已发布。

## 验证、复用与首错

- 当前组合 Python 定向单元 **50 passed**、真实 HTTP/SQL/MinIO 合同 **6 passed**，零 skip。
  覆盖严格 delete_all、鉴权、列表参数/并发、逐字节下载、删除原子性与对象/队列/索引副作用。
  I 将跨租户测试的可选业务码收紧为按端点确切业务码，并在组合中实跑。A 候选另有 100
  单元与 6 合同，保留原源码/输入回执，未与当前计数相加。
- 当前 Go `./internal/...` build/vet、CLI/server/admin 三个独立程序重新链接通过；模型定向
  race **158 个命名用例（含父/子用例）**、catalog 路由 **1 项**通过。新 provider **8**、
  现有 provider **20**、connector **15** 个实际 HTTP/JWT/SQL/CLI 子场景通过，供应商端点
  受控；对应原始 Go 报告计入 3 个父用例共 46 名，不能当作 46 个独立业务场景。
- B 候选 54 个 driver/shared race、D 候选 3,005 个模型命名用例保留原 SHA/输入与零 skip
  回执。D 的 91 文件观察层只替换 fixture server/temp 创建并补日志，I 逐文件正向比较确认
  未改断言或生产代码。该候选全模型结果不冒称新增 provider 后的最终全库运行。
- Web 候选的 282 API Node、15 定向 Node、18 定向组件测试、build/lint/i18n 与真实浏览器
  验证均保留；不同检查有重叠，不求和。Studio/Explore 保存、API/SQL 读回、重开、明暗和
  键盘操作，以及实际 ES tag 聚合、A→B 迟到响应隔离通过。零 count 的浏览器场景使用受控
  tuple 响应；真实 ES 场景为正计数。shell/model/周边 API 与 chunk 显示内容受控，核心
  chat/tag REST、JWT、SQL、ES 真实执行。浏览器后端源码为 `20246443`，其 chat/tag 路径
  在最终组合未修改；不称最终 main 全 UI 重跑。I 先一次审阅有文件名/路由/视口的 contact sheet。
- Ruff/format、适用 mypy、import-linter、async DB 检查通过。三个新增 Go live 入口精确列入
  CI opt-in，不使用前缀 skip。新 Python 矩阵保留默认 core/full-services/串行，因为 fixture
  修改进程全局资源与 Session hooks，不扩并行白名单。
- 原始 native archive SHA256 `45e7beda877c5ea8c5d8009a6e72088d82e3943abb2e9000b0b1ddd7c9db284d`
  仅复用准备；135 个源码/工具链/PCRE2 和 24 个字典输入重新核对后，当前 Go 源码重新链接。
  Darwin 证据不代表 Linux 执行或 C race 自由。

所有候选首错保留，定向修复/复跑另列。I 首轮格式检查只需展开过长的精确 opt-in set，
AST 不变；HTTP setup 首错是隔离树缺少项目根目录的 tiktoken cache，既有网络门禁拦截下载。
接入预登记、SHA256 校验的既有 cache 后 6 项通过；未放宽网络门禁或修改共享配置。
不是完整 Python/core 四分片、默认全 internal Go race、远程 CI 或生产全绿。

## 清理、恢复与留痕限制

I 独立读取 A/B/C/D/G 全部已保留 case 身份与原始失败报告，逐案验证精确 SQL DB、桶/对象、
索引/集合、Redis key、监听器、PID 和临时路径。D 的 5,929 次监听器分配均匹配关闭记录，
5,699 个不同地址及全部已登记资产再由 I 读回缺席；I 自身 12 个 Python case（含首轮 6 个
setup 失败）、15 个 SQL DB、179 个地址及 166 个 PID 也已独立读回，二进制/私有配置与
借用链接清除。此处不以历史 latest 快照或宽泛前缀替代本批精确身份。

**历史预登记证据仍有不可恢复的边界：** B 首次 focused 单测未持久化 listener 身份；C 首次
完整 app 导入在显式模型路径登记前尝试下载；D 初次仅 in-memory RoundTripper 的 helper
测试没有登记 Go PID/compiler temp（没有 HTTP/SQL/provider 任务）。后续带日志重跑和当前
资产缺席已证明，但不能回填这些最初运行的精确身份。因此当前已登记资源清理完成，不能
声称整批所有历史分配均有完整事前登记。A 早期私有 journal 曾记录测试 token，仅凭据被
脱敏并保留原内容 hash，资源身份与清理事件保留。相关限制在最终交付 JSON 中显式为 false，
不以重跑覆盖首错或伪造完整留痕。

原始 28 项 WIP 逐文件保留，PROGRESS 仅暂存本批追加块。恢复 bundle 包含两仓全部 refs；
创建/verify 后从 bundle 独立恢复 mirror，核对所有 refs/提交 tree 与 fsck，再移除本批普通
worktree/分支（含 C 的独立后端验证树）。七个历史 protected managed 树、SDK 树、借用
服务/venv/原始 native/字典与模型保留。恢复、精确最终清理与 SHA 以外部凭据读回为准。

证据目录：`/Users/xldu/.local/state/multirag/validation/20261025-batch-after-0fb85a66/`。
入口 `delivery-complete.json`，其索引绑定 `scope.json`、20 项映射、各 accepted receipt、
两份 frozen inputs、原始 exit/log/JUnit、复用依据、全已知身份清理、WIP 和 bundle 恢复。

仅本地提交，未 push、未部署。远程 `ci-required` 待运行；未发布的 Web pin 远程暂不可取。
常驻 worker、真实付费供应商、完整 Python/远程 CI 与生产未验收。不派下一批，不创建或
恢复自动化。严格历史事前登记合同未完全满足的限制不会被“本地交付完成”掩盖。
