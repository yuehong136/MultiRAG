import logging
import json
import re
import asyncio
from typing import Any
from sqlalchemy.orm import Session

from api.db.db_models import WritingChapter, WritingProject, WritingChapterContent, WritingReferenceMaterial
from api.db.services.chapter_service import ChapterService
from api.db.services.common_service import CommonService
from api.db.services.user_service import TenantService
from api.db.services.llm_service import LLMBundle
from api.db.joint_services.tenant_model_service import get_model_config_by_type_and_name
from common.misc_utils import get_uuid
from common.constants import StatusEnum, LLMType


class WritingService(CommonService):
    """写作内容生成服务类"""

    @classmethod
    def generate_outline(cls, db: Session, user_input: str, content_type: str, language_style: str,
                         word_count: int, model: str, user_id: str, reference: str | None = None,
                         custom_outline_md: str | None = None) -> dict[str, Any]:
        """
        生成文章大纲

        参数:
            db: 数据库会话
            user_input: 用户输入的需求
            content_type: 文案类型
            language_style: 语言风格
            word_count: 文章篇幅/预期字数
            model: 使用的模型
            user_id: 用户ID
            reference: 用户提供的参考信息（可选）
            custom_outline_md: 用户提供的自定义Markdown大纲（可选）

        返回:
            Dict: 包含大纲结构的字典
        """
        try:
            # 获取用户所属租户
            tenants = TenantService.get_info_by(db, user_id)
            if not tenants:
                raise ValueError("找不到用户所属租户信息")

            tenant_id = tenants[0]["tenant_id"]

            # 创建项目
            project_id = get_uuid()

            # 如果用户提供了自定义大纲，则使用它，否则调用大模型生成大纲
            if custom_outline_md:
                # 使用用户提供的自定义大纲
                logging.info(f"使用用户提供的自定义大纲模板")

                # 从Markdown大纲中提取标题
                # 尝试从大纲的第一行提取标题（通常是# 开头的一级标题）
                lines = custom_outline_md.strip().split('\n')
                article_title = content_type

                if lines and lines[0].startswith('# '):
                    article_title = lines[0].replace('# ', '').strip()
                else:
                    article_title = f"{content_type}（{language_style}风格）"

                # 创建项目
                project = WritingProject(
                    id=project_id,
                    user_input=user_input,
                    content_type=content_type,
                    language_style=language_style,
                    word_count=word_count,
                    model=model,
                    user_id=user_id,
                    title=article_title,
                    reference=reference
                )
                db.add(project)

                # 解析自定义大纲并创建章节结构
                outline_structure = cls._parse_custom_outline(db, project_id, custom_outline_md)

                db.commit()
                db.refresh(project)

                return {
                    "outline_md": custom_outline_md,
                    "outline_structure": outline_structure,
                    "article_id": project_id
                }
            else:
                # 原有逻辑：使用大模型生成大纲
                # 创建LLM实例
                chat_config = get_model_config_by_type_and_name(db, tenant_id, LLMType.CHAT.value, model)
                llm_bundle = LLMBundle(db, tenant_id, chat_config)

                # 构建提示词
                # 添加参考信息到提示词中
                reference_text = ""
                if reference:
                    reference_text = f"""
                    参考信息：
                    {reference}

                    请在生成大纲时考虑上述参考信息。
                    """

                prompt = f"""
                请根据以下要求生成一篇文章大纲：

                用户需求：{user_input}
                文案类型：{content_type}
                语言风格：{language_style}
                文章篇幅：约{word_count}字

                {reference_text}

                请注意：
                1. 不是每个主章节都需要有子章节，可以根据内容需要灵活决定
                2. 每个章节（包括主章节和子章节）都应该有简短的内容摘要，用于指导写作方向
                3. 生成一个具有吸引力的文章标题，该标题应体现文章的主题和风格
                4. 只返回JSON格式的大纲内容，不要有额外的解释

                格式要求：
                ```json
                {{
                  "title": "文章标题",
                  "sections": [
                    {{
                      "title": "第一章标题",
                      "summary": "本章将探讨...(简短摘要)",
                      "children": [
                        {{
                          "title": "子章节标题",
                          "summary": "本节将分析...(简短摘要)"
                        }},
                        {{
                          "title": "子章节标题",
                          "summary": "本节将总结...(简短摘要)"
                        }}
                      ]
                    }},
                    {{
                      "title": "第二章标题",
                      "summary": "本章将介绍...(简短摘要)",
                      "children": []
                    }}
                  ]
                }}
                ```
                """

                # 在调用外部LLM前提交事务，确保已有变更不会因后续异常丢失
                try:
                    db.commit()
                except Exception:
                    pass

                # 调用LLM API
                content = llm_bundle.chat("", [{"role": "user", "content": prompt}], {})

                # 提取JSON部分
                json_match = re.search(r'```json\s*(\{.*?\})\s*```', content, re.DOTALL)
                if json_match:
                    content = json_match.group(1)
                else:
                    # 如果没有代码块标记，尝试直接解析整个内容
                    # 寻找第一个{和最后一个}之间的内容
                    start_idx = content.find('{')
                    end_idx = content.rfind('}')
                    if start_idx != -1 and end_idx != -1:
                        content = content[start_idx:end_idx + 1]

                # 解析为JSON
                outline_data = json.loads(content)

                # 从大模型输出中获取标题，如果没有则使用默认生成方式
                article_title = outline_data.get("title", f"{content_type}（{language_style}风格）")

                # 创建项目
                project = WritingProject(
                    id=project_id,
                    user_input=user_input,
                    content_type=content_type,
                    language_style=language_style,
                    word_count=word_count,
                    model=model,
                    user_id=user_id,
                    title=article_title,
                    reference=reference  # 存储参考信息
                )
                db.add(project)

                # 章节创建逻辑保持不变...
                for main_idx, section in enumerate(outline_data.get("sections", [])):
                    main_chapter_id = get_uuid()
                    main_chapter = WritingChapter(
                        id=main_chapter_id,
                        project_id=project_id,
                        title=section.get("title", "未命名章节"),
                        summary=section.get("summary", ""),
                        level=1,
                        parent_id=None,
                        order_index=main_idx
                    )
                    db.add(main_chapter)

                    # 创建子章节
                    for sub_idx, subsection in enumerate(section.get("children", [])):
                        sub_chapter = WritingChapter(
                            id=get_uuid(),
                            project_id=project_id,
                            title=subsection.get("title", "未命名子章节"),
                            summary=subsection.get("summary", ""),
                            level=2,
                            parent_id=main_chapter_id,
                            order_index=sub_idx
                        )
                        db.add(sub_chapter)

                db.commit()
                db.refresh(project)

                # 返回创建的大纲结构
                outline_structure = ChapterService.get_project_outline(db, project_id)

                return {
                    "outline_md": ChapterService.get_markdown_outline(db, project_id),
                    "outline_structure": outline_structure,
                    "article_id": project_id
                }

        except Exception as e:
            db.rollback()
            logging.error(f"生成大纲出错: {str(e)}", exc_info=True)
            raise e

    @classmethod
    def _prepare_context_for_section(cls, db: Session, chapter_id: str) -> dict[str, list[dict[str, Any]]]:
        """
        准备章节写作的上下文信息

        策略:
        1. 相邻章节提供完整内容
        2. 相关章节提供摘要
        3. 其他章节提供标题

        参数:
            db: 数据库会话
            chapter_id: 当前章节ID

        返回:
            Dict: 上下文信息
        """
        chapter = db.query(WritingChapter).filter(
            WritingChapter.id == chapter_id,
            WritingChapter.status == StatusEnum.VALID.value
        ).first()

        if not chapter:
            raise ValueError(f"未找到章节 ID: {chapter_id}")

        # 获取项目中的所有章节，按顺序排列
        all_chapters = db.query(WritingChapter).filter(
            WritingChapter.project_id == chapter.project_id,
            WritingChapter.status == StatusEnum.VALID.value
        ).order_by(
            WritingChapter.level.asc(),
            WritingChapter.order_index.asc()
        ).all()

        # 找到当前章节在列表中的位置
        current_idx = -1
        for i, ch in enumerate(all_chapters):
            if ch.id == chapter_id:
                current_idx = i
                break

        if current_idx == -1:
            raise ValueError(f"无法确定章节在项目中的位置")

        # 获取已完成章节的内容
        contents = db.query(WritingChapterContent).filter(
            WritingChapterContent.status == StatusEnum.VALID.value,
            WritingChapterContent.chapter_id.in_([ch.id for ch in all_chapters])
        ).all()

        # 建立章节ID到内容的映射
        content_map = {content.chapter_id: content.content for content in contents}

        # 当前章节层级和父章节ID
        current_level = chapter.level
        current_parent_id = chapter.parent_id

        # 初始化上下文
        context = {
            "full_content": [],  # 完整内容的章节
            "summary": [],  # 摘要内容的章节
            "titles_only": []  # 仅标题的章节
        }

        # 处理所有章节
        for i, ch in enumerate(all_chapters):
            # 跳过当前章节
            if ch.id == chapter_id:
                continue

            # 如果章节没有内容，跳过
            if ch.id not in content_map:
                continue

            # 确定与当前章节的关系
            is_adjacent = False
            is_related = False

            # 相邻章节判断逻辑
            if abs(i - current_idx) == 1:
                is_adjacent = True

            # 相关章节判断逻辑
            if current_level == 1 and ch.level == 2 and ch.parent_id == chapter_id:
                # 当前是主章节，相关章节为其子章节
                is_related = True
            elif current_level == 2 and ch.level == 2 and ch.parent_id == current_parent_id:
                # 当前是子章节，相关章节为同一父章节下的其他子章节
                is_related = True
            elif current_level == 2 and ch.level == 1 and ch.id == current_parent_id:
                # 当前是子章节，相关章节为其父章节
                is_related = True

            # 根据关系类型添加到上下文
            if is_adjacent:
                context["full_content"].append({
                    "id": ch.id,
                    "title": ch.title,
                    "content": content_map[ch.id]
                })
            elif is_related:
                context["summary"].append({
                    "id": ch.id,
                    "title": ch.title,
                    "summary": content_map[ch.id][:300] + "..." if len(content_map[ch.id]) > 300 else content_map[ch.id]
                })
            else:
                context["titles_only"].append({
                    "id": ch.id,
                    "title": ch.title
                })

        return context

    @classmethod
    def _parse_custom_outline(cls, db: Session, project_id: str, markdown_outline: str) -> dict[str, Any]:
        """
        解析用户提供的自定义Markdown大纲，创建相应的章节结构

        参数:
            db: 数据库会话
            project_id: 项目ID
            markdown_outline: Markdown格式的大纲内容

        返回:
            Dict: 创建的大纲结构
        """
        lines = markdown_outline.strip().split('\n')

        # 跟踪当前的主章节ID和索引
        current_main_chapter_id = None
        main_idx = 0
        sub_idx = 0

        # 逐行解析Markdown
        for line in lines:
            line = line.strip()

            # 忽略空行和一级标题（通常是文章标题）
            if not line or line.startswith('# '):
                continue

            # 解析二级标题（主章节）
            if line.startswith('## '):
                title = line.replace('## ', '').strip()
                # 提取可能的编号前缀，如 "1. 章节标题"
                if '.' in title and title.split('.')[0].strip().isdigit():
                    title = title.split('.', 1)[1].strip()

                main_chapter_id = get_uuid()
                summary = ""

                # 查找下一行是否为摘要
                next_line_idx = lines.index(line) + 1
                if next_line_idx < len(lines) and not lines[next_line_idx].startswith('#') and not lines[
                    next_line_idx].startswith('-'):
                    summary = lines[next_line_idx].strip()
                    if summary.startswith('摘要：'):
                        summary = summary[3:].strip()

                main_chapter = WritingChapter(
                    id=main_chapter_id,
                    project_id=project_id,
                    title=title,
                    summary=summary,
                    level=1,
                    parent_id=None,
                    order_index=main_idx
                )
                db.add(main_chapter)

                current_main_chapter_id = main_chapter_id
                main_idx += 1
                sub_idx = 0

            # 解析三级标题或列表项（子章节）
            elif (line.startswith('### ') or line.startswith('- ')) and current_main_chapter_id:
                if line.startswith('### '):
                    title = line.replace('### ', '').strip()
                else:
                    title = line.replace('- ', '').strip()

                # 提取可能的编号和摘要
                summary = ""

                # 处理可能的格式："1.1 子章节标题 (摘要：...)"
                if ' (摘要：' in title:
                    title_parts = title.split(' (摘要：', 1)
                    title = title_parts[0].strip()
                    summary = title_parts[1].rstrip(')').strip()

                # 处理可能的数字编号
                if '.' in title and title.split('.')[0].strip().isdigit():
                    # 可能是 "1.1 子章节标题" 格式
                    title = title.split('.', 1)[1].strip()
                    if '.' in title and title.split('.')[0].strip().isdigit():
                        title = title.split('.', 1)[1].strip()

                sub_chapter = WritingChapter(
                    id=get_uuid(),
                    project_id=project_id,
                    title=title,
                    summary=summary,
                    level=2,
                    parent_id=current_main_chapter_id,
                    order_index=sub_idx
                )
                db.add(sub_chapter)

                sub_idx += 1

        # 提交更改
        db.commit()

        # 返回创建的大纲结构
        return ChapterService.get_project_outline(db, project_id)

    @classmethod
    @classmethod
    def get_completed_article(cls, db: Session, project_id: str) -> tuple[str, int]:
        """
        获取完整的文章内容

        参数:
            db: 数据库会话
            project_id: 项目ID

        返回:
            Tuple[str, int]: (完整文章内容, 字数)
        """
        # 获取项目信息
        project = db.query(WritingProject).filter(
            WritingProject.id == project_id,
            WritingProject.status == StatusEnum.VALID.value
        ).first()

        if not project:
            raise ValueError(f"未找到项目 ID: {project_id}")

        # 获取所有章节
        chapters = db.query(WritingChapter).filter(
            WritingChapter.project_id == project_id,
            WritingChapter.status == StatusEnum.VALID.value
        ).order_by(
            WritingChapter.level.asc(),
            WritingChapter.order_index.asc()
        ).all()

        # 获取所有章节内容
        contents = db.query(WritingChapterContent).filter(
            WritingChapterContent.status == StatusEnum.VALID.value,
            WritingChapterContent.chapter_id.in_([ch.id for ch in chapters])
        ).all()

        # 建立章节ID到内容的映射
        content_map = {content.chapter_id: content.content for content in contents}

        # 检查是否有缺失章节
        chapter_ids = [ch.id for ch in chapters]
        content_ids = list(content_map.keys())
        missing_chapters = [ch_id for ch_id in chapter_ids if ch_id not in content_ids]

        if missing_chapters:
            missing_titles = []
            for ch_id in missing_chapters:
                for ch in chapters:
                    if ch.id == ch_id:
                        missing_titles.append(ch.title)
                        break

            if missing_titles:
                return f"文章尚未完成，缺少以下章节：{', '.join(missing_titles)}", 0

        # 使用项目的title字段，如果为空则使用备选方案
        title = project.title if project.title else f"关于{project.content_type}的{project.language_style}风格文章"

        # 构建完整文章
        article_text = f"# {title}\n\n"

        # 处理主章节和子章节
        main_chapters = {}
        sub_chapters = {}

        for ch in chapters:
            if ch.level == 1:
                main_chapters[ch.id] = ch
            else:
                if ch.parent_id not in sub_chapters:
                    sub_chapters[ch.parent_id] = []
                sub_chapters[ch.parent_id].append(ch)

        # 按顺序组装文章
        for main_id, main_ch in sorted(
                main_chapters.items(),
                key=lambda x: x[1].order_index
        ):
            article_text += f"## {main_ch.title}\n\n"

            if main_id in content_map:
                article_text += f"{content_map[main_id]}\n\n"

            # 添加子章节
            if main_id in sub_chapters:
                for sub_ch in sorted(
                        sub_chapters[main_id],
                        key=lambda x: x.order_index
                ):
                    article_text += f"### {sub_ch.title}\n\n"

                    if sub_ch.id in content_map:
                        article_text += f"{content_map[sub_ch.id]}\n\n"

        # 计算字数（简单按照字符数计算）
        word_count = len(article_text)

        return article_text, word_count

    @classmethod
    def write_section_improved(cls, db: Session, chapter_id: str) -> dict:
        """
        改进的章节写作方法（自动处理参考资料和子章节）

        参数:
            db: 数据库会话
            chapter_id: 章节ID

        返回:
            dict: 生成的章节内容及相关信息
        """
        # 获取章节信息
        chapter = db.query(WritingChapter).filter(
            WritingChapter.id == chapter_id,
            WritingChapter.status == StatusEnum.VALID.value
        ).first()

        if not chapter:
            return {
                "retcode": 404,
                "retmsg": f"未找到章节 ID: {chapter_id}",
                "data": None
            }

        # 获取项目信息
        project = db.query(WritingProject).filter_by(id=chapter.project_id).first()
        if not project:
            return {
                "retcode": 404,
                "retmsg": f"未找到项目 ID: {chapter.project_id}",
                "data": None
            }

        try:
            # 获取用户所属租户
            tenants = TenantService.get_info_by(db, project.user_id)
            if not tenants:
                return {
                    "retcode": 500,
                    "retmsg": "找不到用户所属租户信息",
                    "data": None
                }

            tenant_id = tenants[0]["tenant_id"]

            # 创建LLM实例
            chat_config = get_model_config_by_type_and_name(db, tenant_id, LLMType.CHAT.value, project.model)
            llm_bundle = LLMBundle(db, tenant_id, chat_config)

            # 准备上下文（复用现有逻辑）
            context = cls._prepare_context_for_section(db, chapter_id)

            # 构建改进的提示词（自动获取参考资料）
            prompt = cls._build_improved_prompt(db, chapter_id, context)

            # 调用LLM API
            content = llm_bundle.chat("", [{"role": "user", "content": prompt}], {})

            # 保存到数据库
            content_obj = db.query(WritingChapterContent).filter(
                WritingChapterContent.chapter_id == chapter_id,
                WritingChapterContent.status == StatusEnum.VALID.value
            ).first()

            if content_obj:
                # 更新现有内容
                content_obj.content = content
                content_obj.update_time = cls.current_timestamp()
                content_obj.update_date = cls.current_datetime()
            else:
                # 创建新内容
                content_obj = WritingChapterContent(
                    id=get_uuid(),
                    chapter_id=chapter_id,
                    content=content,
                    update_time=cls.current_timestamp(),
                    update_date=cls.current_datetime()
                )
                db.add(content_obj)

            # 更新章节的更新时间
            chapter.update_time = cls.current_timestamp()
            chapter.update_date = cls.current_datetime()

            db.commit()

            # 返回结果（直接返回字典而不是JSONResponse）
            return {
                "retcode": 0,
                "retmsg": "success",
                "data": {
                    "type": "complete",
                    "section_id": chapter_id,
                    "section_title": chapter.title,
                    "content": content
                }
            }
        except Exception as e:
            db.rollback()
            logging.error(f"写作章节内容失败: {str(e)}", exc_info=True)
            return {
                "retcode": 500,
                "retmsg": f"章节写作失败: {str(e)}",
                "data": {"type": "error"}
            }

    @classmethod
    async def write_section_stream_improved(cls, db: Session, chapter_id: str):
        """
        改进的流式章节写作方法（自动处理参考资料和子章节）

        参数:
            db: 数据库会话
            chapter_id: 章节ID

        返回:
            AsyncGenerator: 生成内容的流
        """
        from api.db.db_models import db_connection
        
        # 获取章节信息
        chapter = db.query(WritingChapter).filter(
            WritingChapter.id == chapter_id,
            WritingChapter.status == StatusEnum.VALID.value
        ).first()

        if not chapter:
            yield f"data: {json.dumps({'retcode': 404, 'retmsg': f'找不到指定的章节: {chapter_id}', 'data': {'type': 'error'}})}\n\n"
            return

        # 获取项目信息
        project = db.query(WritingProject).filter_by(id=chapter.project_id).first()
        if not project:
            yield f"data: {json.dumps({'retcode': 404, 'retmsg': f'找不到项目: {chapter.project_id}', 'data': {'type': 'error'}})}\n\n"
            return

        # 提前提取需要的数据，避免在LLM调用期间持有数据库连接
        chapter_title = chapter.title
        project_id = chapter.project_id
        user_id = project.user_id
        model = project.model

        try:
            # 获取用户所属租户
            tenants = TenantService.get_info_by(db, user_id)
            if not tenants:
                yield f"data: {json.dumps({'retcode': 500, 'retmsg': '找不到用户所属租户信息', 'data': {'type': 'error'}})}\n\n"
                return

            tenant_id = tenants[0]["tenant_id"]

            # 创建LLM实例（注意：LLMBundle可能会缓存db引用，需要后续处理）
            chat_config = get_model_config_by_type_and_name(db, tenant_id, LLMType.CHAT.value, model)
            llm_bundle = LLMBundle(db, tenant_id, chat_config)

            # 准备上下文（复用现有逻辑）
            context = cls._prepare_context_for_section(db, chapter_id)

            # 构建改进的提示词（自动获取参考资料）
            prompt = cls._build_improved_prompt(db, chapter_id, context)

            # 发送初始元数据
            metadata = {
                "retcode": 0,
                "retmsg": "success",
                "data": {
                    "type": "metadata",
                    "section_id": chapter_id,
                    "section_title": chapter_title
                }
            }
            yield f"data: {json.dumps(metadata, ensure_ascii=False)}\n\n"

            # 存储最后得到的完整内容
            final_content = ""

            # 关键修复：在LLM调用之前关闭数据库连接，释放回连接池
            # db.commit() 只是提交事务，不会释放连接！
            # 必须调用 db.close() 才能真正归还连接到连接池
            try:
                db.commit()
                db.close()  # 关闭连接，归还到连接池
            except Exception:
                pass

            # 发送流式内容到前端（此时不持有数据库连接）
            async for content in llm_bundle.async_chat_streamly("", [{"role": "user", "content": prompt}], {}):
                # 检查返回的是否是token数（最后一个返回值）
                if isinstance(content, int):
                    # 跳过token数输出
                    continue

                # 保存最新的完整内容
                final_content = content

                # 发送内容块到前端
                data = {
                    "retcode": 0,
                    "retmsg": "success",
                    "data": {
                        "type": "content",
                        "content": content
                    }
                }
                yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0.01)  # 小延迟以保证流畅传输

            # 流式生成完成后，使用 db_connection 上下文管理器保存内容
            if final_content:
                with db_connection() as new_db:
                    # 保存生成的内容到数据库
                    content_obj = new_db.query(WritingChapterContent).filter(
                        WritingChapterContent.chapter_id == chapter_id,
                        WritingChapterContent.status == StatusEnum.VALID.value
                    ).first()

                    if content_obj:
                        # 更新现有内容
                        content_obj.content = final_content
                        content_obj.update_time = cls.current_timestamp()
                        content_obj.update_date = cls.current_datetime()
                    else:
                        # 创建新内容
                        content_obj = WritingChapterContent(
                            id=get_uuid(),
                            chapter_id=chapter_id,
                            content=final_content,
                            update_time=cls.current_timestamp(),
                            update_date=cls.current_datetime()
                        )
                        new_db.add(content_obj)

                    # 更新章节的更新时间
                    chapter_to_update = new_db.query(WritingChapter).filter(
                        WritingChapter.id == chapter_id
                    ).first()
                    if chapter_to_update:
                        chapter_to_update.update_time = cls.current_timestamp()
                        chapter_to_update.update_date = cls.current_datetime()

                    new_db.commit()

            # 发送完成信号
            complete_data = {
                "retcode": 0,
                "retmsg": "success",
                "data": {
                    "type": "complete",
                    "section_id": chapter_id,
                    "section_title": chapter.title
                }
            }
            yield f"data: {json.dumps(complete_data, ensure_ascii=False)}\n\n"

        except Exception as e:
            logging.error(f"流式写作章节内容失败: {str(e)}", exc_info=True)
            error_data = {
                "retcode": 500,
                "retmsg": f"章节写作失败: {str(e)}",
                "data": {
                    "type": "error"
                }
            }
            yield f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n"

    @classmethod
    def _build_improved_prompt(cls, db: Session, chapter_id: str, context: dict[str, list[dict[str, Any]]]) -> str:
        """
        构建改进的章节写作提示词（自动获取参考资料和考虑子章节）

        参数:
            db: 数据库会话
            chapter_id: 章节ID
            context: 上下文信息

        返回:
            str: 写作提示词
        """
        # 获取章节信息
        chapter = db.query(WritingChapter).filter_by(id=chapter_id).first()
        if not chapter:
            raise ValueError(f"未找到章节 ID: {chapter_id}")

        # 获取项目信息
        project = db.query(WritingProject).filter_by(id=chapter.project_id).first()
        if not project:
            raise ValueError(f"未找到项目 ID: {chapter.project_id}")

        # 自动获取章节的参考资料
        db_references = db.query(WritingReferenceMaterial).filter(
            WritingReferenceMaterial.chapter_id == chapter_id,
            WritingReferenceMaterial.status == StatusEnum.VALID.value
        ).order_by(WritingReferenceMaterial.order_index.asc()).all()

        # 构建参考资料文本
        reference_text = ""
        if db_references:
            reference_text = "### 参考资料：\n\n"
            for i, ref in enumerate(db_references):
                reference_text += f"{i + 1}. **{ref.title}**\n"
                reference_text += f"   {ref.content}\n\n"

        # 获取子章节信息（如果是主章节）
        sub_chapters_text = ""
        if chapter.level == 1:
            sub_chapters = db.query(WritingChapter).filter(
                WritingChapter.parent_id == chapter_id,
                WritingChapter.status == StatusEnum.VALID.value
            ).order_by(WritingChapter.order_index.asc()).all()

            if sub_chapters:
                sub_chapters_text = "### 子章节结构：\n"
                for i, sub in enumerate(sub_chapters):
                    sub_chapters_text += f"{i + 1}. **{sub.title}**\n"
                    if sub.summary:
                        sub_chapters_text += f"   摘要：{sub.summary}\n\n"

        # 获取完整大纲
        all_chapters = db.query(WritingChapter).filter(
            WritingChapter.project_id == chapter.project_id,
            WritingChapter.status == StatusEnum.VALID.value
        ).order_by(
            WritingChapter.level.asc(),
            WritingChapter.order_index.asc()
        ).all()

        # 构建完整大纲文本
        outline_text = f"# 文章大纲\n\n"

        # 处理主章节和子章节
        main_chapters = {}
        sub_chapters = {}

        for ch in all_chapters:
            if ch.level == 1:
                main_chapters[ch.id] = ch
            else:
                if ch.parent_id not in sub_chapters:
                    sub_chapters[ch.parent_id] = []
                sub_chapters[ch.parent_id].append(ch)

        # 按顺序生成大纲
        for main_id, main_ch in sorted(
                main_chapters.items(),
                key=lambda x: x[1].order_index
        ):
            outline_text += f"## {main_ch.order_index + 1}. {main_ch.title}\n"
            if main_ch.summary:
                outline_text += f"摘要：{main_ch.summary}\n"

            # 添加子章节
            if main_id in sub_chapters:
                for sub_ch in sorted(
                        sub_chapters[main_id],
                        key=lambda x: x.order_index
                ):
                    outline_text += f"- {main_ch.order_index + 1}.{sub_ch.order_index + 1} {sub_ch.title}"
                    if sub_ch.summary:
                        outline_text += f" (摘要：{sub_ch.summary})"
                    outline_text += "\n"

            outline_text += "\n"

        # 构建当前需要写作的章节信息
        current_section_text = ""
        if chapter.level == 1:
            # 主章节
            current_section_text = f"## {chapter.title}\n"
            if chapter.summary:
                current_section_text += f"摘要：{chapter.summary}\n"

            # 添加子章节
            if chapter.id in sub_chapters:
                current_section_text += "子章节：\n"
                for sub_ch in sorted(
                        sub_chapters[chapter.id],
                        key=lambda x: x.order_index
                ):
                    current_section_text += f"- {sub_ch.title}"
                    if sub_ch.summary:
                        current_section_text += f" (摘要：{sub_ch.summary})"
                    current_section_text += "\n"
        else:
            # 子章节
            parent_ch = main_chapters.get(chapter.parent_id)
            if parent_ch:
                current_section_text = f"从 ## {parent_ch.title} 中的子章节：\n"
                current_section_text += f"- {chapter.title}"
                if chapter.summary:
                    current_section_text += f" (摘要：{chapter.summary})"

        # 构建已完成章节的上下文
        context_text = ""

        # 完整内容的章节
        if context["full_content"]:
            context_text += "### 已完成章节（完整内容）：\n\n"
            for item in context["full_content"]:
                context_text += f"#### {item['title']}\n\n{item['content']}\n\n"

        # 摘要内容的章节
        if context["summary"]:
            context_text += "### 已完成章节（摘要）：\n\n"
            for item in context["summary"]:
                context_text += f"#### {item['title']}\n\n摘要：{item['summary']}\n\n"

        # 仅标题的章节
        if context["titles_only"]:
            context_text += "### 其他已完成章节（仅标题）：\n\n"
            titles_list = [f"{item['title']}" for item in context["titles_only"]]
            context_text += ", ".join(titles_list) + "\n\n"

        # 计算章节的预期字数
        total_chapters = len(all_chapters)
        chapter_word_count = project.word_count // total_chapters if total_chapters > 0 else project.word_count

        # 完整提示词
        prompt = f"""
    你现在是一位专业写作助手，请根据以下大纲为用户撰写文章的特定章节。

    用户输入的需求：{project.user_input}
    文案类型：{project.content_type}
    语言风格：{project.language_style}
    文章篇幅：约{project.word_count}字

    ## 完整大纲：
    {outline_text}

    ## 当前需要撰写的章节：
    {current_section_text}

    {sub_chapters_text}

    {context_text}

    {reference_text}

    请注意：
    1. {'如果这是包含子章节的主章节，请创作一个统一的内容，涵盖所有子章节的要点，并为每个子主题提供适当的过渡。' if chapter.level == 1 and sub_chapters else '请根据章节标题和摘要创作完整内容。'}
    2. 保持与已完成章节的连贯性和一致性
    3. 遵循指定的语言风格
    4. 章节长度控制在{chapter_word_count}字左右
    5. 注重逻辑性和可读性
    6. 直接以当前章节的内容开始，不要包含标题或编号
    7. 利用章节摘要作为写作指导
    8. 充分利用所提供的参考资料，但不要直接复制

    请直接撰写章节内容，无需包含大纲或额外说明。
    """

        return prompt