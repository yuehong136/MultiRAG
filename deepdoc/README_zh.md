[English](./README.md) | 简体中文

# *Deep*Doc

- [*Deep*Doc](#deepdoc)
  - [1. 介绍](#1-介绍)
  - [2. 视觉处理](#2-视觉处理)
  - [3. 解析器](#3-解析器)
    - [简历](#简历)

<a name="1"></a>
## 1. 介绍

对于来自不同领域、具有不同格式和不同检索要求的大量文档，准确的分析成为一项极具挑战性的任务。*Deep*Doc 就是为了这个目的而诞生的。到目前为止，*Deep*Doc 中有两个组成部分：视觉处理和解析器。如果您对我们的OCR、布局识别和TSR结果感兴趣，您可以运行下面的测试程序。

```bash
python deepdoc/vision/t_ocr.py -h
usage: t_ocr.py [-h] --inputs INPUTS [--output_dir OUTPUT_DIR]

options:
  -h, --help            show this help message and exit
  --inputs INPUTS       Directory where to store images or PDFs, or a file path to a single image or PDF
  --output_dir OUTPUT_DIR
                        Directory where to store the output images. Default: './ocr_outputs'
```

```bash
python deepdoc/vision/t_recognizer.py -h
usage: t_recognizer.py [-h] --inputs INPUTS [--output_dir OUTPUT_DIR] [--threshold THRESHOLD] [--mode {layout,tsr}]

options:
  -h, --help            show this help message and exit
  --inputs INPUTS       Directory where to store images or PDFs, or a file path to a single image or PDF
  --output_dir OUTPUT_DIR
                        Directory where to store the output images. Default: './layouts_outputs'
  --threshold THRESHOLD
                        A threshold to filter out detections. Default: 0.5
  --mode {layout,tsr}   Task mode: layout recognition or table structure recognition
```

HuggingFace为我们的模型提供服务。如果你在下载HuggingFace模型时遇到问题，这可能会有所帮助！！

```bash
export HF_ENDPOINT=https://hf-mirror.com
```

<a name="2"></a>
## 2. 视觉处理

作为人类，我们使用视觉信息来解决问题。

  - **OCR（Optical Character Recognition，光学字符识别）**。由于许多文档都是以图像形式呈现的，或者至少能够转换为图像，因此OCR是文本提取的一个非常重要、基本，甚至通用的解决方案。

    ```bash
    python deepdoc/vision/t_ocr.py --inputs=path_to_images_or_pdfs --output_dir=path_to_store_result
    ```

    输入可以是图像或PDF的目录，或者单个图像、PDF文件。您可以查看文件夹 `path_to_store_result` ，其中有演示结果位置的图像，以及包含OCR文本的txt文件。
    
    <div align="center" style="margin-top:20px;margin-bottom:20px;">
    <img src="https://github.com/infiniflow/ragflow/assets/12318111/f25bee3d-aaf7-4102-baf5-d5208361d110" width="900"/>
    </div>

  - 布局识别（Layout recognition）。来自不同领域的文件可能有不同的布局，如报纸、杂志、书籍和简历在布局方面是不同的。只有当机器有准确的布局分析时，它才能决定这些文本部分是连续的还是不连续的，或者这个部分需要表结构识别（Table Structure Recognition，TSR）来处理，或者这个部件是一个图形并用这个标题来描述。我们有10个基本布局组件，涵盖了大多数情况：
      - 文本
      - 标题
      - 配图
      - 配图标题
      - 表格
      - 表格标题
      - 页头
      - 页尾
      - 参考引用
      - 公式
      
     请尝试以下命令以查看布局检测结果。

    ```bash
    python deepdoc/vision/t_recognizer.py --inputs=path_to_images_or_pdfs --threshold=0.2 --mode=layout --output_dir=path_to_store_result
    ```

    输入可以是图像或PDF的目录，或者单个图像、PDF文件。您可以查看文件夹 `path_to_store_result` ，其中有显示检测结果的图像，如下所示：
    <div align="center" style="margin-top:20px;margin-bottom:20px;">
    <img src="https://github.com/infiniflow/ragflow/assets/12318111/07e0f625-9b28-43d0-9fbb-5bf586cd286f" width="1000"/>
    </div>
  
  - **TSR（Table Structure Recognition，表结构识别）**。数据表是一种常用的结构，用于表示包括数字或文本在内的数据。表的结构可能非常复杂，比如层次结构标题、跨单元格和投影行标题。除了TSR，我们还将内容重新组合成LLM可以很好理解的句子。TSR任务有五个标签：
      - 列
      - 行
      - 列标题
      - 行标题
      - 合并单元格
      
    请尝试以下命令以查看布局检测结果。

    ```bash
    python deepdoc/vision/t_recognizer.py --inputs=path_to_images_or_pdfs --threshold=0.2 --mode=tsr --output_dir=path_to_store_result
    ```

    输入可以是图像或PDF的目录，或者单个图像、PDF文件。您可以查看文件夹 `path_to_store_result` ，其中包含图像和html页面，这些页面展示了以下检测结果：

    <div align="center" style="margin-top:20px;margin-bottom:20px;">
    <img src="https://github.com/infiniflow/ragflow/assets/12318111/cb24e81b-f2ba-49f3-ac09-883d75606f4c" width="1000"/>
    </div>

  - **表格自动旋转（Table Auto-Rotation）**。对于扫描的 PDF 文档，表格可能存在方向错误（旋转了 90°、180° 或 270°），
    PDF 解析器会在进行表格结构识别之前，自动使用 OCR 置信度来检测最佳旋转角度。这大大提高了旋转表格的 OCR 准确性和表格结构检测效果。
    
    该功能会评估 4 个旋转角度（0°、90°、180°、270°），并选择 OCR 置信度最高的角度。
    确定最佳方向后，会对旋转后的表格图像重新进行 OCR 识别。
    
    此功能**默认启用**。您可以通过环境变量控制：
    ```bash
    # 禁用表格自动旋转
    export TABLE_AUTO_ROTATE=false
    
    # 启用表格自动旋转（默认）
    export TABLE_AUTO_ROTATE=true
    ```
    
    或通过 API 参数控制：
    ```python
    from deepdoc.parser import PdfParser
    
    parser = PdfParser()
    # 禁用此次调用的自动旋转
    boxes, tables = parser(pdf_path, auto_rotate_tables=False)
    ```
        
<a name="3"></a>
## 3. 解析器

### PDF bbox 分批与运行选项

`RAGFlowPdfParser.parse_into_bboxes` 按选定范围分批渲染，默认每批 50 页。
`from_page` 从 0 开始且包含该页，`to_page` 不包含该页，默认上界为 100000；
输出 `page_number`、`position_tag` 和 `positions` 都使用原 PDF 的 1-based 页码。
`positions` 的坐标位于各自页面，bbox 的 `top/bottom` 保持所选范围内累计高度。
原始书签保留，切片裁图在当前窗口内生成；解析结束后整页图像不再驻留。
多栏重排使用首个选中页面的 `bbox_page_width`，不依赖最后一批的图像。

运行选项由 `common/deepdoc_config.py` 校验，通过应用配置的 `deepdoc` section
读取，沿用[配置优先级](../docs/development.md#配置与资源)。无需修改共享配置来调试：

| 选项 | 默认 | 标准环境覆盖 | 兼容环境名 |
|---|---|---|---|
| `page_batch_size` | 50，必须为正整数 | `MULTIRAG_DEEPDOC__PAGE_BATCH_SIZE` | `PDF_PARSER_PAGE_BATCH_SIZE` |
| `dla_url` | 空，使用本地布局模型 | `MULTIRAG_DEEPDOC__DLA_URL` | `DEEPDOC_URL`，其次 `TENSORRT_DLA_SVR` |

明确配置的 section 值优先于兼容环境名，空 `dla_url` 可关闭远程选择。
配置远程 DLA 时，先初始化客户端；本地布局模型加载和下载不会先行执行。
本仓没有经过验证的 `deepdoc.vision.dla_cli.DLAClient` 实现或远端协议，
需要部署方提供该可选客户端，否则立即明确报错；不能把配置了 URL 当成远端已可用。
本地布局模型路径和缺模型时的下载回退继续保留。

分批只限制当前 bbox 解析窗口的整页图像。返回列表仍保留所有 bbox 和裁图，
模型本身也占用内存；flow 的文本预览恢复仍会全文渲染。跨批的文本/长表合并粒度
可能变化，高分辨率完整 OCR 与整条 flow 的峰值需单独验收。
复现窗口驻留与 RSS 对照可运行：

```bash
uv run --no-sync python tests/manual/pdf_bbox_batch_memory.py --pages 121 --batch-size 7 --zoom 1
uv run --no-sync python tests/manual/pdf_bbox_batch_memory.py --pages 121 --batch-size 50 --zoom 1
uv run --no-sync python tests/manual/pdf_bbox_batch_memory.py --pages 121 --batch-size 1000 --zoom 1
```

每条命令在独立进程运行，输出 PDF 页数/分辨率、结果摘要、整页图像驻留和峰值 RSS。
默认用假件隔离 OCR、布局/表格推理与合并，实际执行 PDF 渲染、文字提取、bbox 与裁图；
加 `--real-models` 使用本地模型，需先具备模型资源。记录两种模式时应明确区分。

PDF、DOCX、EXCEL和PPT四种文档格式都有相应的解析器。最复杂的是PDF解析器，因为PDF具有灵活性。PDF解析器的输出包括：
  - 在PDF中有自己位置的文本块（页码和矩形位置）。
  - 带有PDF裁剪图像的表格，以及已经翻译成自然语言句子的内容。
  - 图中带标题和文字的图。
  
### MinerU 输出目录

MinerU 的 ZIP 解包后，解析器按原始文件名和净化文件名寻找 `*_content_list.json`。
优先保留根目录原名、根目录净化名、净化名同名子目录的既有顺序，再递归查找精确文件名，
优先使用当前 method/backend 目录（pipeline 的 `auto` / `ocr` / `txt`、hybrid 的
`hybrid_<method>`、VLM 的 `vlm`）。目录识别不代表新增 backend 的远程调用支持。

精确文件不存在时，支持以下回退：

- 原始或净化文档名目录本身，以及它直属的当前 method/backend 目录内，接受
  `content_list.json` 或改名前缀的 `*_content_list.json`；通用文件优先。
- 其他目录内，带前缀文件必须以原始或净化文档名加 `_`、`-`、`.` 分隔符开头，
  避免将 `report2`、`reporting` 误认为 `report`。
- 根目录或根目录直属的当前 method/backend 目录中的通用 `content_list.json`，
  仅当整个解包目录只有这一份内容结果时接受。其他文档目录下的通用文件不兜底读取。

例如 `wrapper/report/vlm/content_list.json` 中的 `images/a.png`，解析到
`wrapper/report/vlm/images/a.png`。`img_path`、`table_img_path` 和
`equation_img_path` 均以实际选中 JSON 的父目录为基准，返回绝对路径。
日志中的 `Reading output file` 记录该文件；同优先级存在多份结果时报
`Ambiguous output files`，不依赖文件系统遍历顺序；找不到归属明确的结果时报
`Missing output file`。

本地回归会实际写入 JSON 和资源文件，并以模拟 HTTP ZIP 响应走真实保存、解包和读取路径：

```bash
uv run --no-sync pytest tests/unit/test_mineru_parser_output.py tests/unit/test_mineru_output_fallback.py -q
```

这些测试不调用远程 MinerU，也不验证其模型推理、真实服务版本或生产 PDF 解析质量。

### 简历

简历是一种非常复杂的文档。由各种格式的非结构化文本构成的简历可以被解析为包含近百个字段的结构化数据。我们还没有启用解析器，因为在解析过程之后才会启动处理方法。
