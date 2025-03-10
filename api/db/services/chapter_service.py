# coding=utf-8
"""
@project: writing_system
@file： chapter_service.py
@desc: 写作章节服务类
"""
import logging
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from sqlalchemy import func
from sqlalchemy.exc import NoResultFound
from api.db.services.common_service import CommonService
from api.db.db_models import WritingChapter, WritingProject, WritingChapterContent
from api.db import StatusEnum
from api.utils import get_uuid


class ChapterService(CommonService):
    model = WritingChapter

    def __init__(self):
        super().__init__(WritingChapter)

    @classmethod
    def get_by_project_id(cls, db: Session, project_id: str) -> list[dict]:
        """
        获取项目下的所有章节

        参数:
            db: 数据库会话
            project_id: 项目ID

        返回:
            list: 章节列表
        """
        chapters = db.query(cls.model).filter(
            cls.model.project_id == project_id,
            cls.model.status == StatusEnum.VALID.value
        ).order_by(
            cls.model.level.asc(),
            cls.model.order_index.asc()
        ).all()

        return [chapter.to_dict() for chapter in chapters]

    @classmethod
    def get_chapter_with_content(cls, db: Session, chapter_id: str) -> dict:
        """
        获取章节信息及其内容

        参数:
            db: 数据库会话
            chapter_id: 章节ID

        返回:
            dict: 章节信息及内容
        """
        # 获取章节信息
        chapter = db.query(cls.model).filter(
            cls.model.id == chapter_id,
            cls.model.status == StatusEnum.VALID.value
        ).first()

        if not chapter:
            raise NoResultFound(f"未找到章节 ID: {chapter_id}")

        result = chapter.to_dict()

        # 获取章节内容
        content = db.query(WritingChapterContent).filter(
            WritingChapterContent.chapter_id == chapter_id,
            WritingChapterContent.status == StatusEnum.VALID.value
        ).first()

        if content:
            result['content'] = content.content
        else:
            result['content'] = None

        return result

    @classmethod
    def create_chapter(cls, db: Session, chapter_data: dict) -> WritingChapter:
        """
        创建章节

        参数:
            db: 数据库会话
            chapter_data: 章节数据

        返回:
            WritingChapter: 创建的章节对象
        """
        try:
            # 确保有ID
            if "id" not in chapter_data:
                chapter_data["id"] = get_uuid()

            # 如果是子章节，设置level为2
            if chapter_data.get("parent_id"):
                chapter_data["level"] = 2
            else:
                chapter_data["level"] = 1

            # 如果没有指定order_index，获取当前最大值+1
            if "order_index" not in chapter_data:
                if chapter_data.get("parent_id"):
                    # 子章节
                    max_order = db.query(func.max(cls.model.order_index)).filter(
                        cls.model.parent_id == chapter_data["parent_id"],
                        cls.model.status == StatusEnum.VALID.value
                    ).scalar() or -1
                else:
                    # 主章节
                    max_order = db.query(func.max(cls.model.order_index)).filter(
                        cls.model.project_id == chapter_data["project_id"],
                        cls.model.level == 1,
                        cls.model.status == StatusEnum.VALID.value
                    ).scalar() or -1

                chapter_data["order_index"] = max_order + 1

            # 创建章节
            chapter = cls.model(**chapter_data)
            db.add(chapter)
            db.commit()
            db.refresh(chapter)

            return chapter
        except Exception as e:
            db.rollback()
            logging.error(f"创建章节失败: {str(e)}")
            raise e

    @classmethod
    def update_chapter(cls, db: Session, chapter_id: str, chapter_data: dict) -> WritingChapter:
        """更新章节信息"""
        chapter = db.query(cls.model).filter(
            cls.model.id == chapter_id,
            cls.model.status == StatusEnum.VALID.value
        ).first()

        if not chapter:
            raise NoResultFound(f"未找到章节 ID: {chapter_id}")

        try:
            for key, value in chapter_data.items():
                if hasattr(chapter, key) and key not in ['id', 'project_id', 'create_time', 'update_time',
                                                         'create_date', 'update_date', 'status']:
                    setattr(chapter, key, value)

            if 'parent_id' in chapter_data:
                chapter.level = 2 if chapter_data['parent_id'] else 1

            # 手动更新时间戳
            now = datetime.now()
            now_ts = int(now.timestamp())
            now_utc = datetime.now(timezone.utc)

            chapter.update_time = now_ts
            chapter.update_date = now_utc

            db.commit()
            db.refresh(chapter)

            return chapter
        except Exception as e:
            db.rollback()
            logging.error(f"更新章节失败: {str(e)}")
            raise e

    @classmethod
    def delete_chapter(cls, db: Session, chapter_id: str) -> bool:
        """删除章节（逻辑删除）并处理子章节"""
        chapter = db.query(cls.model).filter(
            cls.model.id == chapter_id,
            cls.model.status == StatusEnum.VALID.value
        ).first()

        if not chapter:
            return False

        try:
            # 如果是主章节，将其所有子章节也标记为删除
            if chapter.level == 1:
                # 获取当前时间戳
                now = datetime.now()
                now_ts = int(now.timestamp())
                now_utc = datetime.now(timezone.utc)

                # 更新子章节
                sub_chapters = db.query(cls.model).filter(
                    cls.model.parent_id == chapter_id,
                    cls.model.status == StatusEnum.VALID.value
                ).all()

                for sub in sub_chapters:
                    sub.status = StatusEnum.INVALID.value
                    sub.update_time = now_ts
                    sub.update_date = now_utc

            # 标记章节为已删除
            chapter.status = StatusEnum.INVALID.value
            chapter.update_time = int(datetime.now().timestamp())
            chapter.update_date = datetime.now(timezone.utc)

            # 删除章节内容
            content = db.query(WritingChapterContent).filter(
                WritingChapterContent.chapter_id == chapter_id,
                WritingChapterContent.status == StatusEnum.VALID.value
            ).first()

            if content:
                content.status = StatusEnum.INVALID.value
                content.update_time = int(datetime.now().timestamp())
                content.update_date = datetime.now(timezone.utc)

            db.commit()
            return True
        except Exception as e:
            db.rollback()
            logging.error(f"删除章节失败: {str(e)}")
            return False

    @classmethod
    def reorder_chapters(cls, db: Session, chapters_order: list[dict]) -> bool:
        """
        重新排序章节

        参数:
            db: 数据库会话
            chapters_order: 包含章节ID和新order的列表 [{"id": "...", "order_index": 0, "parent_id": "..."}]

        返回:
            bool: 是否排序成功
        """
        try:
            # 获取当前时间戳
            now = datetime.now()
            now_ts = int(now.timestamp())
            now_utc = datetime.now(timezone.utc)

            for chapter_data in chapters_order:
                chapter_id = chapter_data.get("id")
                new_order = chapter_data.get("order_index")
                new_parent_id = chapter_data.get("parent_id")

                chapter = db.query(cls.model).filter(
                    cls.model.id == chapter_id,
                    cls.model.status == StatusEnum.VALID.value
                ).first()

                if chapter:
                    # 更新order
                    if new_order is not None:
                        chapter.order_index = new_order

                    # 更新父章节
                    if new_parent_id is not None and new_parent_id != chapter.parent_id:
                        old_parent_id = chapter.parent_id
                        chapter.parent_id = new_parent_id

                        # 如果修改了父章节，调整level
                        if not new_parent_id:  # 变为主章节
                            chapter.level = 1
                        else:  # 变为子章节
                            chapter.level = 2

                    # 手动更新时间戳
                    chapter.update_time = now_ts
                    chapter.update_date = now_utc

            db.commit()
            return True
        except Exception as e:
            db.rollback()
            logging.error(f"重新排序章节失败: {str(e)}")
            return False

    @classmethod
    def save_chapter_content(cls, db: Session, chapter_id: str, content: str) -> WritingChapterContent:
        """
        保存章节内容

        参数:
            db: 数据库会话
            chapter_id: 章节ID
            content: 章节内容

        返回:
            WritingChapterContent: 保存的内容对象
        """
        try:
            # 查找是否已经存在内容
            content_obj = db.query(WritingChapterContent).filter(
                WritingChapterContent.chapter_id == chapter_id,
                WritingChapterContent.status == StatusEnum.VALID.value
            ).first()

            if content_obj:
                # 更新现有内容
                content_obj.content = content
                content_obj.update_time = datetime.now()
            else:
                # 创建新内容
                content_obj = WritingChapterContent(
                    id=get_uuid(),
                    chapter_id=chapter_id,
                    content=content
                )
                db.add(content_obj)

            # 更新章节的更新时间
            chapter = db.query(cls.model).filter(cls.model.id == chapter_id).first()
            if chapter:
                chapter.update_time = datetime.now()

            db.commit()
            db.refresh(content_obj)

            return content_obj
        except Exception as e:
            db.rollback()
            logging.error(f"保存章节内容失败: {str(e)}")
            raise e

    @classmethod
    def get_project_outline(cls, db: Session, project_id: str) -> dict:
        """
        获取项目大纲结构

        参数:
            db: 数据库会话
            project_id: 项目ID

        返回:
            dict: 大纲结构，包含章节树
        """
        # 获取项目信息
        project = db.query(WritingProject).filter(
            WritingProject.id == project_id,
            WritingProject.status == StatusEnum.VALID.value
        ).first()

        if not project:
            raise NoResultFound(f"未找到项目 ID: {project_id}")

        # 获取所有章节
        chapters = db.query(cls.model).filter(
            cls.model.project_id == project_id,
            cls.model.status == StatusEnum.VALID.value
        ).order_by(
            cls.model.level.asc(),
            cls.model.order_index.asc()
        ).all()

        # 获取已经有内容的章节ID列表
        completed_chapter_ids = db.query(WritingChapterContent.chapter_id).filter(
            WritingChapterContent.status == StatusEnum.VALID.value,
            WritingChapterContent.chapter_id.in_([ch.id for ch in chapters])
        ).all()

        completed_ids = [str(row.chapter_id) for row in completed_chapter_ids]

        # 构建章节树
        main_chapters = []
        chapters_map = {}

        for chapter in chapters:
            chapter_data = chapter.to_dict()
            chapter_data['completed'] = chapter.id in completed_ids
            chapter_data['children'] = []
            chapters_map[chapter.id] = chapter_data

            if chapter.level == 1:
                main_chapters.append(chapter_data)

        # 关联子章节
        for chapter_id, chapter_data in chapters_map.items():
            if chapter_data.get('parent_id') and chapter_data['parent_id'] in chapters_map:
                parent_data = chapters_map[chapter_data['parent_id']]
                parent_data['children'].append(chapter_data)

        # 使用项目的title字段，如果为空则使用原来的拼接方式作为后备
        article_title = project.title if project.title else f"{project.content_type}（{project.language_style}风格）"

        result = {
            'id': project.id,
            'title': article_title,
            'content_type': project.content_type,
            'language_style': project.language_style,
            'user_input': project.user_input,
            'word_count': project.word_count,
            'sections': main_chapters,
            'total_sections': len(chapters),
            'completed_sections': len(completed_ids)
        }

        return result

    @classmethod
    def update_project_outline(cls, db: Session, project_id: str, outline_data: dict) -> dict:
        """
        更新项目的完整大纲结构
        处理章节的新增、更新、删除和重排序

        参数:
            db: 数据库会话
            project_id: 项目ID
            outline_data: 大纲数据，格式如下：
            {
                "sections": [
                    {
                        "id": "章节ID(可选，新章节不提供)",
                        "title": "章节标题",
                        "summary": "章节摘要",
                        "order_index": 0,
                        "children": [
                            {
                                "id": "子章节ID(可选，新章节不提供)",
                                "title": "子章节标题",
                                "summary": "子章节摘要",
                                "order_index": 0
                            }
                        ]
                    }
                ]
            }

        返回:
            dict: 更新后的大纲结构
        """
        try:
            # 获取项目所有现有章节
            existing_chapters = db.query(cls.model).filter(
                cls.model.project_id == project_id,
                cls.model.status == StatusEnum.VALID.value
            ).all()

            # 构建现有章节ID映射，用于快速查找
            existing_chapter_map = {chapter.id: chapter for chapter in existing_chapters}

            # 跟踪处理过的章节ID，用于确定哪些章节需要删除
            processed_ids = set()

            # 处理主章节
            for index, section_data in enumerate(outline_data.get("sections", [])):
                section_id = section_data.get("id")

                if section_id and section_id in existing_chapter_map:
                    # 更新现有主章节
                    main_chapter = existing_chapter_map[section_id]
                    main_chapter.title = section_data.get("title", main_chapter.title)
                    main_chapter.summary = section_data.get("summary", main_chapter.summary)
                    main_chapter.order_index = index
                    main_chapter.update_time = int(datetime.now().timestamp())
                    main_chapter.update_date = datetime.now(timezone.utc)
                    processed_ids.add(section_id)
                else:
                    # 创建新主章节
                    main_chapter_id = get_uuid()
                    main_chapter = cls.model(
                        id=main_chapter_id,
                        project_id=project_id,
                        title=section_data.get("title", "未命名章节"),
                        summary=section_data.get("summary", ""),
                        level=1,
                        parent_id=None,
                        order_index=index
                    )
                    db.add(main_chapter)
                    db.flush()  # 刷新以获取ID
                    processed_ids.add(main_chapter.id)

                # 处理子章节
                children = section_data.get("children", [])
                main_chapter_id = main_chapter.id if hasattr(main_chapter, 'id') else main_chapter_id

                for child_index, child_data in enumerate(children):
                    child_id = child_data.get("id")

                    if child_id and child_id in existing_chapter_map:
                        # 更新现有子章节
                        sub_chapter = existing_chapter_map[child_id]
                        sub_chapter.title = child_data.get("title", sub_chapter.title)
                        sub_chapter.summary = child_data.get("summary", sub_chapter.summary)
                        sub_chapter.order_index = child_index
                        sub_chapter.parent_id = main_chapter_id  # 确保父章节关系正确
                        sub_chapter.update_time = int(datetime.now().timestamp())
                        sub_chapter.update_date = datetime.now(timezone.utc)
                        processed_ids.add(child_id)
                    else:
                        # 创建新子章节
                        sub_chapter = cls.model(
                            id=get_uuid(),
                            project_id=project_id,
                            title=child_data.get("title", "未命名子章节"),
                            summary=child_data.get("summary", ""),
                            level=2,
                            parent_id=main_chapter_id,
                            order_index=child_index
                        )
                        db.add(sub_chapter)
                        db.flush()
                        processed_ids.add(sub_chapter.id)

            # 标记未处理的章节为删除状态（逻辑删除）
            for chapter_id, chapter in existing_chapter_map.items():
                if chapter_id not in processed_ids:
                    chapter.status = StatusEnum.INVALID.value
                    chapter.update_time = int(datetime.now().timestamp())
                    chapter.update_date = datetime.now(timezone.utc)

            db.commit()

            # 返回更新后的大纲结构
            return cls.get_project_outline(db, project_id)
        except Exception as e:
            db.rollback()
            logging.error(f"更新项目大纲失败: {str(e)}", exc_info=True)
            raise e

    @classmethod
    def get_markdown_outline(cls, db: Session, project_id: str) -> str:
        """
        获取项目大纲的Markdown格式

        参数:
            db: 数据库会话
            project_id: 项目ID

        返回:
            str: Markdown格式的大纲
        """
        # 获取项目信息
        project = db.query(WritingProject).filter(
            WritingProject.id == project_id,
            WritingProject.status == StatusEnum.VALID.value
        ).first()

        if not project:
            raise NoResultFound(f"未找到项目 ID: {project_id}")

        # 使用项目的title字段，如果为空则使用原来的拼接方式作为后备
        article_title = project.title if project.title else f"{project.content_type}（{project.language_style}风格）"

        # 获取所有章节
        chapters = db.query(cls.model).filter(
            cls.model.project_id == project_id,
            cls.model.status == StatusEnum.VALID.value
        ).order_by(
            cls.model.level.asc(),
            cls.model.order_index.asc()
        ).all()

        # 构建Markdown文本
        markdown = f"# {article_title}\n\n"

        # 添加用户输入的需求
        markdown += f"用户需求: {project.user_input}\n\n"

        # 其余Markdown构建代码保持不变...
        # 主章节和子章节
        main_chapters = {}
        sub_chapters = {}

        for chapter in chapters:
            if chapter.level == 1:
                main_chapters[chapter.id] = chapter
            else:
                if chapter.parent_id not in sub_chapters:
                    sub_chapters[chapter.parent_id] = []
                sub_chapters[chapter.parent_id].append(chapter)

        # 按顺序生成Markdown
        for main_id, main_chapter in sorted(
                main_chapters.items(),
                key=lambda x: x[1].order_index
        ):
            markdown += f"## {main_chapter.order_index + 1}. {main_chapter.title}\n"
            if main_chapter.summary:
                markdown += f"摘要：{main_chapter.summary}\n"

            # 添加子章节
            if main_id in sub_chapters:
                for sub_chapter in sorted(
                        sub_chapters[main_id],
                        key=lambda x: x.order_index
                ):
                    markdown += f"- {main_chapter.order_index + 1}.{sub_chapter.order_index + 1} {sub_chapter.title}"
                    if sub_chapter.summary:
                        markdown += f" (摘要：{sub_chapter.summary})"
                    markdown += "\n"

            markdown += "\n"

        return markdown
