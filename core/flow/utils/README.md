# core/flow/utils - Canvas-free 组件 Facade

## 📖 目录说明

此目录包含 `core/flow/` 各组件的 **Canvas-free facade**，去除 Canvas/DSL/Graph 调用成本，可直接在 analyze_v2 等场景中使用。

维护原则已经从“复制一份核心逻辑”调整为：

1. **优先复用正式组件运行时**，例如 `parser_utils.py` 直接分发到 `core.flow.parser.Parser`，`token_chunker_utils.py` 复用 `core.flow.chunker.TokenChunker` 的内部公共函数，`title_chunker_utils.py` 复用 `core.flow.chunker.title_chunker.TitleChunker`。
2. **utils 只做配置归一化和返回契约适配**，不要在 utils 中重新实现 parser/chunker 的核心算法。
3. 如果正式组件新增参数、返回字段或文件类型，先检查 facade 是否已经通过正式组件自动继承；只有配置入口、调用方传参或 analyze_v2 返回契约需要补齐时才修改 utils。

## 📁 文件对应关系

```
core/flow/utils/
├── parser_utils.py              ← Canvas-free facade for core/flow/parser/
├── token_chunker_utils.py       ← Canvas-free facade for core/flow/chunker/
├── title_chunker_utils.py       ← Canvas-free facade for core/flow/chunker/title_chunker/
└── extractor_utils.py           ← Canvas-free facade for core/flow/extractor/
```

**原则：一个 utils 文件对应一个组件目录，但不要复制组件核心算法。**

## 🔧 维护指南

### 当 core/flow 组件更新时

1. **定位对应的 utils 文件**
   ```
   core/flow/parser/parser.py 更新
     ↓
   core/flow/utils/parser_utils.py 需要同步
   ```

2. **检查变更内容**
   - 核心算法是否改变？
   - 参数签名是否变化？
   - 返回格式是否调整？
   - 依赖库 API 是否有更新？（如 check_installation 返回值变化）

3. **同步更新 utils**
   - 优先确认是否已通过正式组件自动继承
   - 更新配置归一化、文件类型路由或调用方传参
   - 更新 analyze_v2 返回契约适配
   - 测试验证

4. **⚠️ 同步更新相关调用方**
   
   除了 utils 文件，以下文件也可能需要同步更新：
   
   | 文件 | 更新内容 | 何时需要更新 |
   |------|----------|--------------|
   | `api/apps/document_app.py` | `AnalyzeDocumentRequest` 模型 | 新增/修改解析器参数时 |
   | `core/svr/task_executor.py` | `run_analyze_v2_task` 函数 | 新增/修改解析器参数时 |
   | `core/app/naive.py` | chunk 函数 | 修改解析逻辑时 |
   | `core/app/paper.py` | chunk 函数 | 修改解析逻辑时 |
   | `core/app/book.py` | chunk 函数 | 修改解析逻辑时 |
   | `core/app/picture.py` | chunk 函数 | 修改图片解析时 |
   | `api/db/db_models.py` | `parser_config` 默认值 | 新增配置字段时 |
   | `api/db/services/document_service.py` | 上传解析配置 | 新增配置字段时 |
   | `api/utils/api_utils.py` | `get_parser_config` 函数 | 新增配置字段时 |
   
   **示例：添加新的解析器参数**
   
   以 `table_context_size` 和 `image_context_size` 为例，完整的检查链路：
   
   ```
   1. core/flow/parser/parser.py        # 原组件添加参数
   2. core/flow/utils/parser_utils.py   # facade 配置入口是否需要透传
   3. core/nlp/__init__.py              # 添加 attach_media_context 函数
   4. core/app/naive.py                 # 传统解析器同步
   5. core/app/paper.py                 # 传统解析器同步
   6. core/app/book.py                  # 传统解析器同步
   7. core/app/picture.py               # 图片解析器同步
   8. api/db/db_models.py               # 数据库默认值（可选）
   9. api/db/services/document_service.py # 服务层默认值（可选）
   10. api/utils/api_utils.py           # API 工具函数（可选）
   11. api/apps/document_app.py         # API 请求模型
   12. core/svr/task_executor.py        # 任务执行器
   ```
   
   **示例：添加 TokenChunker 参数（如 children_delimiters）**
   
   以 `children_delimiters`（child-parent chunking）为例：
   
   ```
   1. core/flow/chunker/token_chunker.py      # 原组件添加参数
   2. core/flow/utils/token_chunker_utils.py  # utils 同步参数
   3. core/nlp/__init__.py              # tokenize_chunks 添加参数
   4. core/app/naive.py                 # 传统解析器同步
   5. api/apps/document_app.py          # TokenChunker 请求模型
   6. core/svr/task_executor.py         # run_analyze_v2_task 函数
   ```

