# PaddleOCR 远程 PDF 解析

模型提供商选择 `PaddleOCR`，模型类型为 `ocr`。Web 的 Settings → Model providers
中配置模型名称、算法、完整推理 URL 和可选 access token；数据集或 Parser 节点继续
选择已保存的 `模型名@paddleocr`。模型名称是本地标识，实际部署的算法由服务端决定，
选择算法不会替服务端安装或切换模型。

| 算法 | 自托管同步服务 URL 示例 | 消费的结果 |
|---|---|---|
| PaddleOCR-VL | `http://localhost:8080/layout-parsing` | `layoutParsingResults[].prunedResult.parsing_res_list` |
| PaddleOCR-VL-1.5 | `http://localhost:8080/layout-parsing` | 同上 |
| PP-StructureV3 | `http://localhost:8080/layout-parsing` | 同上，保留表格等布局标签 |
| PP-OCRv5 | `http://localhost:8080/ocr` | `ocrResults[].prunedResult.rec_texts`，坐标优先 `rec_boxes`，其次 `rec_polys` |

此入口同时支持 PaddleOCR/PaddleX 同步 JSON 服务与官方异步 Job API。
同步 URL 必须包含推理路径，请求发送 PDF Base64 和 `fileType=0`；可选 access token
沿现有合同通过 `Authorization: token …` 传递。

可先使用 [PaddleOCR 官方 AI Studio 云服务](https://aistudio.baidu.com/paddleocr/task)
验收，无需部署本地模型。登录并完成账号资料后，在「系统设置」取得 AI Studio
Access Token。官方当前提供的 Job URL 是
`https://paddleocr.aistudio-app.com/api/v2/ocr/jobs`，四个算法可使用同一 URL 和 Token，
请求中的 `model` 指定算法，不受官网当前展示的默认模型影响。
自定义网关可保留前缀和查询参数；路径以 `/api/v2/ocr/jobs` 结尾时使用 Job 协议。
其余 URL 使用同步协议，沿用服务提供的完整路径。
免费额度与文件限制以[官方配额规则](https://ai.baidu.com/ai-doc/AISTUDIO/Xmjclapam)
为准。云服务 Token 仅放本机覆盖或模型凭据中，不写入示例、日志或仓库。

Job 模式要求非空 Token，通过 `Authorization: Bearer …` 验证，使用 multipart
上传原始 PDF，发送算法和对应的 `optionalPayload`，然后轮询任务状态。
完成后下载 JSONL，按所选算法合并每行的 `ocrResults` 或 `layoutParsingResults`，
保留页序、文字和坐标。结果下载不携带 API Token；`paddleocr_timeout` 约束上传、
轮询、等待及下载的整体预算。HTTP/业务码失败、任务失败、未知状态、缺少结果 URL
和非法 JSONL 均报错，不把“任务已提交”作为解析成功。提交请求失败不会自动重试，
避免响应不确定时重复建任务。队列已满等明确拒绝可在稍后重新解析。

官方错误码 `10010` 表示任务提交队列已满；它与模型名错误 `10007`、请求参数错误
`10008` 不同。遇到 `10010` 应等待云服务恢复或使用可用的同步部署地址。增大
`paddleocr_timeout` 只延长已接受任务的等待预算，不能解除提交队列已满的拒绝。
云端仍为 `pending` 时，本地超时不会将该任务标记为推理成功。

保存格式保持兼容：`api_key` 可以是下列配置对象，服务端会保存到嵌套 JSON；已有扁平
JSON 和环境变量形式也可读取。缺省算法继续为 `PaddleOCR-VL`。

```json
{
  "paddleocr_api_url": "http://localhost:8080/ocr",
  "paddleocr_algorithm": "PP-OCRv5",
  "paddleocr_timeout": 600,
  "paddleocr_algorithm_config": {
    "use_textline_orientation": false,
    "text_rec_score_thresh": 0.0
  }
}
```

环境变量入口仍为 `PADDLEOCR_API_URL`、`PADDLEOCR_ACCESS_TOKEN`、
`PADDLEOCR_ALGORITHM`；模型实例也可读取 `PADDLEOCR_TIMEOUT`。UI 提供基础连接字段；
`paddleocr_timeout` 和 `paddleocr_algorithm_config` 可通过模型 API 配置。

算法名必须完整匹配四个支持值，地址必须为有效的 HTTP/HTTPS URL。算法参数使用
Python 的 snake_case 字段名，并按对应
服务转换为 camelCase；未知字段和错误类型会被拒绝。PP-OCRv5 不发送 Markdown、布局
或 VL 生成参数；PP-StructureV3 可配置 OCR、布局、表格、公式和印章参数；两个 VL
版本共用 VL 参数。`additional_params` 保留调用方原有的原始请求字段覆盖能力。

HTTP 错误、非 JSON、非零 `errorCode`、错误结果结构均报错。识别为空的有效页面允许
为空；缺少所选算法的结果数组不会作为成功空结果。`raw` 返回文本与位置标签，
`manual` / `pipeline` 返回文本、布局标签和位置，`paper` 将位置接在文本后。
坐标沿用既有二倍 PDF 渲染缩放；没有 OCR 坐标时保留文字并使用零坐标。布局表格继续
作为 section 消费，单独的 tables 返回值沿用空列表合同。

当前 `check_available()` 校验本地配置，保存成功并不证明服务可达、token 有效或服务端
算法匹配。实际推理在解析请求中校验；验收需要配置保存及读回、真实服务解析非空 PDF，
并核对文字、页号与位置。远程解析器初始化不加载本地 DeepDOC OCR/布局模型。

官方同步服务合同参见 [通用 OCR](https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/OCR.html)、
[PP-StructureV3](https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/PP-StructureV3.html) 和
[PaddleOCR-VL](https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/PaddleOCR-VL.html)。
云服务合同参见[官方 Job API 文档](https://ai.baidu.com/ai-doc/AISTUDIO/fml7mozw5)。
