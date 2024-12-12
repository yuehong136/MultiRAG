import json

from fastapi import APIRouter, UploadFile, File, HTTPException, Form
from io import BytesIO
from docx import Document
from docxtpl import DocxTemplate
import re
import base64

from api.utils.api_utils import get_json_result

router = APIRouter()

@router.post("/process_docx", summary="Word 文档占位符处理接口", response_description="返回处理后的文档和占位符 JSON 数据")
async def process_docx(file: UploadFile = File(...)):
    """
    ### POST `/v1/document/process_docx` Word 文档占位符处理接口

**功能描述**:
该接口接收用户上传的 Word 文件，对文件中的表格占位符进行解析、标准化和填充，最终生成处理后的文档与占位符的 JSON 数据。

---

### 请求体 (Request Body)

| 字段        | 类型          | 必填 | 描述                          |
|-------------|---------------|------|-------------------------------|
| `file`      | `UploadFile`  | 是   | 用户上传的 Word 文档，格式必须为 `.docx` |

---

### 响应 (Response)

#### 成功响应 (200)

- **响应格式**:
    - **`Content-Type: application/json`**
    - **示例**:
    ```json
    {
        "retcode": 0,
        "retmsg": "File processed successfully.",
        "data": {
            "placeholders": {
                "key1": "",
                "key2": ""
            },
            "file": "base64-encoded-processed-file-content"
        }
    }
    ```

- **字段说明**:
    | 字段             | 类型        | 描述                               |
    |------------------|-------------|------------------------------------|
    | `placeholders`   | `dict`      | 处理后的占位符键值对，键为占位符名称，值为空字符串 |
    | `file`           | `string`    | 处理后文档的 Base64 编码内容       |

---

#### 错误响应

| 状态码 | 错误类型              | 描述                                |
|--------|-----------------------|-------------------------------------|
| 400    | `Invalid file format` | 当上传的文件格式不是 `.docx` 时，返回此错误 |

- **示例**:
    ```json
    {
        "detail": "Invalid file format. Only .docx files are supported."
    }
    ```

---

### 主要流程

1. **文件校验**: 验证上传文件的格式是否为 `.docx`，如果不是，返回 400 错误。
2. **读取文档内容**: 使用 `python-docx` 读取上传文档中的表格内容。
3. **占位符处理**:
    - 判断和包装符合 `{{key}}` 格式的占位符。
    - 标准化占位符键名（仅保留中文字符和下划线）。
    - 填充表格内容：根据规则右填充和下填充缺失内容。
    - 为下填充的占位符添加序号后缀。
4. **文档内容更新**: 将处理后的占位符填充回文档的对应表格。
5. **返回结果**:
    - 将处理后的文档内容以 Base64 格式返回。
    - 返回占位符的 JSON 数据，方便后续填写或二次处理。

---

### 注意事项

- **文件格式**: 仅支持 `.docx` 文件格式。
- **表格占位符规则**:
    - 占位符必须以 `{{` 开始，并以 `}}` 结束。
    - 键名仅支持中文字符和下划线，其他字符会被移除。
    - 自动为填充生成的占位符添加 `_1`、`_2` 等后缀，以避免重复。
- **返回文件编码**: 处理后的文档以 Base64 编码返回，需客户端自行解码并保存为 `.docx` 文件。

---
    """
    # 验证文件名和格式
    if not file.filename.endswith(".docx"):
        raise HTTPException(status_code=400, detail="Invalid file format. Only .docx files are supported.")

    # 工具方法：判断是否是占位符格式
    def is_placeholder_format(value: str) -> bool:
        return value.strip().startswith('{{') and value.strip().endswith('}}')

    # 工具方法：包装占位符
    def wrap_placeholder(value: str) -> str:
        val = value.strip()
        return val if is_placeholder_format(val) else f"{{{{{val}}}}}"

    # 工具方法：标准化占位符键名
    def normalize_placeholder_key(key: str) -> str:
        return re.sub(r'[^\u4e00-\u9fa5_]', '', key)

    # 工具方法：标准化所有占位符
    def normalize_all_placeholders(matrix):
        key_map = {}
        rows = len(matrix)
        if rows == 0:
            return matrix, key_map

        cols = len(matrix[0])
        for r in range(rows):
            for c in range(cols):
                val = matrix[r][c].strip()
                if is_placeholder_format(val):
                    raw_key = val.strip('{').strip('}')
                    new_key = normalize_placeholder_key(raw_key)
                    key_map[raw_key] = new_key
                    matrix[r][c] = f"{{{{{new_key}}}}}"
        return matrix, key_map

    # 工具方法：填充表格（右填充 + 下填充）
    def fill_table(matrix):
        rows = len(matrix)
        if rows == 0:
            return matrix, set()

        cols = len(matrix[0])
        right_filled_cells = set()
        down_filled_cells = set()

        # 第一阶段：右填充
        for r in range(rows):
            for c in range(1, cols):
                if not matrix[r][c].strip() and matrix[r][c - 1].strip():
                    left_value = wrap_placeholder(matrix[r][c - 1])
                    matrix[r][c] = left_value
                    right_filled_cells.add((r, c))

        # 第二阶段：下填充
        for r in range(1, rows):
            for c in range(cols):
                if (r, c) not in right_filled_cells and not matrix[r][c].strip() and matrix[r - 1][c].strip():
                    top_value = wrap_placeholder(matrix[r - 1][c])
                    matrix[r][c] = top_value
                    down_filled_cells.add((r, c))

        return matrix, down_filled_cells

    # 工具方法：按列为下填充的占位符添加下划线序号
    def add_sequential_suffixes_by_column(matrix, filled_cells):
        rows = len(matrix)
        if rows == 0:
            return matrix

        max_cols = max(len(row) for row in matrix)
        for c in range(max_cols):
            placeholders_seen = {}
            for r in range(rows):
                if (r, c) in filled_cells:
                    val = matrix[r][c].strip()
                    if is_placeholder_format(val):
                        raw_key = val.strip('{').strip('}')
                        if raw_key not in placeholders_seen:
                            placeholders_seen[raw_key] = 1
                            matrix[r][c] = f"{{{{{raw_key}_1}}}}"
                        else:
                            placeholders_seen[raw_key] += 1
                            suffix_index = placeholders_seen[raw_key]
                            matrix[r][c] = f"{{{{{raw_key}_{suffix_index}}}}}"
        return matrix

    # Step 1: 读取文件
    file_content = await file.read()
    input_doc = Document(BytesIO(file_content))

    # Step 2: 处理表格
    placeholders = {}
    all_tables_original = []
    all_tables_result = []
    all_down_filled_cells = []

    for table in input_doc.tables:
        original_matrix = [[cell.text.strip() for cell in row.cells] for row in table.rows]
        all_tables_original.append(original_matrix)

    for original_matrix in all_tables_original:
        result_matrix = [row[:] for row in original_matrix]
        filled_matrix, down_filled_cells = fill_table(result_matrix)
        all_tables_result.append(filled_matrix)
        all_down_filled_cells.append(down_filled_cells)

    for idx, table_matrix in enumerate(all_tables_result):
        new_matrix, key_map = normalize_all_placeholders(table_matrix)
        all_tables_result[idx] = new_matrix

    for idx, (table_matrix, down_filled_cells) in enumerate(zip(all_tables_result, all_down_filled_cells)):
        all_tables_result[idx] = add_sequential_suffixes_by_column(table_matrix, down_filled_cells)

    # Step 3: 填充文档内容
    for t_idx, table in enumerate(input_doc.tables):
        result_matrix = all_tables_result[t_idx]
        for r_idx, row in enumerate(table.rows):
            for c_idx, cell in enumerate(row.cells):
                cell.text = result_matrix[r_idx][c_idx]

    # Step 4: 收集占位符 JSON 数据
    for result_matrix in all_tables_result:
        for row in result_matrix:
            for val in row:
                val = val.strip()
                if is_placeholder_format(val):
                    key = val.strip("{").strip("}")
                    placeholders[key] = ""

    # Step 5: 返回处理后的文档和占位符
    output_stream = BytesIO()
    input_doc.save(output_stream)
    output_stream.seek(0)

    response_data = {
        "placeholders": placeholders,
        "file": base64.b64encode(output_stream.getvalue()).decode("utf-8")
    }

    return get_json_result(retmsg="File processed successfully.", data=response_data)


