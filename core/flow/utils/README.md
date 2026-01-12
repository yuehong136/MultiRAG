# core/flow/utils - 组件纯函数提取工具

## 📖 目录说明

此目录包含 `core/flow/` 各组件的**纯函数提取版本**，去除 Canvas/DSL/Graph 框架依赖，可直接在 analyze_v2 等场景中使用。

## 📁 文件对应关系

```
core/flow/utils/
├── parser_utils.py              ← 对应 core/flow/parser/
├── splitter_utils.py            ← 对应 core/flow/splitter/
├── hierarchical_merger_utils.py ← 对应 core/flow/hierarchical_merger/
└── extractor_utils.py           ← 对应 core/flow/extractor/
```

**原则：一个 utils 文件对应一个组件目录**

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
   - 更新核心逻辑
   - 更新参考注释（行号）
   - 更新错误处理逻辑
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
   
   以 `table_context_size` 和 `image_context_size` 为例，完整的更新链路：
   
   ```
   1. core/flow/parser/parser.py        # 原组件添加参数
   2. core/flow/utils/parser_utils.py   # utils 同步参数
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
   
   **示例：添加 Splitter 参数（如 children_delimiters）**
   
   以 `children_delimiters`（child-parent chunking）为例：
   
   ```
   1. core/flow/splitter/splitter.py    # 原组件添加参数
   2. core/flow/utils/splitter_utils.py # utils 同步参数
   3. core/nlp/__init__.py              # tokenize_chunks 添加参数
   4. core/app/naive.py                 # 传统解析器同步
   5. api/apps/document_app.py          # SplitterConfig 模型
   6. core/svr/task_executor.py         # run_analyze_v2_task 函数
   ```

### 注释规范

每个函数都标注了参考来源：

```python
async def parse_audio(...):
    """
    音频解析（参考 core/flow/parser/parser.py._audio 第 598-615 行）
                ↑                              ↑           ↑
             组件文件                        方法名      行号范围
    """
```

**修改 core/flow 后记得更新行号！**

## 📚 使用示例

### 基础使用

```python
from core.flow.utils import parse_file, split_chunks

# 解析（使用默认 deepdoc）
parsed = await parse_file(filename, binary, tenant_id)

# 切分
chunks = await split_chunks(parsed, overlapped_percent=0.1)
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

# Parser → Splitter → HierarchicalMerger
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

