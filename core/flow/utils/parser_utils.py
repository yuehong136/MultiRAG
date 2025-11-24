# coding=utf-8
"""
core/flow/parser 的纯函数提取

对应组件：core/flow/parser/parser.py
参考来源：Parser 类的各个 _pdf、_audio、_word 等方法

无需 Canvas/DSL/Graph 依赖，直接使用核心解析逻辑

@project: multirag
@date: 2025-11-10
"""
import asyncio
import logging
import tempfile
import os
import re
from io import BytesIO
from typing import Literal
from functools import reduce

import trio
import numpy as np
from PIL import Image

from api.db import LLMType
from api.db.db_models import db_connection
from api.db.services.llm_service import LLMBundle
from deepdoc.parser import ExcelParser
from deepdoc.parser.pdf_parser import PlainParser, RAGFlowPdfParser, VisionParser
from deepdoc.parser.mineru_parser import MinerUParser
from deepdoc.parser.ppt_parser import RAGFlowPptParser
from deepdoc.parser.tcadp_parser import TCADPParser
from tika import parser as tika_parser
from core.app.naive import Docx, Markdown as MarkdownParser
from core.nlp import concat_img
from deepdoc.vision import OCR
from core.llm.cv import Base as VLM

logger = logging.getLogger(__name__)


def _get_running_backend():
    """检测当前运行的异步后端（trio 或 asyncio）"""
    try:
        # 检查是否在 trio 环境
        trio.lowlevel.current_task()
        return "trio"
    except RuntimeError:
        pass
    
    try:
        # 检查是否在 asyncio 环境
        asyncio.get_running_loop()
        return "asyncio"
    except RuntimeError:
        pass
    
    return None


async def _to_thread(func, *args, **kwargs):
    """兼容 trio 和 asyncio 的 to_thread"""
    backend = _get_running_backend()
    
    if backend == "trio":
        return await trio.to_thread.run_sync(lambda: func(*args, **kwargs))
    elif backend == "asyncio":
        return await asyncio.to_thread(func, *args, **kwargs)
    else:
        # 没有事件循环，同步调用
        return func(*args, **kwargs)