### Facade 规范

`parser_utils.py` 不再维护 `_pdf`、`_word`、`_image` 等方法的复制实现。新增文件类型或解析参数时，应优先更新 `ParserParam` / `Parser`，然后在 `parse_file()` 的配置归一化中补齐 direct-call 入参。

`token_chunker_utils.py` 和 `title_chunker_utils.py` 已经跟随 Pipeline 重构，分别复用 `TokenChunker` / `TitleChunker` 的运行时语义。不要恢复旧组件工具实现。

`extractor_utils.py` 复用正式 `Extractor` 的 chunk 迭代和 prompt 渲染，但 analyze_v2 主链路仍使用 `task_executor.py` / `pipeline_analysis_service.py` 中更完整的元数据提取能力。

> ⚠️ 注意：parser.py 已支持 MinerU、PaddleOCR 和 Docling 三种解析器。
> MinerU 和 PaddleOCR 使用 `LLMBundle` + `LLMType.OCR` 获取模型，支持 `模型名@mineru` / `模型名@paddleocr` 格式。
> Docling 支持本地模式和外部服务器模式（通过 `DOCLING_SERVER_URL` 环境变量）。

## 📚 使用示例

### 基础使用

```python
from core.flow.utils import parse_file, split_chunks

# 解析（使用默认 deepdoc）
parsed = await parse_file(filename, binary, tenant_id)

# 切分
chunks = await split_chunks(parsed, overlapped_percent=0.1)
```

### 使用 MinerU 解析（OCR 模型）

```python
from core.flow.utils import parse_file

# 方式1：使用默认配置的 MinerU 模型
parsed = await parse_file(
    filename="document.pdf",
    binary=file_content,
    tenant_id=tenant_id,
    pdf_config={
        "parse_method": "mineru",
        "output_format": "json"
    }
)

# 方式2：指定 MinerU 模型名（mineru@模型名 格式）
parsed = await parse_file(
    filename="document.pdf",
    binary=file_content,
    tenant_id=tenant_id,
    pdf_config={
        "parse_method": "mineru@my-mineru-model",
        "output_format": "json",
        "mineru_parse_method": "raw"  # 可选：raw/ocr
    }
)

# 方式3：模型名@mineru 格式也支持
parsed = await parse_file(
    filename="document.pdf",
    binary=file_content,
    tenant_id=tenant_id,
    pdf_config={
        "parse_method": "my-mineru-model@mineru",
        "output_format": "json"
    }
)
```

### 使用 PaddleOCR 解析

```python
from core.flow.utils import parse_file

# 方式1：使用默认配置的 PaddleOCR 模型
parsed = await parse_file(
    filename="document.pdf",
    binary=file_content,
    tenant_id=tenant_id,
    pdf_config={
        "parse_method": "paddleocr",
        "output_format": "json"
    }
)

# 方式2：指定 PaddleOCR 模型名（模型名@paddleocr 格式）
parsed = await parse_file(
    filename="document.pdf",
    binary=file_content,
    tenant_id=tenant_id,
    pdf_config={
        "parse_method": "my-paddleocr-model@paddleocr",
        "output_format": "json",
        "paddleocr_parse_method": "raw"  # 可选：raw/manual/paper
    }
)
```

### 使用 Docling 解析

```python
from core.flow.utils import parse_file

# 方式1：使用本地 Docling（需要 pip install docling）
parsed = await parse_file(
    filename="document.pdf",
    binary=file_content,
    tenant_id=tenant_id,
    pdf_config={
        "parse_method": "docling",
        "output_format": "json",
        "docling_parse_method": "raw"  # 可选：raw/manual/paper
    }
)

# 方式2：使用外部 Docling 服务器（设置 DOCLING_SERVER_URL 环境变量）
# export DOCLING_SERVER_URL=http://docling-server:5001
parsed = await parse_file(
    filename="document.pdf",
    binary=file_content,
    tenant_id=tenant_id,
    pdf_config={
        "parse_method": "docling",
        "output_format": "json"
    }
)
```

### 使用 TCADP parser（腾讯云 ADP）

