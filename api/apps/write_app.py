# coding=utf-8
"""
@project: writing_system
@file： routes.py
@desc: API路由定义
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, File, Form, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from api.apps import manager
from common.constants import StatusEnum
from api.db.db_models import WritingChapter, WritingProject, WritingReferenceMaterial, get_db
from api.db.services.project_service import ProjectService
from api.db.services.chapter_service import ChapterService
from api.db.services.reference_service import ReferenceService
from api.db.services.writing_service import WritingService
from api.utils.api_utils import get_json_result
from pydantic import BaseModel, Field
from typing import Any

router = APIRouter()

class OutlineRequest(BaseModel):
    """大纲生成请求"""
    user_input: str = Field(..., description="用户输入的需求")
    content_type: str = Field(..., description="文案类型")
    language_style: str = Field(..., description="语言风格")
    word_count: int = Field(..., description="文章篇幅/预期字数")
    model: str = Field(..., description="使用的模型")
    reference: str | None = Field(default=None, description="用户提供的参考信息")
    custom_outline_md: str | None = Field(default=None, description="用户提供的自定义Markdown大纲模板")


class OutlineResponse(BaseModel):
    """大纲生成响应"""
    outline_md: str = Field(..., description="Markdown格式的大纲")
    outline_structure: dict[str, Any] = Field(..., description="结构化的大纲数据")
    article_id: str = Field(..., description="文章唯一标识符")


class SectionWriteRequest(BaseModel):
    """章节写作请求"""
    section_identifier: str = Field(..., description="章节标识符，可以是ID")
    reference_materials: list[str] = Field(default=None, description="参考材料")


class SectionWriteResponse(BaseModel):
    """章节写作响应"""
    section_id: str = Field(..., description="章节ID")
    section_title: str = Field(..., description="章节标题")
    content: str = Field(..., description="章节内容")


class ChapterRequest(BaseModel):
    """章节创建/更新请求"""
    title: str = Field(..., description="章节标题")
    summary: str = Field(default="", description="章节摘要")
    parent_id: str | None = Field(default=None, description="父章节ID（如果是子章节）")
    order_index: int | None = Field(default=None, description="排序索引")


class ReferenceRequest(BaseModel):
    """参考资料创建/更新请求"""
    title: str = Field(..., description="参考资料标题")
    content: str = Field(..., description="参考资料内容")
    source: str | None = Field(default="", description="来源")
    type: str = Field(default="text", description="类型(text,url,file)")


# ========== 项目相关路由 ==========#

@router.post("/generate-outline", response_model=OutlineResponse)
def generate_outline(
        request: OutlineRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """生成文章大纲"""
    try:
        # 生成大纲
        outline_result = WritingService.generate_outline(
            db=db,
            user_input=request.user_input,
            content_type=request.content_type,
            language_style=request.language_style,
            word_count=request.word_count,
            model=request.model,
            user_id=user.id,
            reference=request.reference,
            custom_outline_md=request.custom_outline_md  # 传递自定义大纲参数
        )

        return {
            "outline_md": outline_result["outline_md"],
            "outline_structure": outline_result["outline_structure"],
            "article_id": outline_result["article_id"]
        }

    except Exception as e:
        logging.error(f"生成大纲出错: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"生成大纲失败: {str(e)}")


@router.get("/api/projects")
def get_projects(
        page: int = 1,
        limit: int = 10,
        orderby: str = "create_time",
        desc: bool = True,
        keywords: str = None,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """获取用户的写作项目列表"""
    try:
        projects, total = ProjectService.get_list(
            db=db,
            user_id=user.id,
            page_number=page,
            items_per_page=limit,
            orderby=orderby,
            sort_desc=desc,
            keywords=keywords
        )

        return get_json_result(data={
            "items": projects,
            "total": total,
            "page": page,
            "limit": limit
        })
    except Exception as e:
        logging.error(f"获取项目列表出错: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取项目列表失败: {str(e)}")


@router.get("/api/projects/{project_id}")
def get_project_detail(
        project_id: str,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """获取项目详情"""
    try:
        project_detail = ProjectService.get_project_details(
            db=db,
            project_id=project_id,
            user_id=user.id
        )

        return get_json_result(data=project_detail)
    except Exception as e:
        logging.error(f"获取项目详情出错: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取项目详情失败: {str(e)}")


@router.put("/api/projects/{project_id}")
def update_project(
        project_id: str,
        project_data: dict,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """更新项目信息"""
    try:
        updated_project = ProjectService.update_project(
            db=db,
            project_id=project_id,
            project_data=project_data,
            user_id=user.id
        )

        return get_json_result(data=updated_project.to_dict())
    except Exception as e:
        logging.error(f"更新项目出错: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"更新项目失败: {str(e)}")


@router.api_route("/api/projects/{project_id}/outline", methods=["PUT", "POST"])
def update_project_outline(
        project_id: str,
        outline_data: dict,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    更新项目的完整大纲结构

    接收一个完整的大纲JSON，处理章节的新增、更新、删除和重排序。
    前端可以在用户实时编辑大纲时调用此接口，无需单独的确认按钮。

    请求体格式示例：
    {
        "sections": [
            {
                "id": "章节ID(可选，新章节不提供)",
                "title": "章节标题",
                "summary": "章节摘要",
                "children": [
                    {
                        "id": "子章节ID(可选，新章节不提供)",
                        "title": "子章节标题",
                        "summary": "子章节摘要"
                    }
                ]
            }
        ]
    }
    """
    try:
        # 确保有项目访问权限
        project = db.query(WritingProject).filter(
            WritingProject.id == project_id,
            WritingProject.status == StatusEnum.VALID.value,
            WritingProject.user_id == user.id
        ).first()

        if not project:
            raise HTTPException(status_code=404, detail=f"项目不存在或无权访问: {project_id}")

        # 执行大纲更新
        updated_outline = ChapterService.update_project_outline(
            db=db,
            project_id=project_id,
            outline_data=outline_data
        )

        return get_json_result(data={
            "message": "大纲已成功更新",
            "outline": updated_outline
        })
    except Exception as e:
        logging.error(f"更新项目大纲失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"更新项目大纲失败: {str(e)}")


@router.delete("/api/projects/{project_id}")
def delete_project(
        project_id: str,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """删除项目"""
    try:
        success = ProjectService.delete_project(
            db=db,
            project_id=project_id,
            user_id=user.id
        )

        if not success:
            raise HTTPException(status_code=404, detail=f"项目不存在或已删除")

        return get_json_result(data={"message": "项目已成功删除"})
    except Exception as e:
        logging.error(f"删除项目出错: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"删除项目失败: {str(e)}")


# ========== 章节相关路由 ==========#

@router.post("/api/projects/{project_id}/sections")
def add_chapter(
        project_id: str,
        chapter_data: ChapterRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """添加章节"""
    try:
        # 确保有项目访问权限
        project = ProjectService.get_project_details(db, project_id, user.id)

        # 创建章节数据
        chapter_dict = chapter_data.model_dump()
        chapter_dict["project_id"] = project_id

        new_chapter = ChapterService.create_chapter(
            db=db,
            chapter_data=chapter_dict
        )

        return get_json_result(data=new_chapter.to_dict())
    except Exception as e:
        logging.error(f"添加章节出错: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"添加章节失败: {str(e)}")


@router.get("/api/projects/{project_id}/sections")
def get_project_chapters(
        project_id: str,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """获取项目的章节列表"""
    try:
        # 确保有项目访问权限
        project = ProjectService.get_project_details(db, project_id, user.id)

        # 获取项目的大纲结构
        outline = ChapterService.get_project_outline(db, project_id)

        return get_json_result(data=outline)
    except Exception as e:
        logging.error(f"获取章节列表出错: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取章节列表失败: {str(e)}")


@router.delete("/api/chapters/{chapter_id}/references")
def delete_chapter_references(
        chapter_id: str,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """删除章节的所有参考资料"""
    try:
        # 验证章节是否存在
        chapter = db.query(WritingChapter).filter(
            WritingChapter.id == chapter_id,
            WritingChapter.status == StatusEnum.VALID.value
        ).first()

        if not chapter:
            return get_json_result(
                data=False,
                retmsg=f"章节不存在: {chapter_id}",
                retcode=404
            )

        # 验证项目所有权
        project = db.query(WritingProject).filter(
            WritingProject.id == chapter.project_id,
            WritingProject.status == StatusEnum.VALID.value
        ).first()

        if not project or project.user_id != user.id:
            return get_json_result(
                data=False,
                retmsg="无权操作此章节的参考资料",
                retcode=403
            )

        # 删除章节的所有参考资料
        count = ReferenceService.delete_chapter_references(db, chapter_id)

        return get_json_result(data={
            "message": f"成功删除 {count} 个参考资料",
            "deleted_count": count
        })
    except Exception as e:
        logging.error(f"删除章节参考资料失败: {str(e)}", exc_info=True)
        return get_json_result(
            data=False,
            retmsg=f"删除章节参考资料失败: {str(e)}",
            retcode=500
        )


@router.put("/api/projects/{project_id}/outline/reorder")
def reorder_outline_sections(
        project_id: str,
        sections_order: list[dict[str, Any]],
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """重新排序大纲章节"""
    try:
        # 确保有项目访问权限
        project = ProjectService.get_project_details(db, project_id, user.id)

        # 更新章节顺序
        success = ChapterService.reorder_chapters(db, sections_order)

        if not success:
            raise HTTPException(status_code=500, detail="排序章节失败")

        # 获取更新后的大纲
        updated_outline = ChapterService.get_project_outline(db, project_id)

        return get_json_result(data={
            "message": "章节顺序已更新",
            "outline": updated_outline
        })
    except Exception as e:
        logging.error(f"重新排序章节出错: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"重新排序章节失败: {str(e)}")


# ========== 参考资料相关路由 ==========#

@router.post("/api/reference-materials/parse")
async def parse_reference_material(
        chapter_id: str = Form(..., description="章节ID"),
        title: str = Form(None, description="参考资料标题（可选）"),
        file: UploadFile = File(None, description="上传文件（可选）"),
        url: str = Form(None, description="网页URL（可选）"),
        reference: str = Form(None, description="文本内容（可选）"),
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """解析参考资料"""
    try:
        if file:
            # 读取文件内容
            file_content = await file.read()
            file_name = file.filename

            # 检查文件是否为空
            if not file_content:
                return get_json_result(
                    data=False,
                    retmsg="文件内容为空",
                    retcode=400
                )

            # 解析文件内容
            content = ReferenceService.parse_file_content(file_content, file_name)

            # 检查解析结果
            if not content or content.startswith("解析文件失败") or content.startswith("无法解析"):
                return get_json_result(
                    data=False,
                    retmsg=f"文件解析失败: {content}",
                    retcode=400
                )

            # 如果未提供标题，使用文件名
            if not title:
                title = file_name

            source = file_name
            ref_type = "file"
        elif url:
            # 验证URL
            if not url or not url.startswith(('http://', 'https://')):
                return get_json_result(
                    data=False,
                    retmsg="无效的URL格式",
                    retcode=400
                )

            # 解析URL内容
            content = ReferenceService.parse_url_content(url)

            # 检查解析结果
            if not content or content.startswith("解析URL内容失败"):
                return get_json_result(
                    data=False,
                    retmsg=f"URL解析失败: {content}",
                    retcode=400
                )

            # 如果未提供标题，使用URL
            if not title:
                title = url

            source = url
            ref_type = "url"
        elif reference:
            # 直接使用用户提供的文本内容
            content = reference

            # 如果未提供标题，使用默认标题
            if not title:
                title = "用户提供的参考文本"

            source = "用户输入"
            ref_type = "text"
        else:
            return get_json_result(
                data=False,
                retmsg="请提供文件、URL或文本内容",
                retcode=400
            )

        # 验证章节ID
        chapter = db.query(WritingChapter).filter(
            WritingChapter.id == chapter_id,
            WritingChapter.status == StatusEnum.VALID.value
        ).first()

        if not chapter:
            return get_json_result(
                data=False,
                retmsg=f"章节不存在: {chapter_id}",
                retcode=404
            )

        # 创建参考资料
        reference_data = {
            "chapter_id": chapter_id,
            "title": title,
            "content": content,
            "source": source,
            "type": ref_type
        }

        new_reference = ReferenceService.add_reference(db, reference_data)

        # 返回参考资料信息（不包括完整内容，只返回摘要）
        return get_json_result(data={
            "id": new_reference.id,
            "title": new_reference.title,
            "summary": ReferenceService.get_summary(content),
            "source": new_reference.source,
            "type": new_reference.type
        })
    except Exception as e:
        logging.error(f"解析参考资料失败: {str(e)}", exc_info=True)
        return get_json_result(
            data=False,
            retmsg=f"解析参考资料失败: {str(e)}",
            retcode=500
        )


@router.get("/api/chapters/{chapter_id}/references")
def get_chapter_references(
        chapter_id: str,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """获取章节的参考资料列表"""
    try:
        references = ReferenceService.get_by_chapter_id(db, chapter_id)

        # 处理摘要
        for ref in references:
            ref["summary"] = ReferenceService.get_summary(ref["content"])
            ref.pop("content", None)  # 移除完整内容

        return get_json_result(data=references)
    except Exception as e:
        logging.error(f"获取参考资料失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取参考资料失败: {str(e)}")


@router.delete("/api/references/{reference_id}")
def delete_reference(
        reference_id: str,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """删除单个参考资料"""
    try:
        # 获取参考资料信息
        reference = db.query(WritingReferenceMaterial).filter(
            WritingReferenceMaterial.id == reference_id,
            WritingReferenceMaterial.status == StatusEnum.VALID.value
        ).first()

        if not reference:
            return get_json_result(
                data=False,
                retmsg=f"参考资料不存在: {reference_id}",
                retcode=404
            )

        # 获取章节信息
        chapter = db.query(WritingChapter).filter(
            WritingChapter.id == reference.chapter_id,
            WritingChapter.status == StatusEnum.VALID.value
        ).first()

        if not chapter:
            return get_json_result(
                data=False,
                retmsg="参考资料关联的章节不存在",
                retcode=404
            )

        # 验证项目所有权
        project = db.query(WritingProject).filter(
            WritingProject.id == chapter.project_id,
            WritingProject.status == StatusEnum.VALID.value
        ).first()

        if not project or project.user_id != user.id:
            return get_json_result(
                data=False,
                retmsg="无权删除此参考资料",
                retcode=403
            )

        # 删除参考资料
        success = ReferenceService.delete_reference(db, reference_id)

        if not success:
            return get_json_result(
                data=False,
                retmsg="删除参考资料失败",
                retcode=500
            )

        return get_json_result(data={
            "message": "参考资料已成功删除"
        })
    except Exception as e:
        logging.error(f"删除参考资料失败: {str(e)}", exc_info=True)
        return get_json_result(
            data=False,
            retmsg=f"删除参考资料失败: {str(e)}",
            retcode=500
        )

# ========== 内容生成相关路由 ==========#

@router.post("/write-section")
def write_section(
        chapter_id: str,  # 简化为只需要章节ID参数
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """写作特定章节（非流式）"""
    try:
        if not chapter_id:
            return get_json_result(
                retcode=400,
                retmsg="必须提供有效的章节ID",
                data=None
            )

        # 获取章节信息
        chapter = db.query(WritingChapter).filter(
            WritingChapter.id == chapter_id,
            WritingChapter.status == StatusEnum.VALID.value
        ).first()

        if not chapter:
            return get_json_result(
                retcode=404,
                retmsg=f"未找到章节 ID: {chapter_id}",
                data=None
            )

        # 验证项目所有权
        project = db.query(WritingProject).filter(
            WritingProject.id == chapter.project_id,
            WritingProject.status == StatusEnum.VALID.value
        ).first()

        if not project or project.user_id != user.id:
            return get_json_result(
                retcode=403,
                retmsg="无权访问此章节",
                data=None
            )

        # 调用改进的写作服务方法（得到字典结果）
        result = WritingService.write_section_improved(
            db=db,
            chapter_id=chapter_id
        )

        # 将字典结果转换为JSONResponse
        return get_json_result(
            retcode=result["retcode"],
            retmsg=result["retmsg"],
            data=result["data"]
        )
    except Exception as e:
        logging.error(f"章节写作出错: {str(e)}", exc_info=True)
        return get_json_result(
            retcode=500,
            retmsg=f"章节写作失败: {str(e)}",
            data={"type": "error"}
        )


@router.post("/write-section/stream")
def write_section_stream(
        chapter_id: str,  # 简化为只需要章节ID参数
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """流式写作特定章节，实时返回生成内容"""
    if not chapter_id:
        raise HTTPException(status_code=400, detail="必须提供有效的章节ID")

    # 验证章节存在并权限检查
    chapter = db.query(WritingChapter).filter(
        WritingChapter.id == chapter_id,
        WritingChapter.status == StatusEnum.VALID.value
    ).first()

    if not chapter:
        raise HTTPException(status_code=404, detail=f"未找到章节 ID: {chapter_id}")

    # 验证项目所有权
    project = db.query(WritingProject).filter(
        WritingProject.id == chapter.project_id,
        WritingProject.status == StatusEnum.VALID.value
    ).first()

    if not project or project.user_id != user.id:
        raise HTTPException(status_code=403, detail="无权访问此章节")

    return StreamingResponse(
        WritingService.write_section_stream_improved(
            db=db,
            chapter_id=chapter_id
        ),
        media_type="text/event-stream",
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive"
        }
    )


@router.get("/complete-article/{article_id}")
def get_complete_article(
        article_id: str,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """获取完整文章内容"""
    try:
        # 确保有项目访问权限
        project = ProjectService.get_project_details(db, article_id, user.id)

        content, word_count = WritingService.get_completed_article(db, article_id)

        complete = not content.startswith("文章尚未完成")

        return get_json_result(data={
            "complete": complete,
            "article_id": article_id,
            "content": content,
            "word_count": word_count
        })
    except Exception as e:
        logging.error(f"获取完整文章出错: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取完整文章失败: {str(e)}")


@router.get("/article-status/{article_id}")
def get_article_status(
        article_id: str,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """获取文章状态和进度"""
    try:
        # 获取项目大纲和完成情况
        outline = ChapterService.get_project_outline(db, article_id)

        return get_json_result(data=outline)
    except Exception as e:
        logging.error(f"获取文章状态出错: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取文章状态失败: {str(e)}")