class FlowParser:
    """
    core/flow/parser 的独立版本 - 不依赖 Canvas/Graph
    
    提取 core/flow/parser/parser.py 的核心逻辑，去除框架依赖
    
    支持的文件类型：
    - PDF (deepdoc/plain_text)
    - Word (json/markdown)
    - Excel (html/json/markdown)
    - PPT (json)
    - 音频 (text)
    - 视频 (text)
    - 图片 (ocr/vlm)
    - Markdown (json/text)
    """
    
    @staticmethod
    async def parse_audio(
        filename: str,
        binary: bytes,
        tenant_id: str,
        callback=None
    ) -> dict:
        """
        音频解析（参考 core/flow/parser/parser.py._audio 第 412-429 行）
        
        Returns:
            {"output_format": "text", "text": "转录文本"}
        """
        if callback:
            callback(0.1, "Start to work on an audio.")
        
        _, ext = os.path.splitext(filename)
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmpf:
            tmpf.write(binary)
            tmpf.flush()
            tmp_path = os.path.abspath(tmpf.name)
        
        try:
            # 检查音频文件信息
            audio_size = os.path.getsize(tmp_path)
            logger.info(f"[AUDIO] Audio file: {filename}, size: {audio_size} bytes, path: {tmp_path}")
            
            with db_connection() as db:
                seq2txt_mdl = LLMBundle(db, tenant_id, LLMType.SPEECH2TEXT)
            
            logger.info(f"[AUDIO] Calling SPEECH2TEXT model: {seq2txt_mdl.model_name if hasattr(seq2txt_mdl, 'model_name') else 'unknown'}")
            
            try:
                txt = await _to_thread(seq2txt_mdl.transcription, tmp_path)
            except Exception as e:
                logger.error(f"[AUDIO] Transcription API error: {e}")
                raise
            
            if txt is None:
                txt = ""
                logger.warning(f"[AUDIO] Transcription returned None for {filename}")
            
            logger.info(f"[AUDIO] Transcription result length: {len(txt)}, content: {txt[:200] if txt else '(empty)'}")
            
            if not txt:
                logger.error(f"[AUDIO] Transcription failed or returned empty for {filename} (size={audio_size} bytes)")
                # 返回友好提示而不是空字符串
                return {
                    "output_format": "text", 
                    "text": f"[音频转录失败] 文件：{filename}，大小：{audio_size} bytes。可能原因：1) 音频格式不支持 2) 音频内容无法识别 3) API 调用失败"
                }
            
            return {"output_format": "text", "text": txt}
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
    
    @staticmethod
    async def parse_pdf(
        filename: str,
        binary: bytes,
        tenant_id: str,
        parse_method: Literal["deepdoc", "plain_text", "mineru", "tcadp parser"] | str = "deepdoc",
        output_format: Literal["json", "markdown"] = "json",
        lang: str = "Chinese",
        callback=None,
        **method_kwargs
    ) -> dict:
        """
        PDF 解析（参考 core/flow/parser/parser.py._pdf 第 214-300 行）
        
        Args:
            parse_method: 
                - deepdoc: 深度布局解析（保留位置、表格、图片）
                - plain_text: 纯文本解析（快速）
                - mineru: MinerU 解析（需要安装 mineru）
                - tcadp parser: 腾讯云 ADP 解析（支持定位信息）
                - 其他: VLM 模型名称（如 "qwen-vl-plus"）
            output_format: json（保留位置） 或 markdown
            lang: 语言（VLM 模式使用）
            method_kwargs: 解析器特定参数（如 table_result_type、mineru_api 等）
        
        Returns:
            {"output_format": "json", "json": [bboxes with positions]}
        """
        if callback:
            callback(0.1, "Start to work on a PDF.")
        
        method = (parse_method or "deepdoc").lower()
        
        if method == "deepdoc":
            parser = RAGFlowPdfParser()
            bboxes = await _to_thread(parser.parse_into_bboxes, binary, callback)
        
        elif method == "plain_text":
            parser = PlainParser()
            lines, _ = await _to_thread(parser, binary)
            bboxes = [{"text": t} for t, _ in lines]
        
        elif method == "mineru":
            # MinerU 解析（参考第 223-243 行）
            mineru_executable = (
                method_kwargs.get("mineru_executable")
                or os.environ.get("MINERU_EXECUTABLE", "mineru")
            )
            mineru_api = (
                method_kwargs.get("mineru_api")
                or os.environ.get("MINERU_APISERVER", "http://host.docker.internal:9987")
            )
            mineru_output_dir = method_kwargs.get("mineru_output_dir") or os.environ.get("MINERU_OUTPUT_DIR", "")
            mineru_delete_output = method_kwargs.get("mineru_delete_output")
            if mineru_delete_output is None:
                mineru_delete_output = bool(int(os.environ.get("MINERU_DELETE_OUTPUT", 1)))
            else:
                if isinstance(mineru_delete_output, str):
                    mineru_delete_output = mineru_delete_output.lower() not in {"0", "false", "no"}
                else:
                    mineru_delete_output = bool(mineru_delete_output)
            pdf_parser = MinerUParser(mineru_path=mineru_executable, mineru_api=mineru_api)
            
            if not pdf_parser.check_installation():
                raise RuntimeError("MinerU not found or server not accessible. Please install it via: pip install -U 'mineru[core]'.")
            
            lines, _ = await _to_thread(
                pdf_parser.parse_pdf,
                filepath=filename,
                binary=binary,
                callback=callback,
                output_dir=mineru_output_dir,
                delete_output=mineru_delete_output
            )
            
            bboxes = []
            for t, poss in lines:
                box = {
                    "image": pdf_parser.crop(poss, 1),
                    "positions": [[pos[0][-1], *pos[1:]] for pos in pdf_parser.extract_positions(poss)],
                    "text": t,
                }
                bboxes.append(box)
        
        elif method == "tcadp parser":
            # ADP is a document parsing tool using Tencent Cloud API
            table_result_type = method_kwargs.get("table_result_type", "1")
            markdown_image_response_type = method_kwargs.get("markdown_image_response_type", "1")
            tcadp_parser = TCADPParser(
                table_result_type=table_result_type,
                markdown_image_response_type=markdown_image_response_type
            )
            sections, _ = await _to_thread(
                tcadp_parser.parse_pdf,
                filepath=filename,
                binary=binary,
                callback=callback,
                file_type="PDF",
                file_start_page=1,
                file_end_page=1000
            )
            bboxes = []
            for section, position_tag in sections:
                if position_tag:
                    # Extract position information from TCADP's position tag
                    # Format: @@{page_number}\t{x0}\t{x1}\t{top}\t{bottom}##
                    match = re.match(r"@@([0-9-]+)\t([0-9.]+)\t([0-9.]+)\t([0-9.]+)\t([0-9.]+)##", position_tag)
                    if match:
                        pn, x0, x1, top, bott = match.groups()
                        bboxes.append({
                            "page_number": int(pn.split('-')[0]),  # Take the first page number
                            "x0": float(x0),
                            "x1": float(x1),
                            "top": float(top),
                            "bottom": float(bott),
                            "text": section
                        })
                    else:
                        # If no position info, add as text without position
                        bboxes.append({"text": section})
                else:
                    bboxes.append({"text": section})
        
        else:
            # VLM 模式（参考第 244-251 行）
            # parse_method 是 VLM 模型名称（如 "qwen-vl-plus"）
            with db_connection() as db:
                vision_model = LLMBundle(db, tenant_id, LLMType.IMAGE2TEXT, llm_name=parse_method, lang=lang)
            
            lines, _ = await _to_thread(VisionParser(vision_model=vision_model), binary, callback)
            
            bboxes = []
            for t, poss in lines:
                for pn, x0, x1, top, bott in RAGFlowPdfParser.extract_positions(poss):
                    bboxes.append({
                        "page_number": int(pn[0]),
                        "x0": float(x0),
                        "x1": float(x1),
                        "top": float(top),
                        "bottom": float(bott),
                        "text": t
                    })
        
        if output_format == "json":
            return {"output_format": "json", "json": bboxes}
        elif output_format == "markdown":
            mkdn = ""
            for b in bboxes:
                if b.get("layout_type", "") == "title":
                    mkdn += "\n## "
                if b.get("layout_type", "") == "figure":
                    mkdn += "\n![Image]({})".format(VLM.image2base64(b.get("image"))) if b.get("image") else ""
                    continue
                mkdn += b.get("text", "") + "\n"
            return {"output_format": "markdown", "markdown": mkdn}
    
    @staticmethod
    async def parse_excel(
        filename: str,
        binary: bytes,
        output_format: Literal["html", "json", "markdown"] = "html",
        callback=None,
        *,
        parse_method: str = "deepdoc",
        **method_kwargs
    ) -> dict:
        """
        Excel 解析（参考 core/flow/parser/parser.py._spreadsheet 第 301-313 行）
        
        Returns:
            {"output_format": "html", "html": "<table>..."}
        """
        if callback:
            callback(0.1, "Start to work on a Spreadsheet.")
        
        spreadsheet_parser = ExcelParser()
        
        if output_format == "html":
            htmls = await _to_thread(spreadsheet_parser.html, binary, 1000000000)
            return {"output_format": "html", "html": htmls[0]}
        elif output_format == "json":
            texts = await _to_thread(spreadsheet_parser, binary)
            return {"output_format": "json", "json": [{"text": txt} for txt in texts if txt]}
        elif output_format == "markdown":
            markdown = await _to_thread(spreadsheet_parser.markdown, binary)
            return {"output_format": "markdown", "markdown": markdown}
    
    @staticmethod
    async def parse_word(
        filename: str,
        binary: bytes,
        output_format: Literal["json", "markdown"] = "json",
        callback=None
    ) -> dict:
        """
        Word 文档解析（参考 core/flow/parser/parser.py._word 第 314-327 行）
        
        Returns:
            {"output_format": "json", "json": [sections with images]}
        """
        if callback:
            callback(0.1, "Start to work on a Word Processor Document")
        
        # Check extension for .doc support via Tika
        ext = filename.lower().split(".")[-1] if "." in filename else ""
        
        if ext == "doc":
            try:
                # Use Tika for .doc files
                # Ensure binary is bytes
                if hasattr(binary, 'read'):
                     binary_data = binary.read()
                else:
                     binary_data = binary
                     
                doc_parsed = await _to_thread(tika_parser.from_buffer, binary_data)
                content = doc_parsed.get('content', '') or ''
                
                # Simple processing for .doc content
                sections = [s.strip() for s in content.split('\n') if s.strip()]
                
                if output_format == "json":
                    json_sections = [{"text": s, "image": None} for s in sections]
                    return {"output_format": "json", "json": json_sections}
                else:
                    return {"output_format": "markdown", "markdown": "\n\n".join(sections)}
            except Exception as e:
                error_msg = str(e)
                if "ContentTooShortError" in error_msg or "retrieval incomplete" in error_msg:
                    logger.error(f"Tika server JAR download failed: {e}")
                    raise ValueError(
                        f"Failed to download Tika JAR (Network Error). Please manually download 'tika-server-standard-2.6.0.jar' "
                        f"and set TIKA_SERVER_JAR environment variable, or ensure you have stable internet access to maven.org. "
                        f"Details: {e}"
                    )
                logger.error(f"Error parsing .doc file with Tika: {e}")
                raise ValueError(f"Failed to parse .doc file: {e}")

        docx_parser = Docx()
        
        if output_format == "json":
            sections, tbls = await _to_thread(docx_parser, filename, binary)
            json_sections = [{"text": section[0], "image": section[1]} for section in sections if section]
            json_sections.extend([{"text": tb, "image": None} for ((_,tb), _) in tbls])
            return {"output_format": "json", "json": json_sections}
        elif output_format == "markdown":
            markdown_text = await _to_thread(docx_parser.to_markdown, filename, binary)
            return {"output_format": "markdown", "markdown": markdown_text}
    
    @staticmethod
    async def parse_ppt(
        filename: str,
        binary: bytes,
        callback=None,
        *,
        output_format: Literal["json"] = "json",
        parse_method: str = "deepdoc",
        **method_kwargs
    ) -> dict:
        """
        PPT 解析（参考 core/flow/parser/parser.py._slides 第 329-346 行）
        
        Returns:
            {"output_format": "json", "json": [sections]}
        """
        if callback:
            callback(0.1, "Start to work on a PowerPoint Document")
        
        if output_format != "json":
            raise ValueError("PPT parser currently only supports JSON output_format")
        
        method = (parse_method or "deepdoc").lower()
        
        if method == "tcadp parser":
            table_result_type = method_kwargs.get("table_result_type", "1")
            markdown_image_response_type = method_kwargs.get("markdown_image_response_type", "1")
            tcadp_parser = TCADPParser(
                table_result_type=table_result_type,
                markdown_image_response_type=markdown_image_response_type
            )
            install_result = tcadp_parser.check_installation()
            if isinstance(install_result, tuple):
                ok, reason = install_result
            else:
                ok = bool(install_result)
                reason = "" if ok else "Unknown reason"
            if not ok:
                raise RuntimeError(f"TCADP parser not available. Please check Tencent Cloud API configuration. {reason}".strip())
            
            if re.search(r"\.pptx?$", filename, re.IGNORECASE):
                file_type = "PPTX"
            else:
                file_type = "PPT"
            
            sections, tables = await _to_thread(
                tcadp_parser.parse_pdf,
                filepath=filename,
                binary=binary,
                callback=callback,
                file_type=file_type,
                file_start_page=1,
                file_end_page=1000
            )
            
            result = []
            for section, _ in sections:
                if section:
                    result.append({"text": section})
            for table in tables:
                if table:
                    result.append({"text": table})
            
            return {"output_format": "json", "json": result}
        
        ext = filename.lower().split(".")[-1] if "." in filename else ""
        
        # 1. 尝试使用 Tika 解析 .ppt (老格式)
        if ext == "ppt":
            try:
                # Ensure binary is bytes
                if hasattr(binary, 'read'):
                     binary_data = binary.read()
                else:
                     binary_data = binary

                doc_parsed = await _to_thread(tika_parser.from_buffer, binary_data)
                content = doc_parsed.get('content', '') or ''
                
                # Simple processing for .ppt content
                sections = [s.strip() for s in content.split('\n') if s.strip()]
                
                # Tika doesn't extract structure well from ppt, so we return simple text blocks
                json_sections = [{"text": s} for s in sections]
                return {"output_format": "json", "json": json_sections}

            except Exception as e:
                logger.warning(f"Tika parsing failed for .ppt file: {e}. Falling back to pptx parser (unlikely to work).")
                # Continue to try RAGFlowPptParser as fallback (though it likely fails for .ppt)

        # 2. 尝试使用 python-pptx 解析 .pptx (或作为回退)
        try:
            ppt_parser = RAGFlowPptParser()
            txts = await _to_thread(ppt_parser, binary, 0, 100000, None)
            
            sections = [{"text": section} for section in txts if section.strip()]
            
            return {"output_format": "json", "json": sections}
        except KeyError as e:
            error_msg = str(e)
            if "no relationship of type" in error_msg:
                # 这是 python-pptx 解析 .ppt 文件时的典型错误
                raise ValueError(f"The file format is not supported by the PPTX parser (python-pptx only supports .pptx). Please convert '{filename}' to .pptx format or ensure Tika is configured correctly for .ppt support. Error: {e}")
            raise
        except Exception as e:
            logger.exception(f"PPT parsing failed: {e}")
            raise ValueError(f"Failed to parse PowerPoint file: {e}")
    
    @staticmethod
    async def parse_markdown(
        filename: str,
        binary: bytes,
        output_format: Literal["json", "text"] = "json",
        callback=None
    ) -> dict:
        """
        Markdown 解析（参考 core/flow/parser/parser.py._markdown 第 347-379 行）
        
        Returns:
            {"output_format": "json", "json": [sections with images]}
        """
        if callback:
            callback(0.1, "Start to work on a markdown.")
        
        markdown_parser = MarkdownParser()
        sections, tables = await _to_thread(markdown_parser, filename, binary, separate_tables=False)
        
        if output_format == "json":
            json_results = []
            
            for section_text, _ in sections:
                json_result = {"text": section_text}
                
                images = markdown_parser.get_pictures(section_text) if section_text else None
                if images:
                    combined_image = reduce(concat_img, images) if len(images) > 1 else images[0]
                    json_result["image"] = combined_image
                
                json_results.append(json_result)
            
            return {"output_format": "json", "json": json_results}
        else:
            text = "\n".join([section_text for section_text, _ in sections])
            return {"output_format": "text", "text": text}
    
    @staticmethod
    async def parse_image(
        filename: str,
        binary: bytes,
        tenant_id: str,
        parse_method: str = "ocr",
        llm_name: str | None = None,
        lang: str = "Chinese",
        system_prompt: str | None = None,
        callback=None
    ) -> dict:
        """
        图片解析（参考 core/flow/parser/parser.py._image 第 381-410 行）
        
        Args:
            parse_method: 
                - "ocr": OCR 文字识别
                - "vlm": VLM 视觉理解（需要 llm_name）
                - VLM 模型名（如 "qwen-vl-plus"）: 直接使用该模型
            llm_name: VLM 模型名称（parse_method="vlm" 时需要）
        
        Returns:
            {"output_format": "text", "text": "..."}
        """
        if callback:
            callback(0.1, "Start to work on an image.")
        
        img = Image.open(BytesIO(binary)).convert("RGB")
        
        if parse_method in ["ocr", "deepdoc", "plain_text"]:
            # 使用 OCR 识别文字
            ocr = OCR()
            bxs = await _to_thread(ocr, np.array(img))
            txt = "\n".join([t[0] for _, t in bxs if t[0]])
        else:
            # 使用 VLM 描述图片
            # 兼容两种方式：
            # 1. parse_method="vlm" + llm_name="qwen-vl-plus"（我们的方式）
            # 2. parse_method="qwen-vl-plus"（core/flow 的方式）
            # 3. parse_method="deepdoc"（task_executor 默认值）-> 已在上一个 if 分支处理
            
            actual_llm_name = llm_name or parse_method  # 优先使用 llm_name，否则用 parse_method
            with db_connection() as db:
                cv_model = LLMBundle(db, tenant_id, LLMType.IMAGE2TEXT, llm_name=actual_llm_name, lang=lang)
            
            img_binary = BytesIO()
            img.save(img_binary, format="JPEG")
            img_binary.seek(0)
            
            img_data = img_binary.read()
            if system_prompt:
                txt = await _to_thread(cv_model.describe_with_prompt, img_data, system_prompt)
            else:
                txt = await _to_thread(cv_model.describe, img_data)
        
        return {"output_format": "text", "text": txt}
    
    @staticmethod
    async def parse_video(
        filename: str,
        binary: bytes,
        tenant_id: str,
        callback=None
    ) -> dict:
        """
        视频解析（参考 core/flow/parser/parser.py._video 第 431-440 行）
        
        Returns:
            {"output_format": "text", "text": "视频描述"}
        """
        if callback:
            callback(0.1, "Start to work on a video.")
        
        with db_connection() as db:
            cv_mdl = LLMBundle(db, tenant_id, LLMType.IMAGE2TEXT)
        
        # 视频分析（传递 video_bytes 和 filename）
        txt = await _to_thread(cv_mdl.chat, "", [], {}, video_bytes=binary, filename=filename)
        
        return {"output_format": "text", "text": txt}
    
    @staticmethod
    async def parse_email(
        filename: str,
        binary: bytes,
        output_format: Literal["json", "text"] = "json",
        fields: list[str] | None = None,
        callback=None
    ) -> dict:
        """
        邮件解析（参考 core/flow/parser/parser.py._email 第 442-557 行）
        
        Args:
            output_format: json 或 text
            fields: 需要提取的字段（from/to/cc/bcc/date/subject/body/attachments/metadata）
        
        Returns:
            {"output_format": "json", "json": [email_content]}
            或
            {"output_format": "text", "text": "邮件内容"}
        """
        import io
        
        if callback:
            callback(0.1, "Start to work on an email.")
        
        if fields is None:
            fields = ["from", "to", "cc", "bcc", "date", "subject", "body", "attachments", "metadata"]
        
        email_content = {}
        _, ext = os.path.splitext(filename)
        
        if ext == ".eml":
            # 处理 .eml 文件
            from email import policy
            from email.parser import BytesParser
            
            msg = BytesParser(policy=policy.default).parse(io.BytesIO(binary))
            email_content['metadata'] = {}
            
            # 提取头部信息
            for header, value in msg.items():
                if header.lower() in fields:
                    email_content[header.lower()] = value
                elif header.lower() not in ["from", "to", "cc", "bcc", "date", "subject"]:
                    email_content["metadata"][header.lower()] = value
            
            # 提取正文
            if "body" in fields:
                body_text, body_html = [], []
                
                def _add_content(m, content_type):
                    if content_type == "text/plain":
                        body_text.append(m.get_payload(decode=True).decode(m.get_content_charset()))
                    elif content_type == "text/html":
                        body_html.append(m.get_payload(decode=True).decode(m.get_content_charset()))
                    elif "multipart" in content_type:
                        if m.is_multipart():
                            for part in m.iter_parts():
                                _add_content(part, part.get_content_type())
                
                _add_content(msg, msg.get_content_type())
                email_content["text"] = "\n".join(body_text)
                email_content["text_html"] = "\n".join(body_html)
            
            # 提取附件
            if "attachments" in fields:
                attachments = []
                for part in msg.iter_attachments():
                    content_disposition = part.get("Content-Disposition")
                    if content_disposition:
                        dispositions = content_disposition.strip().split(";")
                        if dispositions[0].lower() == "attachment":
                            filename_att = part.get_filename()
                            payload = part.get_payload(decode=True).decode(part.get_content_charset())
                            attachments.append({"filename": filename_att, "payload": payload})
                email_content["attachments"] = attachments
        
        else:
            # 处理 .msg 文件
            import extract_msg
            msg = extract_msg.Message(binary)
            
            basic_content = {
                "from": msg.sender,
                "to": msg.to,
                "cc": msg.cc,
                "bcc": msg.bcc,
                "date": msg.date,
                "subject": msg.subject,
            }
            email_content.update({k: v for k, v in basic_content.items() if k in fields})
            
            email_content['metadata'] = {
                'message_id': msg.messageId,
                'in_reply_to': msg.inReplyTo,
            }
            
            if "body" in fields:
                email_content["text"] = msg.body[0] if isinstance(msg.body, list) and msg.body else msg.body
                if not email_content["text"] and msg.htmlBody:
                    email_content["text"] = msg.htmlBody[0] if isinstance(msg.htmlBody, list) and msg.htmlBody else msg.htmlBody
            
            if "attachments" in fields:
                attachments = []
                for t in msg.attachments:
                    attachments.append({"filename": t.name, "payload": t.data.decode("utf-8")})
                email_content["attachments"] = attachments
        
        # 返回格式
        if output_format == "json":
            return {"output_format": "json", "json": [email_content]}
        else:
            # 转换为纯文本
            content_txt = ''
            for k, v in email_content.items():
                if isinstance(v, str):
                    content_txt += f'{k}:{v}\n'
                elif isinstance(v, dict):
                    import json
                    content_txt += f'{k}:{json.dumps(v)}\n'
                elif isinstance(v, list):
                    for fb in v:
                        if isinstance(fb, dict):
                            content_txt += f'{fb["filename"]}:{fb["payload"]}\n'
                        else:
                            content_txt += fb
            return {"output_format": "text", "text": content_txt}


