#
#  Copyright 2024 The InfiniFlow Authors. All Rights Reserved.
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
import logging
import uuid

from sqlalchemy.orm import Session

from api import settings
from api.db import FileType, UserTenantRole
from api.db.db_models import TenantLLM
from api.db.services.llm_service import get_init_tenant_llm
from api.db.services.file_service import FileService
from api.db.services.tenant_llm_service import TenantLLMService
from api.db.services.user_service import TenantService, UserService, UserTenantService



def create_new_user(db: Session, user_info: dict) -> dict:
    """
    Add a new user, and create tenant, tenant llm, file folder for new user.
    
    Args:
        db: 数据库会话
        user_info: {
            "email": <example@example.com>,
            "nickname": <str, "name">,
            "password": <decrypted password>,
            "login_channel": <enum, "password">,
            "is_superuser": <bool, role == "admin">,
        }
        
    Returns:
        {
            "success": <bool>,
            "user_info": <dict>, # if true, return user_info
        }
    """
    # generate user_id and access_token for user
    user_id = uuid.uuid1().hex
    user_info['id'] = user_id
    user_info['access_token'] = uuid.uuid1().hex
    
    # construct tenant info
    tenant = {
        "id": user_id,
        "name": user_info["nickname"] + "'s Kingdom",
        "llm_id": settings.CHAT_MDL,
        "embd_id": settings.EMBEDDING_MDL,
        "asr_id": settings.ASR_MDL,
        "parser_ids": settings.PARSERS,
        "img2txt_id": settings.IMAGE2TEXT_MDL,
        "rerank_id": settings.RERANK_MDL,
    }
    
    usr_tenant = {
        "tenant_id": user_id,
        "user_id": user_id,
        "invited_by": user_id,
        "role": UserTenantRole.OWNER,
    }
    
    # construct file folder info（根目录：parent_id 等于自己的 id）
    file_id = uuid.uuid1().hex
    file = {
        "id": file_id,
        "parent_id": file_id,
        "tenant_id": user_id,
        "created_by": user_id,
        "name": "/",
        "type": FileType.FOLDER.value,
        "size": 0,
        "location": "",
    }
    
    try:
        # 获取初始租户 LLM 配置
        logging.info(f"Getting initial tenant LLM config for user_id: {user_id}")
        tenant_llm = get_init_tenant_llm(db, user_id)

        # 创建用户
        logging.info(f"Creating user with info: email={user_info.get('email')}, nickname={user_info.get('nickname')}")
        user = UserService.save(db, **user_info)
        if not user:
            logging.error("UserService.save returned None")
            return {"success": False}

        # 创建租户
        logging.info(f"Creating tenant for user_id: {user_id}")
        TenantService.insert(db, **tenant)
        
        # 创建用户-租户关系
        logging.info(f"Creating user-tenant relationship")
        UserTenantService.insert(db, **usr_tenant)
        
        # 批量插入租户 LLM 配置
        logging.info(f"Inserting tenant LLM configs, count: {len(tenant_llm)}")
        TenantLLMService.insert_many(db, tenant_llm)
        
        # 创建根文件夹
        # 注意：FileService.insert(db, file_dict) 接受字典作为第二个参数
        logging.info(f"Creating root folder for user")
        FileService.insert(db, file)

        logging.info(f"User {user_info.get('email')} created successfully")
        return {
            "success": True,
            "user_info": user_info,
        }

    except Exception as create_error:
        logging.exception(f"Exception occurred while creating user: {create_error}")
        # 发生异常时回滚数据库
        db.rollback()
        
        # 清理已创建的数据
        try:
            TenantService.delete_by_id(db, user_id)
        except Exception as e:
            logging.exception(e)
            
        try:
            u = UserTenantService.query(db, tenant_id=user_id)
            if u:
                UserTenantService.delete_by_id(db, u[0].id)
        except Exception as e:
            logging.exception(e)
            
        try:
            # 删除租户 LLM 配置（SQLAlchemy 风格）
            db.query(TenantLLM).filter(TenantLLM.tenant_id == user_id).delete(synchronize_session=False)
            db.commit()
        except Exception as e:
            logging.exception(e)
            db.rollback()
            
        try:
            # 删除文件夹
            FileService.delete_by_id(db, file["id"])
        except Exception as e:
            logging.exception(e)
            
        # delete user row finally
        try:
            UserService.delete_by_id(db, user_id)
        except Exception as e:
            logging.exception(e)
            
        # reraise
        raise create_error