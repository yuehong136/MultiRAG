#
#  Copyright 2025 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
import json
import logging
import os
from datetime import datetime, timedelta

from sqlalchemy import asc, func
from sqlalchemy import desc as sa_desc
from sqlalchemy.orm import Session

from api.db.db_models import Document, PipelineOperationLog
from api.db.services.canvas_service import UserCanvasService
from api.db.services.common_service import CommonService
from api.db.services.document_service import DocumentService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.task_service import GRAPH_RAPTOR_FAKE_DOC_ID
from common.constants import VALID_PIPELINE_TASK_TYPES, PipelineTaskType
from common.misc_utils import get_uuid
from common.time_utils import current_timestamp, datetime_format


class PipelineOperationLogService(CommonService):
    model = PipelineOperationLog

    @classmethod
    def get_file_logs_fields(cls):
        return [
            cls.model.id,
            cls.model.document_id,
            cls.model.tenant_id,
            cls.model.kb_id,
            cls.model.pipeline_id,
            cls.model.pipeline_title,
            cls.model.parser_id,
            cls.model.document_name,
            cls.model.document_suffix,
            cls.model.document_type,
            cls.model.source_from,
            cls.model.progress,
            cls.model.progress_msg,
            cls.model.process_begin_at,
            cls.model.process_duration,
            cls.model.dsl,
            cls.model.task_type,
            cls.model.operation_status,
            cls.model.avatar,
            cls.model.status,
            cls.model.create_time,
            cls.model.create_date,
            cls.model.update_time,
            cls.model.update_date,
        ]

    @classmethod
    def get_dataset_logs_fields(cls):
        return [
            cls.model.id,
            cls.model.tenant_id,
            cls.model.kb_id,
            cls.model.progress,
            cls.model.progress_msg,
            cls.model.process_begin_at,
            cls.model.process_duration,
            cls.model.task_type,
            cls.model.operation_status,
            cls.model.avatar,
            cls.model.status,
            cls.model.create_time,
            cls.model.create_date,
            cls.model.update_time,
            cls.model.update_date,
        ]

    @classmethod
    def save(cls, db: Session, **kwargs):
        """
        Wrap this function in a transaction
        """
        sample_obj = cls.model(**kwargs)
        db.add(sample_obj)
        db.flush()  # Get the ID without committing
        return sample_obj

    @classmethod
    def create(cls, db: Session, document_id, pipeline_id, task_type, fake_document_ids=[], dsl: str = "{}"):
        referred_document_id = document_id

        if referred_document_id == GRAPH_RAPTOR_FAKE_DOC_ID and fake_document_ids:
            referred_document_id = fake_document_ids[0]

        document = DocumentService.get_by_id(db, referred_document_id)
        if not document:
            logging.warning(f"Document for referred_document_id {referred_document_id} not found")
            return None
        DocumentService.update_progress_immediately(db, [document.to_dict()])
        document = DocumentService.get_by_id(db, referred_document_id)
        if not document:
            logging.warning(f"Document for referred_document_id {referred_document_id} not found")
            return None
        if document.progress not in [1, -1]:
            return None
        operation_status = document.run

        if pipeline_id:
            user_pipeline = UserCanvasService.get_by_id(db, pipeline_id)
            if not user_pipeline:
                raise RuntimeError(f"Pipeline {pipeline_id} not found")
            tenant_id = user_pipeline.user_id
            title = user_pipeline.title
            avatar = user_pipeline.avatar
        else:
            kb_info = KnowledgebaseService.get_by_id(db, document.kb_id)
            if not kb_info:
                raise RuntimeError(f"Cannot find dataset {document.kb_id} for referred_document {referred_document_id}")

            tenant_id = kb_info.tenant_id
            title = document.parser_id
            avatar = document.thumbnail

        if task_type not in VALID_PIPELINE_TASK_TYPES:
            raise ValueError(f"Invalid task type: {task_type}")

        if task_type in [PipelineTaskType.GRAPH_RAG, PipelineTaskType.RAPTOR, PipelineTaskType.MINDMAP]:
            finish_at = document.process_begin_at + timedelta(seconds=document.process_duration)
            if task_type == PipelineTaskType.GRAPH_RAG:
                KnowledgebaseService.update_by_id(
                    db,
                    document.kb_id,
                    {"graphrag_task_finish_at": finish_at},
                )
            elif task_type == PipelineTaskType.RAPTOR:
                KnowledgebaseService.update_by_id(
                    db,
                    document.kb_id,
                    {"raptor_task_finish_at": finish_at},
                )
            elif task_type == PipelineTaskType.MINDMAP:
                KnowledgebaseService.update_by_id(
                    db,
                    document.kb_id,
                    {"mindmap_task_finish_at": finish_at},
                )

        log = {
            "id": get_uuid(),
            "document_id": document_id,  # GRAPH_RAPTOR_FAKE_DOC_ID or real document_id
            "tenant_id": tenant_id,
            "kb_id": document.kb_id,
            "pipeline_id": pipeline_id,
            "pipeline_title": title,
            "parser_id": document.parser_id,
            "document_name": document.name,
            "document_suffix": document.suffix,
            "document_type": document.type,
            "source_from": document.source_type.split("/")[0] if document.source_type else "",
            "progress": document.progress,
            "progress_msg": document.progress_msg,
            "process_begin_at": document.process_begin_at,
            "process_duration": document.process_duration,
            "dsl": json.loads(dsl),
            "task_type": task_type,
            "operation_status": operation_status,
            "avatar": avatar,
        }
        timestamp = current_timestamp()
        datetime_now = datetime_format(datetime.now())
        log["create_time"] = timestamp
        log["create_date"] = datetime_now
        log["update_time"] = timestamp
        log["update_date"] = datetime_now

        # SQLAlchemy transaction
        try:
            obj = cls.save(db, **log)

            limit = int(os.getenv("PIPELINE_OPERATION_LOG_LIMIT", 1000))
            total = db.query(func.count(cls.model.id)).filter(cls.model.kb_id == document.kb_id).scalar()

            if total > limit:
                # Get IDs to keep
                keep_ids_query = db.query(cls.model.id).filter(cls.model.kb_id == document.kb_id).order_by(sa_desc(cls.model.create_time)).limit(limit)
                keep_ids = [row.id for row in keep_ids_query.all()]

                # Delete old logs
                deleted = db.query(cls.model).filter(cls.model.kb_id == document.kb_id, cls.model.id.notin_(keep_ids)).delete(synchronize_session=False)
                logging.info(f"[PipelineOperationLogService] Cleaned {deleted} old logs, kept latest {limit} for {document.kb_id}")

            db.commit()
        except Exception as e:
            db.rollback()
            logging.exception(f"Failed to create pipeline operation log: {e}")
            raise

        return obj

    @classmethod
    def record_pipeline_operation(cls, db: Session, document_id, pipeline_id, task_type, fake_document_ids=[]):
        return cls.create(db=db, document_id=document_id, pipeline_id=pipeline_id, task_type=task_type, fake_document_ids=fake_document_ids)

    @classmethod
    def get_file_logs_by_kb_id(cls, db: Session, kb_id, page_number, items_per_page, orderby, is_desc, keywords, operation_status, types, suffix, create_date_from=None, create_date_to=None):
        fields = cls.get_file_logs_fields()

        # Build query
        query = db.query(*fields)

        # Apply filters
        if keywords:
            query = query.filter(cls.model.kb_id == kb_id, func.lower(cls.model.document_name).contains(keywords.lower()))
        else:
            query = query.filter(cls.model.kb_id == kb_id)

        query = query.filter(cls.model.document_id != GRAPH_RAPTOR_FAKE_DOC_ID)

        if operation_status:
            query = query.filter(cls.model.operation_status.in_(operation_status))
        if types:
            query = query.filter(cls.model.document_type.in_(types))
        if suffix:
            query = query.filter(cls.model.document_suffix.in_(suffix))
        if create_date_from:
            query = query.filter(cls.model.create_date >= create_date_from)
        if create_date_to:
            query = query.filter(cls.model.create_date <= create_date_to)

        # Get count before pagination
        count = query.count()

        # Apply ordering
        order_column = getattr(cls.model, orderby)
        if is_desc:
            query = query.order_by(sa_desc(order_column))
        else:
            query = query.order_by(asc(order_column))

        # Apply pagination
        if page_number and items_per_page:
            offset = (page_number - 1) * items_per_page
            query = query.offset(offset).limit(items_per_page)

        # Execute and return results
        results = query.all()
        return [dict(zip([c.name for c in cls.model.__table__.columns], row)) for row in results], count

    @classmethod
    def get_documents_info(cls, db: Session, id):
        fields = [Document.id, Document.name, Document.progress, Document.kb_id]
        query = db.query(*fields).select_from(cls.model).join(Document, cls.model.document_id == Document.id).filter(cls.model.id == id)
        results = query.all()
        return [dict(zip(["id", "name", "progress", "kb_id"], row)) for row in results]

    @classmethod
    def get_dataset_logs_by_kb_id(cls, db: Session, kb_id, page_number, items_per_page, orderby, is_desc, operation_status, create_date_from=None, create_date_to=None):
        fields = cls.get_dataset_logs_fields()

        # Build query
        query = db.query(*fields).filter(cls.model.kb_id == kb_id, cls.model.document_id == GRAPH_RAPTOR_FAKE_DOC_ID)

        # Apply filters
        if operation_status:
            query = query.filter(cls.model.operation_status.in_(operation_status))
        if create_date_from:
            query = query.filter(cls.model.create_date >= create_date_from)
        if create_date_to:
            query = query.filter(cls.model.create_date <= create_date_to)

        # Get count before pagination
        count = query.count()

        # Apply ordering
        order_column = getattr(cls.model, orderby)
        if is_desc:
            query = query.order_by(sa_desc(order_column))
        else:
            query = query.order_by(asc(order_column))

        # Apply pagination
        if page_number and items_per_page:
            offset = (page_number - 1) * items_per_page
            query = query.offset(offset).limit(items_per_page)

        # Execute and return results
        results = query.all()
        # Map results to dictionaries
        field_names = [field.name for field in fields]
        return [dict(zip(field_names, row)) for row in results], count
