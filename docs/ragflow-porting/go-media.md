# Go 媒体命令与能力合同

这里说明停滞的 Go 并行实现，不作为生产 Python 后端的启动或能力声明。

Go CLI 支持以下语法，标识符固定为 `model@instance@provider`：

```text
ASR WITH 'asr-model@instance@provider' AUDIO './speech.wav';
STREAM ASR WITH 'asr-model@instance@provider' AUDIO './speech.mp3';
TTS WITH 'speech-2.8-hd@voice@MiniMax' TEXT 'hello' PARAM '{"voice_setting":{"voice_id":"English_radiant_girl"},"audio_setting":{"sample_rate":32000}}' FORMAT wav SAVE './audio';
STREAM TTS WITH 'tts-model@instance@provider' TEXT 'hello';
OCR WITH 'ocr-model@instance@provider' FILE './image.png';
OCR WITH 'HunyuanOCR@instance@Gitee' URL 'https://example.com/image.png';
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
| `/audio/transcriptions` | `file` 为 base64；可选 `format`、`language`、`prompt`、`stream`、`asr_config` | `{"text":"..."}` |
| `/audio/speech` | `text`；可选 `voice`、`format`、`stream`、`tts_config` | `{"audio":"<base64>","format":"wav"}` |
| `/file/ocr` | `content` 为 base64 或 `url` 为 HTTP(S) 图片地址，二选一；可选 `language` | `{"text":"..."}` |

JSON 数字数组不能替代 base64 字符串。解码后的文件、结果与累计流输出均限制为
20 MiB，JSON 请求体也有编码长度上限。文本结果必须显式包含非 null 的 `text`；空字符串可表示静音或空白页，缺失字段不能作为成功。
音频必须包含字节及明确格式。
CLI 的 TTS 输出是 base64，可用 `-o json` 获取。只有显式 `SAVE '目录'` 时才保存，
返回绝对 `saved_path`；文件名清理模型名中的 `/` 等字符、使用唯一后缀，权限为 0600。
`PLAY` 才启动 macOS afplay 或已安装的 mpv/ffplay，失败会使命令失败；仅 PLAY 的临时文件在播放后删除。

`ASRConfig` / `TTSConfig` 新增 `params` JSON 对象，CLI 用 `PARAM '{...}'` 传入，
最多 64 KiB。配置为请求独立的深拷贝；params 不得覆盖模型、凭据、输入与 stream。
顶层 language/prompt/voice/format 仍支持；与嵌套配置中同名的显式值冲突时返回 400。
TTS `FORMAT` 优先于 params 的 format/response_format/audio_setting.format；
未显式指定时从这些固定路径读取，冲突或非法格式失败，默认 MP3。
MiniMax 将 format 合并进 audio_setting，保留 sample_rate/bitrate 等兄弟字段。

ASR/TTS 的 SSE 使用 `message` 事件携带上述结果片段，成功后由 handler 发送
`done` / `[DONE]`。错误事件携带非零 `code` 和 `message`，不再发送成功结束标记。
未产生任何结果事件的空流、部分输出后失败、格式改变、发送失败、取消和未收到结束标记都会失败；CLI 进程返回非零。
非流响应沿用 Go 的 `code/data/message` 信封；调用方必须检查业务码，不能只检查 HTTP 200。

## 模型选择与实现边界

服务复用 `userInferenceModel/getModelConfig`：从当前有效 owner 租户选择 provider 和
instance，再校验实例启用、模型启用和类型。ASR、TTS、OCR 分别要求
`speech2text`、`tts`、`ocr`。凭据、region 和 base URL 只来自 SQL 实例配置；请求不能覆盖。
每次调用使用独立配置和最长 120 秒的上下文，并保留请求取消。

`ASRDriver`、`ASRStreamDriver`、`TTSDriver`、`TTSStreamDriver`、`OCRDriver`、`OCRURLDriver` 是可选接口，
没有扩张所有 `ModelDriver` 的必选方法。目录中的模型类型并不证明驱动已经实现推理。
Gitee 实现 `OCRDriver` 和 `OCRURLDriver`：向配置的 `images/ocr` 发送 multipart
`model` 与 `image`（文件字节或 URL），把响应 `text_result` 映射为 `text`。
URL 最长 8 KiB，拒绝非 HTTP(S)、凭据和 fragment；由供应商下载图片。
MiniMax 实现 TTS 和流式 TTS：`v1/t2a_v2`，严格检查 base_resp.status_code、hex 音频、
明确格式和 final data.status=2。流式请求排除最终累计音频；兼容供应商仍返回累计音频时，
校验长度/hash 后去重，不重复追加。缺终帧、非法 JSON/hex 或业务错误不能作为成功。
FishAudio 实现 TTS、流式 TTS 和 ASR：TTS 返回原始音频，流式端点为
`v1/tts/stream/with-timestamp`，校验 SSE framing 和 audio_base64；ASR 向
`v1/asr` 发 multipart audio 字节及参数，使用 model header，显式 text 可合法为空。
该 SSE 端点按[官方协议](https://docs.fish.audio/api-reference/endpoint/openapi-v1/text-to-speech-stream-with-timestamps)
以完整事件后的正常 EOF 结束，没有独立供应商终帧；本地 API 仍在成功后发送 done。
OpenRouter 实现非流 TTS：`audio/speech`，model/input/response_format，返回原始音频。
原始音频返回校验 MIME、字节数量与请求格式；HTTP 200 JSON 错误体不算音频。
FishAudio 流式 ASR、OpenRouter 流式 TTS 及其余未实现的媒体能力返回业务码 **501**；流返回错误事件。LocalAI 和 Voyage AI 不声明媒体能力。
不搬入 panic 方法、空响应或批量重复的 unsupported 桩。
`NewModelProviderServiceWithFactory` 允许受控适配器复用同一套真实 SQL 绑定，默认构造器仍使用内置 factory。

本地验收运行真实 JWT、注册路由、HTTP、CLI 二进制及隔离 PostgreSQL；通用媒体合同
使用受控适配器，Gitee OCR 与上述语音实现运行内置驱动并控制供应商 HTTP 响应。
独立读回证明失败无 SQL 写入，并分别验证内置驱动的 unsupported 路径。
这些证据不代表真实供应商音频/OCR 或生产验收。