# ========== 便捷接口 ==========

async def parse_file(
    filename: str,
    binary: bytes,
    tenant_id: str,
    pdf_config: dict | None = None,
    excel_config: dict | None = None,
    word_config: dict | None = None,
    image_config: dict | None = None,
    email_config: dict | None = None,
    slides_config: dict | None = None,
    markdown_config: dict | None = None,
    callback=None
) -> dict:
    """
    使用 core/flow 逻辑解析文件，返回结构化结果（保留位置信息）

    参考：core/flow/parser/parser.py._invoke 的逻辑（第 559-600 行）
    根据文件扩展名自动路由到对应的解析方法
    
    Args:
        filename: 文件名
        binary: 文件二进制内容
        tenant_id: 租户 ID
        pdf_config: PDF 配置 {"parse_method": "deepdoc", "output_format": "json"}
        excel_config: Excel 配置 {"output_format": "html"}
        word_config: Word 配置 {"output_format": "json"}
        image_config: 图片配置 {"parse_method": "ocr", "llm_name": None, "lang": "Chinese"}
        email_config: 邮件配置 {"output_format": "json", "fields": [...]}
        slides_config: PPT 配置 {"parse_method": "deepdoc", "output_format": "json"}
        markdown_config: Markdown 配置 {"output_format": "json"}
        callback: 进度回调
    
    Returns:
        {
            "output_format": "json",
            "json": [{"text": "...", "page": 1, "x0": 100, "position_tag": "..."}]
        }
        或
        {
            "output_format": "text",
            "text": "..."
        }
    """
    # 提取文件扩展名
    ext = filename.lower().split(".")[-1] if "." in filename else ""
    
    # 默认配置
    if pdf_config is None:
        pdf_config = {"parse_method": "deepdoc", "output_format": "json"}
    if excel_config is None:
        excel_config = {"output_format": "html"}
    if word_config is None:
        word_config = {"output_format": "json"}
    if image_config is None:
        image_config = {"parse_method": "ocr", "llm_name": None, "lang": "Chinese"}
    if email_config is None:
        email_config = {"output_format": "json", "fields": None}
    if slides_config is None:
        slides_config = {"output_format": "json", "parse_method": "deepdoc"}
    if markdown_config is None:
        markdown_config = {"output_format": "json"}
    
    # 根据扩展名路由（参考 core/flow/parser/parser.py 第 551-556 行）
    # 音频文件
    if ext in ["mp3", "wav", "aac", "flac", "ogg", "aiff", "au", "midi", "wma", "da", "wave", "ape"]:
        return await FlowParser.parse_audio(filename, binary, tenant_id, callback)
    
    # PDF 文件
    elif ext == "pdf":
        pdf_extra = {k: v for k, v in pdf_config.items() if k not in {"parse_method", "output_format", "lang"}}
        return await FlowParser.parse_pdf(
            filename,
            binary,
            tenant_id,
            pdf_config.get("parse_method", "deepdoc"),
            pdf_config.get("output_format", "json"),
            pdf_config.get("lang", "Chinese"),
            callback=callback,
            **pdf_extra
        )
    
    # Excel 文件
    elif ext in ["xls", "xlsx", "csv"]:
        excel_extra = {k: v for k, v in excel_config.items() if k not in {"output_format", "parse_method"}}
        return await FlowParser.parse_excel(
            filename,
            binary,
            excel_config.get("output_format", "html"),
            callback=callback,
            parse_method=excel_config.get("parse_method", "deepdoc"),
            **excel_extra
        )
    
    # Word 文件
    elif ext in ["doc", "docx"]:
        return await FlowParser.parse_word(
            filename, binary,
            word_config.get("output_format", "json"),
            callback
        )
    
    # PPT 文件
    elif ext in ["ppt", "pptx"]:
        slides_extra = {k: v for k, v in slides_config.items() if k not in {"output_format", "parse_method"}}
        return await FlowParser.parse_ppt(
            filename,
            binary,
            callback=callback,
            output_format=slides_config.get("output_format", "json"),
            parse_method=slides_config.get("parse_method", "deepdoc"),
            **slides_extra
        )
    
    # Markdown/文本文件
    elif ext in ["md", "markdown", "mdx", "txt"]:
        return await FlowParser.parse_markdown(
            filename,
            binary,
            markdown_config.get("output_format", "json"),
            callback
        )
    
    # 图片文件
    elif ext in ["jpg", "jpeg", "png", "gif", "bmp", "tiff", "webp"]:
        return await FlowParser.parse_image(
            filename, binary, tenant_id,
            image_config.get("parse_method", "ocr"),
            image_config.get("llm_name"),
            image_config.get("lang", "Chinese"),
            image_config.get("system_prompt"),
            callback
        )
    
    # 视频文件
    elif ext in ["mp4", "avi", "mkv", "mov", "flv", "wmv"]:
        return await FlowParser.parse_video(filename, binary, tenant_id, callback)
    
    # 邮件文件
    elif ext in ["eml", "msg"]:
        return await FlowParser.parse_email(
            filename, binary,
            email_config.get("output_format", "json"),
            email_config.get("fields"),
            callback
        )
    
    else:
        raise ValueError(f"Unsupported file extension: {ext}")