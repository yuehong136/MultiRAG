# SiliconFlow TTS 本地补修

2026-10-30，在后端 `bb98552a184212350e433036e636fc5a7d37c3ca` 上收尾
[上一批第 20 项](batch-after-10bbe6b5.md)保留的两项 TTS 能力。

## 当前行为

SiliconFlow 语音请求固定使用 MP3、`sample_rate=32000`、流式响应和模型限定的默认
`<model>:anna` 音色；保留现有 base URL、超时、语速、增益和响应关闭机制。
原 `sample_rate=123` 不符合接口合同。
[官方接口文档](https://docs.siliconflow.cn/docs/api/audio-speech-post)列出的 MP3
采样率为 32000 或 44100，当前选择 32000。

目录新增 `fnlp/MOSS-TTSD-v0.5`、`FunAudioLLM/CosyVoice2-0.5B`，均声明 `tts`、
`max_tokens=26214400`、`is_tools=false`；SiliconFlow 工厂增加 TTS 标签。
其他目录条目及能力保持原值。实际链路通过现有 provider 校验、服务端重验、原子创建、
租户数值模型与默认模型解析接入 `LLMBundle`，没有新增模型身份或路由合同。

上游对照：目录为 `dcf623d60dde9620278a7827778eb2cea6f0c99a`；采样率精确后修为
`b6ee6c3116d2087882e4f0904d70b9a43d11a1f2`（单行 123 → 32000），在既定参考上限
`519e7d98a5651564d4e35d6648f006cba4baaf4f` 内。采用后修不改变上一批冻结范围，
未来批次遇到该行应核对已有覆盖，避免重复移植。

## 本地定向验证

- 修复前在原生产代码和目录上运行校正后的新增单测：12 项中 6 失败、6 通过；
  4 项定位无效采样率，2 项定位缺失目录，零收集错误。
- 修复后组合 unit：**119 通过、0 skip**，覆盖实际适配器请求、两项目录、流完成/中断/
  消费后关闭、供应商拒绝、现有 TTS 缓存及 provider 模型合同。
- PostgreSQL 与真实回环 HTTP 定向 integration：**7 通过、0 skip**，
  `REQUIRE_SERVICES=1`。覆盖目录选择、只读验证不写模型、服务端重验与原子创建、
  数值默认模型、`LLMBundle` 实际二进制流、跨租户拒绝先于网络、错误正文脱敏、
  供应商拒绝/空音频无新增模型，以及既有默认模型 backfill。
- 改动的 4 个 Python 文件 Ruff check/format 通过；当前纳管 mypy 156 文件通过；
  async DB 新增违规与双 session 均为 0。

unit 与最终 integration 输入仅有新 integration 测试的导入排序差异，后者已复跑；
生产、目录、锁与消费者 pin 输入一致。新增测试登记在 database integration 分组。
本地证据根为
`/Users/xldu/.local/state/multirag/validation/20261030-siliconflow-tts-followup`，
保存原始 runner exit、JUnit、逐文件输入、首败、合同对照及独立清理读回；
最终提交、接收、恢复 bundle、资源和分支清理以其中 `delivery-complete.json` 为准。

## 交付边界

Python 是当前生产后端；本次未修改 Go、Web 或消费者接口，Web/master 与
`.github/consumer-web.json` 保持 `6bea392bee56268ecaa47ee5b4fc946d46253c19`。
本次 provider HTTP 测试注入可信 principal，不宣称新增 JWT 或 Web 端到端验收。
音频为受控二进制夹具，未验真实供应商账号、MP3 解码/音质、克隆音色或 MOSS 双音色。
远程最终 `ci-required` 待运行；未 push、未部署，不称全量门禁或生产通过。
上一批历史协议偏差和证据边界继续保留，本次补修不追认其为通过。