@router.post("/fill_docx", summary="Word 文档填充接口", response_description="返回填充后的文档 Base64 数据")
async def fill_docx(
        file: UploadFile = File(...),
        data: str = Form(...)
):
    """
    ### POST `/v1/document/fill_docx` Word 文档填充接口

**功能描述**:
该接口接收用户上传的带占位符的 Word 文档（`.docx` 格式）和 JSON 数据，将 JSON 数据填充到文档中的占位符位置，并返回填充后的文档（Base64 编码）。

---

### 请求参数 (Request Parameters)

| 字段        | 类型          | 必填 | 描述                                    |
|-------------|---------------|------|-----------------------------------------|
| `file`      | `UploadFile`  | 是   | 用户上传的 Word 文档，格式必须为 `.docx` |
| `data`      | `string`      | 是   | JSON 格式的字符串，包含占位符对应的键值对 |

#### 示例请求 (Sample Request)
**表单数据**:
- 文件上传: `file` 上传一个包含占位符的 Word 文档。
- JSON 数据:
    ```json
    {
        "name": "张三",
        "date": "2024-12-11"
    }
    ```

---

### 响应 (Response)

#### 成功响应 (200)

- **响应格式**:
    - **`Content-Type: application/json`**
    - **示例**:
    ```json
    {
        "retmsg": "File filled successfully.",
        "data": {
            "file": "base64-encoded-filled-file-content"
        }
    }
    ```

- **字段说明**:
    | 字段       | 类型        | 描述                                     |
    |------------|-------------|------------------------------------------|
    | `retmsg`   | `string`    | 响应消息，表示处理结果。                 |
    | `data`     | `object`    | 包含填充后的文档的 Base64 编码数据。      |
    | `file`     | `string`    | Base64 编码的填充文档数据，需解码后保存为 `.docx` 文件 |

#### 错误响应

| 状态码 | 错误类型                     | 描述                                     |
|--------|------------------------------|------------------------------------------|
| 400    | `Invalid file format`        | 上传文件格式不是 `.docx`                 |
| 400    | `Invalid JSON data provided` | 提供的 `data` 不是有效的 JSON 格式       |
| 500    | `Internal server error`      | 填充过程中出现意外错误                   |

- **示例**:
    ```json
    {
        "detail": "Invalid file format. Only .docx files are supported."
    }
    ```

---

### 主要流程

1. **文件校验**:
    - 验证上传文件是否为 `.docx` 格式，如果格式错误，返回 400 错误。
2. **解析 JSON 数据**:
    - 使用 Python 内置的 `json.loads` 解析上传的 JSON 数据。
    - 如果 JSON 数据格式错误，返回 400 错误。
3. **填充文档**:
    - 使用 `python-docx-template` 将 JSON 数据渲染到文档中的占位符。
    - 占位符的格式需为 `{{key}}`。
4. **生成 Base64 编码**:
    - 将填充后的文档保存到内存流中，并转换为 Base64 编码字符串。
5. **返回响应**:
    - 返回包含 Base64 编码文档的 JSON 数据，客户端可解码后保存为 `.docx` 文件。

---

### 注意事项

- **占位符格式**:
    - 文档中的占位符需遵循 `{{key}}` 格式，`key` 必须与 JSON 数据中的键匹配。
- **文件格式限制**:
    - 仅支持 `.docx` 格式的 Word 文件，其他格式会返回错误。
- **Base64 文档解码**:
    - 响应返回的文档内容为 Base64 编码，客户端需解码后保存为 `.docx` 文件以查看填充结果。
- **异常处理**:
    - 如果填充过程中发生未预料的错误，将返回 500 错误，并包含具体错误描述。

---
    """
    # 验证文件类型
    if not file.filename.endswith(".docx"):
        raise HTTPException(status_code=400, detail="Invalid file format. Only .docx files are supported.")

    try:
        # Step 1: 解析 JSON 数据
        fill_data = json.loads(data)

        # Step 2: 读取上传的文件
        file_content = await file.read()
        input_doc = BytesIO(file_content)
        doc = DocxTemplate(input_doc)

        # Step 3: 使用数据填充文档
        doc.render(fill_data)

        # Step 4: 保存填充后的文档到内存流
        output_stream = BytesIO()
        doc.save(output_stream)
        output_stream.seek(0)

        # Step 5: 将文档转为 Base64 数据
        base64_encoded_file = base64.b64encode(output_stream.getvalue()).decode("utf-8")

        # Step 6: 返回结果
        response_data = {
            "file": base64_encoded_file
        }
        return get_json_result(retmsg="File filled successfully.", data=response_data)

    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON data provided.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred: {str(e)}")