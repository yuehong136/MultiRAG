"""
@project: writing_system
@file： reference_service.py
@desc: 参考资料服务类
"""

import logging
from datetime import UTC, datetime

from sqlalchemy import func
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from api.db.db_models import WritingChapter, WritingReferenceMaterial
from api.db.services.common_service import CommonService
from common.constants import StatusEnum
from common.misc_utils import get_uuid


class ReferenceService(CommonService):
    model = WritingReferenceMaterial

    def __init__(self):
        super().__init__(WritingReferenceMaterial)

    @classmethod
    def get_main_chapter_id(cls, db: Session, chapter_id: str) -> str:
        """
        获取主章节ID
        如果输入的是子章节ID，返回其父章节ID
        如果输入的是主章节ID，则直接返回

        参数:
            db: 数据库会话
            chapter_id: 章节ID

        返回:
            str: 主章节ID
        """
        # 获取章节信息
        chapter = db.query(WritingChapter).filter(WritingChapter.id == chapter_id, WritingChapter.status == StatusEnum.VALID.value).first()

        if not chapter:
            raise NoResultFound(f"未找到章节 ID: {chapter_id}")

        # 如果是子章节，返回父章节ID
        if chapter.level == 2 and chapter.parent_id:
            # 验证父章节是否存在
            parent_chapter = db.query(WritingChapter).filter(WritingChapter.id == chapter.parent_id, WritingChapter.status == StatusEnum.VALID.value).first()

            if parent_chapter:
                return parent_chapter.id
            else:
                # 如果父章节不存在或已删除，返回原章节ID
                logging.warning(f"章节 {chapter_id} 的父章节 {chapter.parent_id} 不存在或已删除")
                return chapter_id
        else:
            # 如果是主章节，直接返回
            return chapter_id

    @classmethod
    def get_by_chapter_id(cls, db: Session, chapter_id: str) -> list[dict]:
        """
        获取章节的所有参考资料
        无论传入的是主章节还是子章节ID，都会返回主章节的参考资料

        参数:
            db: 数据库会话
            chapter_id: 章节ID

        返回:
            list: 参考资料列表
        """
        try:
            # 获取主章节ID
            main_chapter_id = cls.get_main_chapter_id(db, chapter_id)

            # 获取主章节的所有参考资料
            references = db.query(cls.model).filter(cls.model.chapter_id == main_chapter_id, cls.model.status == StatusEnum.VALID.value).order_by(cls.model.order_index.asc()).all()

            return [ref.to_dict() for ref in references]
        except NoResultFound:
            # 如果找不到章节，返回空列表
            logging.warning(f"获取参考资料时找不到章节: {chapter_id}")
            return []
        except Exception as e:
            logging.error(f"获取章节参考资料失败: {e!s}")
            return []

    @classmethod
    def get_reference_by_id(cls, db: Session, reference_id: str) -> dict:
        """
        获取参考资料详情

        参数:
            db: 数据库会话
            reference_id: 参考资料ID

        返回:
            dict: 参考资料详情
        """
        reference = db.query(cls.model).filter(cls.model.id == reference_id, cls.model.status == StatusEnum.VALID.value).first()

        if not reference:
            raise NoResultFound(f"未找到参考资料 ID: {reference_id}")

        return reference.to_dict()

    @classmethod
    def add_reference(cls, db: Session, reference_data: dict) -> WritingReferenceMaterial:
        """
        添加参考资料
        无论传入的是主章节还是子章节ID，参考资料都会关联到主章节

        参数:
            db: 数据库会话
            reference_data: 参考资料数据

        返回:
            WritingReferenceMaterial: 创建的参考资料对象
        """
        try:
            chapter_id = reference_data.get("chapter_id")
            if not chapter_id:
                raise ValueError("必须提供章节ID")

            # 获取主章节ID
            main_chapter_id = cls.get_main_chapter_id(db, chapter_id)

            # 使用主章节ID替换原始chapter_id
            reference_data["chapter_id"] = main_chapter_id

            # 验证章节是否存在
            chapter = db.query(WritingChapter).filter(WritingChapter.id == main_chapter_id, WritingChapter.status == StatusEnum.VALID.value).first()

            if not chapter:
                raise NoResultFound(f"未找到章节 ID: {main_chapter_id}")

            # 确保有ID
            if "id" not in reference_data:
                reference_data["id"] = get_uuid()

            # 如果没有指定order_index，获取当前最大值+1
            if "order_index" not in reference_data:
                max_order = db.query(func.max(cls.model.order_index)).filter(cls.model.chapter_id == main_chapter_id, cls.model.status == StatusEnum.VALID.value).scalar() or -1

                reference_data["order_index"] = max_order + 1

            # 创建参考资料
            reference = cls.model(**reference_data)
            db.add(reference)
            db.commit()
            db.refresh(reference)

            return reference
        except Exception as e:
            db.rollback()
            logging.error(f"添加参考资料失败: {e!s}")
            raise e

    @classmethod
    def update_reference(cls, db: Session, reference_id: str, reference_data: dict) -> WritingReferenceMaterial:
        """更新参考资料"""
        reference = db.query(cls.model).filter(cls.model.id == reference_id, cls.model.status == StatusEnum.VALID.value).first()

        if not reference:
            raise NoResultFound(f"未找到参考资料 ID: {reference_id}")

        try:
            for key, value in reference_data.items():
                if hasattr(reference, key) and key not in ["id", "chapter_id", "create_time", "update_time", "create_date", "update_date", "status"]:
                    setattr(reference, key, value)

            # 手动更新时间戳
            now = datetime.now()
            reference.update_time = int(now.timestamp())
            reference.update_date = datetime.now(UTC)

            db.commit()
            db.refresh(reference)

            return reference
        except Exception as e:
            db.rollback()
            logging.error(f"更新参考资料失败: {e!s}")
            raise e

    @classmethod
    def delete_reference(cls, db: Session, reference_id: str) -> bool:
        """
        删除参考资料（逻辑删除）

        参数:
            db: 数据库会话
            reference_id: 参考资料ID

        返回:
            bool: 是否删除成功
        """
        # 获取参考资料
        reference = db.query(cls.model).filter(cls.model.id == reference_id, cls.model.status == StatusEnum.VALID.value).first()

        if not reference:
            return False

        try:
            # 逻辑删除，设置状态为无效
            reference.status = StatusEnum.INVALID.value

            db.commit()
            return True
        except Exception as e:
            db.rollback()
            logging.error(f"删除参考资料失败: {e!s}")
            return False

    @classmethod
    def delete_chapter_references(cls, db: Session, chapter_id: str) -> int:
        """
        删除章节的所有参考资料（逻辑删除）
        无论传入的是主章节还是子章节ID，都会删除主章节的参考资料

        参数:
            db: 数据库会话
            chapter_id: 章节ID

        返回:
            int: 删除的参考资料数量
        """
        try:
            # 获取主章节ID
            main_chapter_id = cls.get_main_chapter_id(db, chapter_id)

            # 查找主章节的所有参考资料
            references = db.query(cls.model).filter(cls.model.chapter_id == main_chapter_id, cls.model.status == StatusEnum.VALID.value).all()

            if not references:
                return 0

            # 获取当前时间戳
            now = datetime.now()
            now_ts = int(now.timestamp())
            now_utc = datetime.now(UTC)

            # 逻辑删除所有参考资料
            count = 0
            for ref in references:
                ref.status = StatusEnum.INVALID.value
                ref.update_time = now_ts
                ref.update_date = now_utc
                count += 1

            db.commit()
            return count
        except Exception as e:
            db.rollback()
            logging.error(f"删除章节参考资料失败: {e!s}")
            raise e

    @classmethod
    def reorder_references(cls, db: Session, chapter_id: str, references_order: list[dict]) -> bool:
        """
        重新排序章节的参考资料

        参数:
            db: 数据库会话
            chapter_id: 章节ID
            references_order: 包含参考资料ID和新order的列表 [{"id": "...", "order_index": 0}]

        返回:
            bool: 是否排序成功
        """
        try:
            # 获取当前时间戳
            now = datetime.now()
            now_ts = int(now.timestamp())
            now_utc = datetime.now(UTC)

            for ref_data in references_order:
                ref_id = ref_data.get("id")
                new_order = ref_data.get("order_index")

                # 验证参考资料是否属于指定章节
                reference = db.query(cls.model).filter(cls.model.id == ref_id, cls.model.chapter_id == chapter_id, cls.model.status == StatusEnum.VALID.value).first()

                if reference and new_order is not None:
                    reference.order_index = new_order
                    # 手动更新时间戳
                    reference.update_time = now_ts
                    reference.update_date = now_utc

            db.commit()
            return True
        except Exception as e:
            db.rollback()
            logging.error(f"重新排序参考资料失败: {e!s}")
            return False

    @classmethod
    def parse_file_content(cls, file_content: bytes, file_name: str) -> str:
        """
        解析文件内容

        参数:
            file_content: 文件内容字节流
            file_name: 文件名

        返回:
            str: 解析后的文本内容
        """
        try:
            # 直接调用FileService解析方法
            from api.db.services.file_service import FileService

            # 将文件内容和文件名作为元组传递给parse_docs方法
            # 这样与paste.txt中的用法保持一致
            file_data = [(file_content, file_name)]

            # 使用system作为用户ID进行解析
            parsed_text = FileService.parse_docs(file_data, "system")

            if parsed_text:
                return parsed_text
            else:
                return f"无法解析文件内容: {file_name}"

        except ImportError as e:
            logging.error(f"FileService导入失败: {e!s}")
            # 如果没有FileService，返回简单处理
            return cls._fallback_file_parsing(file_content, file_name)
        except Exception as e:
            logging.error(f"解析文件内容失败: {e!s}", exc_info=True)
            return f"解析文件失败: {e!s}"

    @classmethod
    def _fallback_file_parsing(cls, file_content: bytes, file_name: str) -> str:
        """当FileService不可用时的备选解析方法"""
        # 根据文件类型进行不同处理
        if file_name.lower().endswith(".txt"):
            return file_content.decode("utf-8", errors="ignore")
        elif file_name.lower().endswith(".pdf"):
            try:
                from io import BytesIO

                import pypdf

                pdf_file = BytesIO(file_content)
                pdf_reader = pypdf.PdfReader(pdf_file)
                text = ""
                for page_num in range(len(pdf_reader.pages)):
                    text += pdf_reader.pages[page_num].extract_text() + "\n"
                return text
            except ImportError:
                return "解析PDF需要pypdf库，请安装该库。"
        elif file_name.lower().endswith((".docx", ".doc")):
            try:
                from io import BytesIO

                import docx

                doc = docx.Document(BytesIO(file_content))
                text = "\n".join([para.text for para in doc.paragraphs if para.text.strip()])
                return text
            except ImportError:
                return "解析Word文档需要python-docx库，请安装该库。"
        else:
            return f"不支持的文件类型: {file_name}"

    @classmethod
    def parse_url_content(cls, url: str) -> str:
        """
        解析URL内容

        参数:
            url: 网页URL

        返回:
            str: 解析后的文本内容
        """
        # 这里应该调用您的URL解析逻辑
        # 以下是示例实现
        try:
            import requests
            from bs4 import BeautifulSoup

            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")

                # 移除脚本和样式元素
                for script in soup(["script", "style"]):
                    script.extract()

                # 获取文本内容
                text = soup.get_text(separator="\n", strip=True)

                # 清理多余空白行
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                return "\n".join(lines)
            else:
                return f"无法获取URL内容，状态码: {response.status_code}"
        except Exception as e:
            logging.error(f"解析URL内容失败: {e!s}")
            return f"解析URL内容失败: {e!s}"

    @classmethod
    def get_summary(cls, content: str, max_length: int = 200) -> str:
        """
        获取内容摘要

        参数:
            content: 原始内容
            max_length: 最大长度

        返回:
            str: 内容摘要
        """
        if not content:
            return ""

        # 简单截取前max_length个字符作为摘要
        if len(content) <= max_length:
            return content

        # 尝试在句子结束处截断
        for i in range(max_length - 1, max_length // 2, -1):
            if i < len(content) and content[i] in [".", "!", "?", "。", "！", "？"]:
                return content[: i + 1] + "..."

        # 如果没有找到合适的句子结束点，直接截断
        return content[:max_length] + "..."

    @classmethod
    def transfer_chapter_references(cls, db: Session, source_chapter_id: str, target_chapter_id: str) -> int:
        """
        将一个章节的参考资料转移到另一个章节

        参数:
            db: 数据库会话
            source_chapter_id: 源章节ID
            target_chapter_id: 目标章节ID

        返回:
            int: 转移的参考资料数量
        """
        try:
            # 验证两个章节是否存在
            source_chapter = db.query(WritingChapter).filter(WritingChapter.id == source_chapter_id, WritingChapter.status == StatusEnum.VALID.value).first()

            target_chapter = db.query(WritingChapter).filter(WritingChapter.id == target_chapter_id, WritingChapter.status == StatusEnum.VALID.value).first()

            if not source_chapter or not target_chapter:
                raise NoResultFound("源章节或目标章节不存在")

            # 获取目标章节当前最大order_index
            max_order = db.query(func.max(cls.model.order_index)).filter(cls.model.chapter_id == target_chapter_id, cls.model.status == StatusEnum.VALID.value).scalar() or -1

            # 获取源章节所有参考资料
            source_refs = db.query(cls.model).filter(cls.model.chapter_id == source_chapter_id, cls.model.status == StatusEnum.VALID.value).all()

            # 转移参考资料
            for i, ref in enumerate(source_refs):
                # 创建新的参考资料记录
                new_ref = WritingReferenceMaterial(id=get_uuid(), chapter_id=target_chapter_id, title=ref.title, content=ref.content, source=ref.source, type=ref.type, order_index=max_order + i + 1)
                db.add(new_ref)

            db.commit()
            return len(source_refs)
        except Exception as e:
            db.rollback()
            logging.error(f"转移参考资料失败: {e!s}")
            raise e

    @classmethod
    def duplicate_reference(cls, db: Session, reference_id: str, target_chapter_id: str = None) -> WritingReferenceMaterial:
        """
        复制参考资料

        参数:
            db: 数据库会话
            reference_id: 要复制的参考资料ID
            target_chapter_id: 目标章节ID (如果不指定，复制到原章节)

        返回:
            WritingReferenceMaterial: 新创建的参考资料对象
        """
        try:
            # 获取原参考资料
            source_ref = db.query(cls.model).filter(cls.model.id == reference_id, cls.model.status == StatusEnum.VALID.value).first()

            if not source_ref:
                raise NoResultFound(f"未找到参考资料 ID: {reference_id}")

            # 确定目标章节
            chapter_id = target_chapter_id or source_ref.chapter_id

            # 验证目标章节是否存在
            chapter = db.query(WritingChapter).filter(WritingChapter.id == chapter_id, WritingChapter.status == StatusEnum.VALID.value).first()

            if not chapter:
                raise NoResultFound(f"未找到章节 ID: {chapter_id}")

            # 获取当前最大order_index
            max_order = db.query(func.max(cls.model.order_index)).filter(cls.model.chapter_id == chapter_id, cls.model.status == StatusEnum.VALID.value).scalar() or -1

            # 创建新的参考资料
            new_ref = WritingReferenceMaterial(
                id=get_uuid(), chapter_id=chapter_id, title=f"{source_ref.title} (副本)", content=source_ref.content, source=source_ref.source, type=source_ref.type, order_index=max_order + 1
            )

            db.add(new_ref)
            db.commit()
            db.refresh(new_ref)

            return new_ref
        except Exception as e:
            db.rollback()
            logging.error(f"复制参考资料失败: {e!s}")
            raise e
