# project_service.py 最终优化版本

import logging
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from sqlalchemy.exc import NoResultFound, OperationalError

from api.db.services.common_service import CommonService
from api.db.db_models import WritingProject, WritingChapter, WritingChapterContent
from common.constants import StatusEnum
from common.misc_utils import get_uuid


class ProjectService(CommonService):
    model = WritingProject

    def __init__(self):
        super().__init__(WritingProject)

    @classmethod
    def get_list(cls, db: Session, user_id: str, page_number: int, items_per_page: int,
                 orderby: str = "create_time", sort_desc: bool = True, keywords: str = None) -> tuple[list[dict], int]:
        """
        获取用户的写作项目列表
        """
        # 直接使用CommonService的查询功能，添加自定义过滤条件
        filters = [
            cls.model.status == StatusEnum.VALID.value,
            cls.model.user_id == user_id
        ]

        if keywords:
            filters.append(
                cls.model.user_input.ilike(f'%{keywords}%') |
                cls.model.content_type.ilike(f'%{keywords}%') |
                cls.model.language_style.ilike(f'%{keywords}%')
            )

        # 使用内置分页查询
        query = db.query(cls.model).filter(*filters)

        # 总数
        total_count = query.count()

        # 排序
        order_col = getattr(cls.model, orderby)
        if sort_desc:
            query = query.order_by(order_col.desc())
        else:
            query = query.order_by(order_col.asc())

        # 分页
        projects = query.offset((page_number - 1) * items_per_page).limit(items_per_page).all()

        # 转换为字典
        result = [project.to_dict() for project in projects]

        return result, total_count

    @classmethod
    def create_project(cls, db: Session, project_data: dict) -> WritingProject:
        """创建写作项目"""
        # 直接使用CommonService的insert方法，自动处理ID和时间字段
        if "id" not in project_data:
            project_data["id"] = get_uuid()

        return cls.insert(db, **project_data)

    @classmethod
    def get_project_details(cls, db: Session, project_id: str, user_id: str = None) -> dict:
        """获取项目详情"""
        # 获取项目
        project = db.query(cls.model).filter(
            cls.model.id == project_id,
            cls.model.status == StatusEnum.VALID.value
        ).first()

        if not project:
            raise NoResultFound(f"未找到项目 ID: {project_id}")

        # 权限检查
        if user_id and project.user_id != user_id:
            raise PermissionError(f"用户 {user_id} 无权访问此项目")

        # 获取项目关联的所有章节
        chapters = db.query(WritingChapter).filter(
            WritingChapter.project_id == project_id,
            WritingChapter.status == StatusEnum.VALID.value
        ).order_by(
            WritingChapter.level.asc(),
            WritingChapter.order_index.asc()
        ).all()

        # 构建章节树结构
        chapters_dict = {}
        main_chapters = []

        for chapter in chapters:
            chapter_data = chapter.to_dict()
            chapter_data['children'] = []
            chapters_dict[chapter.id] = chapter_data

            if chapter.level == 1:  # 主章节
                main_chapters.append(chapter_data)

        # 将子章节关联到父章节
        for chapter in chapters:
            if chapter.parent_id and chapter.parent_id in chapters_dict:
                parent = chapters_dict[chapter.parent_id]
                chapter_data = chapters_dict[chapter.id]
                if chapter_data not in parent['children']:
                    parent['children'].append(chapter_data)

        # 构建结果
        result = project.to_dict()
        result['chapters'] = main_chapters
        result['chapter_count'] = len(chapters)

        return result

    @classmethod
    def update_project(cls, db: Session, project_id: str, project_data: dict, user_id: str = None) -> WritingProject:
        """更新项目信息"""
        # 获取项目
        project = db.query(cls.model).filter(
            cls.model.id == project_id,
            cls.model.status == StatusEnum.VALID.value
        ).first()

        if not project:
            raise NoResultFound(f"未找到项目 ID: {project_id}")

        # 权限检查
        if user_id and project.user_id != user_id:
            raise PermissionError(f"用户 {user_id} 无权修改此项目")

        try:
            # 使用CommonService的update_by_id方法更新项目
            # 这会自动处理时间字段
            cls.update_by_id(db, project_id, project_data)

            # 重新获取更新后的项目
            updated_project = db.query(cls.model).filter(cls.model.id == project_id).first()

            return updated_project
        except Exception as e:
            db.rollback()
            logging.error(f"更新项目失败: {str(e)}")
            raise e

    @classmethod
    def delete_project(cls, db: Session, project_id: str, user_id: str = None) -> bool:
        """删除项目（逻辑删除）"""
        # 获取项目
        project = db.query(cls.model).filter(
            cls.model.id == project_id,
            cls.model.status == StatusEnum.VALID.value
        ).first()

        if not project:
            logging.warning(f"项目不存在或已删除: {project_id}")
            return False

        # 权限检查
        if user_id and project.user_id != user_id:
            logging.warning(f"用户 {user_id} 无权删除此项目 {project_id}")
            raise PermissionError(f"用户 {user_id} 无权删除此项目")

        try:
            logging.info(f"开始删除项目: {project_id}")

            # 使用CommonService的update_by_id方法更新项目状态
            # 这会自动处理时间字段
            updated = cls.update_by_id(db, project_id, {"status": StatusEnum.INVALID.value})

            if updated == 0:
                logging.error(f"项目状态更新失败: {project_id}")
                return False

            logging.info(f"项目状态已更新，现在更新相关章节")

            # 使用filter_update批量更新章节状态
            from api.db.services.chapter_service import ChapterService

            # 获取所有相关章节
            chapters = db.query(WritingChapter).filter(
                WritingChapter.project_id == project_id,
                WritingChapter.status == StatusEnum.VALID.value
            ).all()

            # 逐个更新章节状态
            for chapter in chapters:
                ChapterService.update_by_id(db, chapter.id, {"status": StatusEnum.INVALID.value})

                # 同时更新章节内容状态
                content = db.query(WritingChapterContent).filter(
                    WritingChapterContent.chapter_id == chapter.id,
                    WritingChapterContent.status == StatusEnum.VALID.value
                ).first()

                if content:
                    from api.db.services.chapter_service import ChapterContentService
                    ChapterContentService.update_by_id(db, content.id, {"status": StatusEnum.INVALID.value})

            logging.info(f"已更新 {len(chapters)} 个相关章节")

            # 验证更改是否成功
            check_project = db.query(cls.model).filter(cls.model.id == project_id).first()

            if check_project and check_project.status == StatusEnum.INVALID.value:
                logging.info(f"项目删除成功: {project_id}")
                return True
            else:
                logging.error(f"项目删除验证失败: {project_id}")
                return False

        except Exception as e:
            db.rollback()
            logging.error(f"删除项目失败: {str(e)}", exc_info=True)
            return False

    @classmethod
    def duplicate_project(cls, db: Session, project_id: str, user_id: str) -> WritingProject:
        """复制项目及其所有章节"""
        # 获取原项目
        project = db.query(cls.model).filter(
            cls.model.id == project_id,
            cls.model.status == StatusEnum.VALID.value
        ).first()

        if not project:
            raise NoResultFound(f"未找到项目 ID: {project_id}")

        try:
            # 创建新项目
            new_project_id = get_uuid()
            new_project_data = project.to_dict()
            new_project_data.pop('id', None)
            new_project_data.pop('create_time', None)
            new_project_data.pop('update_time', None)
            new_project_data.pop('create_date', None)
            new_project_data.pop('update_date', None)

            new_project_data['id'] = new_project_id
            new_project_data['user_id'] = user_id

            # 使用CommonService的insert方法创建新项目
            new_project = cls.insert(db, **new_project_data)

            # 获取原项目的所有章节
            chapters = db.query(WritingChapter).filter(
                WritingChapter.project_id == project_id,
                WritingChapter.status == StatusEnum.VALID.value
            ).order_by(WritingChapter.level.asc()).all()

            # 建立新旧章节ID的映射
            id_mapping = {}

            # 复制章节（先复制主章节，再复制子章节）
            for chapter in chapters:
                old_id = chapter.id
                new_id = get_uuid()
                id_mapping[old_id] = new_id

                new_chapter_data = chapter.to_dict()
                new_chapter_data.pop('id', None)
                new_chapter_data.pop('create_time', None)
                new_chapter_data.pop('update_time', None)
                new_chapter_data.pop('create_date', None)
                new_chapter_data.pop('update_date', None)

                new_chapter_data['id'] = new_id
                new_chapter_data['project_id'] = new_project_id

                # 处理父章节ID
                if chapter.parent_id:
                    if chapter.parent_id in id_mapping:
                        new_chapter_data['parent_id'] = id_mapping[chapter.parent_id]
                    else:
                        # 父章节还没有映射，这是一个错误
                        logging.warning(f"复制章节 {chapter.id} 时未找到父章节 {chapter.parent_id} 的映射")
                        new_chapter_data['parent_id'] = None

                # 使用ChapterService的insert方法
                from api.db.services.chapter_service import ChapterService
                ChapterService.insert(db, **new_chapter_data)

            return new_project
        except Exception as e:
            db.rollback()
            logging.error(f"复制项目失败: {str(e)}")
            raise e