```python
from core.flow.utils import parse_file

# PDF 使用 TCADP 解析
parsed = await parse_file(
    filename="document.pdf",
    binary=file_content,
    tenant_id=tenant_id,
    pdf_config={
        "parse_method": "tcadp parser",
        "output_format": "json",
        "table_result_type": "1",
        "markdown_image_response_type": "1"
    }
)

# Excel 使用 TCADP 解析
parsed = await parse_file(
    filename="data.xlsx",
    binary=file_content,
    tenant_id=tenant_id,
    excel_config={
        "parse_method": "tcadp parser",
        "output_format": "html"
    }
)

# PPT 使用 TCADP 解析（支持 .ppt 和 .pptx）
parsed = await parse_file(
    filename="presentation.pptx",
    binary=file_content,
    tenant_id=tenant_id,
    slides_config={
        "parse_method": "tcadp parser",
        "output_format": "json"
    }
)
```

### 为图片和表格添加上下文

```python
from core.flow.utils import parse_file

# PDF 解析时为表格和图片添加周围文本上下文
parsed = await parse_file(
    filename="document.pdf",
    binary=file_content,
    tenant_id=tenant_id,
    pdf_config={
        "parse_method": "deepdoc",
        "output_format": "json",
        "table_context_size": 256,  # 表格前后各添加 256 tokens 的上下文
        "image_context_size": 128   # 图片前后各添加 128 tokens 的上下文
    }
)

# Word 解析时添加上下文
parsed = await parse_file(
    filename="document.docx",
    binary=file_content,
    tenant_id=tenant_id,
    word_config={
        "output_format": "json",
        "table_context_size": 256,
        "image_context_size": 128
    }
)
```

### Markdown 解析（支持 delimiter）

```python
from core.flow.utils import parse_file

# 使用自定义分隔符解析 Markdown
parsed = await parse_file(
    filename="document.md",
    binary=file_content,
    tenant_id=tenant_id,
    markdown_config={
        "output_format": "json",
        "delimiter": "\n\n",  # 按双换行分割段落
        "table_context_size": 256,
        "image_context_size": 128
    }
)
```

### Child-Parent Chunking（子父块切分）

```python
from core.flow.utils import parse_file, split_chunks

# 解析文档
parsed = await parse_file(
    filename="document.pdf",
    binary=file_content,
    tenant_id=tenant_id
)

# 使用 children_delimiters 实现 child-parent chunking
# 父块用于提供完整上下文，子块用于精确检索
chunks = await split_chunks(
    parsed,
    chunk_token_size=512,
    overlapped_percent=0.1,
    children_delimiters=["\n\n", "。", "！", "？"]  # 子块分隔符
)

# 返回结果中每个子块包含 "mom" 字段指向其父块
# [
#     {"text": "子块1内容", "mom": "父块完整内容..."},
#     {"text": "子块2内容", "mom": "父块完整内容..."},
#     ...
# ]
```

**子父块切分的优势**：
- 子块：较小的文本片段，用于精确语义检索
- 父块：保留完整上下文，提供给 LLM 生成更准确的回答

### 高级组合

```python
from core.flow.utils import (
    parse_file, 
    split_chunks, 
    hierarchical_merge
)

# Parser → TokenChunker → TitleChunker
parsed = await parse_file(...)
chunks = await split_chunks(parsed, ...)
hierarchy = await hierarchical_merge(chunks, ...)
```

## ⚠️ 重要提示

### 不要直接修改此目录文件

**正确流程：**
1. 先修改 `core/flow/xxx/` 原组件
2. 再同步到 `core/flow/utils/xxx_utils.py`

**原因：**
- 原组件是真理来源（Canvas/Workflow 使用）
- utils 是提取版本（analyze_v2 使用）
- 保持一致性很重要

### 添加新功能

**示例：添加新的解析器类型**

1. 在 `core/flow/parser/parser.py` 添加 `_video()` 方法
2. 在 `parser_utils.py` 添加 `FlowParser.parse_video()` 方法
3. 在 `parse_file()` 中添加文件类型判断

## 🧪 测试

```bash
# 测试所有 utils
python -m pytest tests/test_flow_utils.py

# 测试特定组件
python -m pytest tests/test_flow_utils.py -k "parser"
python -m pytest tests/test_flow_utils.py -k "splitter"
```

## 🎯 设计原则

1. **简单优于复杂**：去除框架，保留核心
2. **明确对应关系**：一个组件 → 一个 utils
3. **便于追踪**：注释标注参考来源
4. **易于维护**：清晰的目录结构
