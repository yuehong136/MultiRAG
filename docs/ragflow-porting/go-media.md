# Go 媒体命令与能力合同

这里说明停滞的 Go 并行实现，不作为生产 Python 后端的启动或能力声明。

Go CLI 支持以下语法，标识符固定为 `model@instance@provider`：

```text
ASR WITH 'asr-model@instance@provider' AUDIO './speech.wav';
STREAM ASR WITH 'asr-model@instance@provider' AUDIO './speech.mp3';
TTS WITH 'tts-model@instance@provider' TEXT 'hello';
STREAM TTS WITH 'tts-model@instance@provider' TEXT 'hello';
OCR WITH 'ocr-model@instance@provider' FILE './image.png';
CHAT WITH 'chat-model@instance@provider' AUDIO './speech.wav';
```

CLI 在本地读取文件，限制为 20 MiB；ASR 与 chat 音频只接受 WAV/MP3。
服务端不读取客户端提供的文件路径，也不下载 URL。chat 音频按
`input_audio: {data: <base64>, format: wav|mp3}` 转发，接收方必须支持该内容类型。
Google 当前适配器仍明确拒绝不支持的音频内容。

## HTTP

三个接口都注册在 `/api/v1` 的认证路由组，复用当前 JWT/API token 鉴权。
请求都必须包含 `provider_name`、`instance_name`、`model_name`：

| POST 路径 | 输入 | 成功的 `data` |
| --- | --- | --- |
| `/audio/transcriptions` | `file` 为 base64；可选 `format`、`language`、`prompt`、`stream` | `{"text":"..."}` |
| `/audio/speech` | `text`；可选 `voice`、`format`、`stream` | `{"audio":"<base64>","format":"wav"}` |
| `/file/ocr` | `content` 为 base64；可选 `language` | `{"text":"..."}` |

JSON 数字数组不能替代 base64 字符串。解码后的文件、结果与累计流输出均限制为
20 MiB，JSON 请求体也有编码长度上限。文本结果必须显式包含非 null 的 `text`；空字符串可表示静音或空白页，缺失字段不能作为成功。
音频必须包含字节及明确格式。
CLI 的 TTS 输出是 base64，可用 `-o json` 获取，不自动写入或覆盖本地音频文件。

ASR/TTS 的 SSE 使用 `message` 事件携带上述结果片段，成功后由 handler 发送
`done` / `[DONE]`。错误事件携带非零 `code` 和 `message`，不再发送成功结束标记。
未产生任何结果事件的空流、部分输出后失败、格式改变、发送失败、取消和未收到结束标记都会失败；CLI 进程返回非零。
非流响应沿用 Go 的 `code/data/message` 信封；调用方必须检查业务码，不能只检查 HTTP 200。

## 模型选择与实现边界

服务复用 `userInferenceModel/getModelConfig`：从当前有效 owner 租户选择 provider 和
instance，再校验实例启用、模型启用和类型。ASR、TTS、OCR 分别要求
`speech2text`、`tts`、`ocr`。凭据、region 和 base URL 只来自 SQL 实例配置；请求不能覆盖。
每次调用使用独立配置和最长 120 秒的上下文，并保留请求取消。

`ASRDriver`、`ASRStreamDriver`、`TTSDriver`、`TTSStreamDriver`、`OCRDriver` 是可选接口，
没有扩张所有 `ModelDriver` 的必选方法。目录中的模型类型并不证明驱动已经实现推理。
当前内置驱动没有实现这五项媒体能力：命令和路由返回业务码 **501**；流返回错误事件。
不搬入 panic 方法、空响应或批量重复的 unsupported 桩。
`NewModelProviderServiceWithFactory` 允许受控适配器复用同一套真实 SQL 绑定，默认构造器仍使用内置 factory。

本地验收使用受控媒体适配器，运行真实 JWT、注册路由、HTTP、CLI 二进制及隔离 PostgreSQL，
独立读回证明失败无 SQL 写入，并分别验证内置驱动的 unsupported 路径。
这些证据不代表真实供应商音频/OCR 或生产验收。
