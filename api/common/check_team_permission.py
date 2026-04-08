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
from sqlalchemy.orm.session import Session

from api.db import TenantPermission
from api.db.db_models import File, Knowledgebase
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.user_service import TenantService


def check_kb_team_permission(db: Session, kb: dict | Knowledgebase, other: str) -> bool:
    kb = kb.to_dict() if isinstance(kb, Knowledgebase) else kb

    kb_tenant_id = kb["tenant_id"]

    if kb_tenant_id == other:
        return True

    if kb["permission"] != TenantPermission.TEAM:
        return False

    joined_tenants = TenantService.get_joined_tenants_by_user_id(db, other)
    return any(tenant.tenant_id == kb_tenant_id for tenant in joined_tenants)


def check_file_team_permission(db: Session, file: dict | File, other: str) -> bool:
    file = file.to_dict() if isinstance(file, File) else file

    file_tenant_id = file["tenant_id"]
    if file_tenant_id == other:
        return True

    file_id = file["id"]

    kb_ids = [kb_info["kb_id"] for kb_info in FileService.get_kb_id_by_file_id(db, file_id)]

    for kb_id in kb_ids:
        kb = KnowledgebaseService.get_by_id(db, kb_id)
        if not kb:
            continue

        if check_kb_team_permission(db, kb, other):
            return True

    return False