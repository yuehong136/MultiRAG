# coding=utf-8
"""
@project: multirag
@Author：龙
@file： canvas_service.py
@date：2024/8/7 10:30
@desc:
"""

from datetime import datetime
import sqlalchemy
from api.db.db_models import CanvasTemplate, UserCanvas
from api.db.services.common_service import CommonService


class CanvasTemplateService(CommonService):
    model = CanvasTemplate

class UserCanvasService(CommonService):
    model = UserCanvas