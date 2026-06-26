from lark import Transformer

GRAMMAR = r"""
start: command

command: sql_command | meta_command

sql_command: login_user
           | list_services
           | show_service
           | startup_service
           | shutdown_service
           | restart_service
           | list_users
           | show_user
           | drop_user
           | alter_user
           | create_user
           | activate_user
           | list_datasets
           | list_agents
           | create_role
           | drop_role
           | alter_role
           | list_roles
           | show_role
           | grant_permission
           | revoke_permission
           | alter_user_role
           | show_user_permission
           | show_version
           | grant_admin
           | revoke_admin
           | set_variable
           | show_variable
           | list_variables
           | list_configs
           | list_environments
           | generate_key
           | list_keys
           | drop_key
           | create_token
           | list_tokens
           | drop_token
           | get_chunk
           | list_chunks
           | list_user_datasets
           | list_user_agents
           | list_user_chats
           | list_user_model_providers
           | list_user_default_models
           | ping_server
           | register_user
           | show_current_user
           | set_default_llm
           | set_default_vlm
           | set_default_embedding
           | set_default_reranker
           | set_default_asr
           | set_default_tts
           | reset_default_llm
           | reset_default_vlm
           | reset_default_embedding
           | reset_default_reranker
           | reset_default_asr
           | reset_default_tts
           | create_model_provider
           | drop_model_provider
           | create_user_dataset_with_parser
           | create_user_dataset_with_pipeline
           | drop_user_dataset
           | list_user_dataset_files
           | list_user_dataset_documents
           | list_user_datasets_metadata
           | list_user_documents_metadata_summary
           | create_user_chat
           | drop_user_chat
           | create_index
           | drop_index
           | create_doc_meta_index
           | drop_doc_meta_index
           | create_chat_session
           | drop_chat_session
           | list_chat_sessions
           | chat_on_session
           | import_docs_into_dataset
           | insert_dataset_from_file
           | insert_metadata_from_file
           | search_on_datasets
           | parse_dataset_docs
           | parse_dataset_sync
           | parse_dataset_async
           | show_fingerprint
           | set_license
           | set_license_config
           | show_license
           | check_license
           | benchmark

// meta command definition
meta_command: "\\" meta_command_name [meta_args]

meta_command_name: /[a-zA-Z?]+/
meta_args: (meta_arg)+

meta_arg: /[^\\s"']+/ | quoted_string

// command definition

LIST: "LIST"i
SERVICES: "SERVICES"i
SHOW: "SHOW"i
CREATE: "CREATE"i
SERVICE: "SERVICE"i
SHUTDOWN: "SHUTDOWN"i
STARTUP: "STARTUP"i
RESTART: "RESTART"i
USERS: "USERS"i
DROP: "DROP"i
USER: "USER"i
ALTER: "ALTER"i
ACTIVE: "ACTIVE"i
ADMIN: "ADMIN"i
PASSWORD: "PASSWORD"i
DATASETS: "DATASETS"i
OF: "OF"i
AGENTS: "AGENTS"i
ROLE: "ROLE"i
ROLES: "ROLES"i
DESCRIPTION: "DESCRIPTION"i
GRANT: "GRANT"i
REVOKE: "REVOKE"i
ALL: "ALL"i
PERMISSION: "PERMISSION"i
TO: "TO"i
FROM: "FROM"i
FOR: "FOR"i
RESOURCES: "RESOURCES"i
ON: "ON"i
SET: "SET"i
VERSION: "VERSION"i
VAR: "VAR"i
VARS: "VARS"i
CONFIGS: "CONFIGS"i
ENVS: "ENVS"i
KEY: "KEY"i
KEYS: "KEYS"i
TOKEN: "TOKEN"i
TOKENS: "TOKENS"i
GENERATE: "GENERATE"i
MODEL: "MODEL"i
MODELS: "MODELS"i
PROVIDERS: "PROVIDERS"i
DEFAULT: "DEFAULT"i
CHATS: "CHATS"i
FILES: "FILES"i
DOCUMENTS: "DOCUMENTS"i
DOCUMENT: "DOCUMENT"i
CHUNK: "CHUNK"i
CHUNKS: "CHUNKS"i
GET: "GET"i
PAGE: "PAGE"i
SIZE: "SIZE"i
KEYWORDS: "KEYWORDS"i
AVAILABLE: "AVAILABLE"i
METADATA: "METADATA"i
SUMMARY: "SUMMARY"i
PING: "PING"i
LOGIN: "LOGIN"i
REGISTER: "REGISTER"i
CURRENT: "CURRENT"i
PROVIDER: "PROVIDER"i
LLM: "LLM"i
VLM: "VLM"i
EMBEDDING: "EMBEDDING"i
RERANKER: "RERANKER"i
ASR: "ASR"i
TTS: "TTS"i
IMPORT: "IMPORT"i
INSERT: "INSERT"i
FILE: "FILE"i
INTO: "INTO"i
WITH: "WITH"i
VECTOR_SIZE: "VECTOR_SIZE"i
PARSER: "PARSER"i
PIPELINE: "PIPELINE"i
PARSE: "PARSE"i
SEARCH: "SEARCH"i
ASYNC: "ASYNC"i
SYNC: "SYNC"i
SESSION: "SESSION"i
SESSIONS: "SESSIONS"i
FINGERPRINT: "FINGERPRINT"i
LICENSE: "LICENSE"i
CHECK: "CHECK"i
CONFIG: "CONFIG"i
INDEX: "INDEX"i
DOC_META: "DOC_META"i
BENCHMARK: "BENCHMARK"i
AS: "AS"i
RESET: "RESET"i
DATASET: "DATASET"i
CHAT: "CHAT"i
COMMA.2: ","

list_services: LIST SERVICES ";"
show_service: SHOW SERVICE NUMBER ";"
startup_service: STARTUP SERVICE NUMBER ";"
shutdown_service: SHUTDOWN SERVICE NUMBER ";"
restart_service: RESTART SERVICE NUMBER ";"

list_users: LIST USERS ";"
drop_user: DROP USER quoted_string ";"
alter_user: ALTER USER PASSWORD quoted_string quoted_string ";"
show_user: SHOW USER quoted_string ";"
create_user: CREATE USER quoted_string quoted_string ";"
activate_user: ALTER USER ACTIVE quoted_string status ";"

list_datasets: LIST DATASETS OF quoted_string ";"
list_agents: LIST AGENTS OF quoted_string ";"

create_role: CREATE ROLE identifier [DESCRIPTION quoted_string] ";"
drop_role: DROP ROLE identifier ";"
alter_role: ALTER ROLE identifier SET DESCRIPTION quoted_string ";"
list_roles: LIST ROLES ";"
show_role: SHOW ROLE identifier ";"

grant_permission: GRANT action_list ON identifier TO ROLE identifier ";"
revoke_permission: REVOKE action_list ON identifier FROM ROLE identifier ";"
alter_user_role: ALTER USER quoted_string SET ROLE identifier ";"
show_user_permission: SHOW USER PERMISSION quoted_string ";"

grant_admin: GRANT ADMIN quoted_string ";"
revoke_admin: REVOKE ADMIN quoted_string ";"

set_variable: SET VAR identifier identifier ";"
show_variable: SHOW VAR identifier ";"
list_variables: LIST VARS ";"
list_configs: LIST CONFIGS ";"
list_environments: LIST ENVS ";"

show_fingerprint: SHOW FINGERPRINT ";"
set_license: SET LICENSE quoted_string ";"
set_license_config: SET LICENSE CONFIG NUMBER NUMBER ";"
show_license: SHOW LICENSE ";"
check_license: CHECK LICENSE ";"

generate_key: GENERATE KEY FOR USER quoted_string ";"
list_keys: LIST KEYS OF quoted_string ";"
drop_key: DROP KEY quoted_string OF quoted_string ";"

create_token: CREATE TOKEN quoted_string ";"
list_tokens: LIST TOKENS ";"
drop_token: DROP TOKEN quoted_string ";"

get_chunk: GET CHUNK quoted_string ";"
list_chunks: LIST CHUNKS OF DOCUMENT quoted_string (PAGE NUMBER)? (SIZE NUMBER)? (KEYWORDS quoted_string)? (AVAILABLE NUMBER)? ";"

show_version: SHOW VERSION ";"

list_user_datasets: LIST DATASETS ";"
list_user_agents: LIST AGENTS ";"
list_user_chats: LIST CHATS ";"
list_user_model_providers: LIST MODEL PROVIDERS ";"
list_user_default_models: LIST DEFAULT MODELS ";"

login_user: LOGIN USER quoted_string ";"
ping_server: PING ";"
show_current_user: SHOW CURRENT USER ";"
register_user: REGISTER USER quoted_string AS quoted_string PASSWORD quoted_string ";"
create_model_provider: CREATE MODEL PROVIDER quoted_string quoted_string ";"
drop_model_provider: DROP MODEL PROVIDER quoted_string ";"
set_default_llm: SET DEFAULT LLM quoted_string ";"
set_default_vlm: SET DEFAULT VLM quoted_string ";"
set_default_embedding: SET DEFAULT EMBEDDING quoted_string ";"
set_default_reranker: SET DEFAULT RERANKER quoted_string ";"
set_default_asr: SET DEFAULT ASR quoted_string ";"
set_default_tts: SET DEFAULT TTS quoted_string ";"
reset_default_llm: RESET DEFAULT LLM ";"
reset_default_vlm: RESET DEFAULT VLM ";"
reset_default_embedding: RESET DEFAULT EMBEDDING ";"
reset_default_reranker: RESET DEFAULT RERANKER ";"
reset_default_asr: RESET DEFAULT ASR ";"
reset_default_tts: RESET DEFAULT TTS ";"
create_user_dataset_with_parser: CREATE DATASET quoted_string WITH EMBEDDING quoted_string PARSER quoted_string ";"
create_user_dataset_with_pipeline: CREATE DATASET quoted_string WITH EMBEDDING quoted_string PIPELINE quoted_string ";"
drop_user_dataset: DROP DATASET quoted_string ";"
list_user_dataset_files: LIST FILES OF DATASET quoted_string ";"
list_user_dataset_documents: LIST DOCUMENTS OF DATASET quoted_string ";"
list_user_datasets_metadata: LIST METADATA OF DATASETS quoted_string (COMMA quoted_string)* ";"
list_user_documents_metadata_summary: LIST METADATA SUMMARY OF DATASET quoted_string (DOCUMENTS quoted_string (COMMA quoted_string)*)? ";"
create_user_chat: CREATE CHAT quoted_string ";"
drop_user_chat: DROP CHAT quoted_string ";"
create_index: CREATE INDEX FOR DATASET quoted_string VECTOR_SIZE NUMBER ";"
drop_index: DROP INDEX FOR DATASET quoted_string ";"
create_doc_meta_index: CREATE INDEX DOC_META ";"
drop_doc_meta_index: DROP INDEX DOC_META ";"
create_chat_session: CREATE CHAT quoted_string SESSION ";"
drop_chat_session: DROP CHAT quoted_string SESSION quoted_string ";"
list_chat_sessions: LIST CHAT quoted_string SESSIONS ";"
chat_on_session: CHAT quoted_string ON quoted_string SESSION quoted_string ";"
import_docs_into_dataset: IMPORT quoted_string INTO DATASET quoted_string ";"
// Internal CLI for GO
insert_dataset_from_file: INSERT DATASET FROM FILE quoted_string ";"
insert_metadata_from_file: INSERT METADATA FROM FILE quoted_string ";"
search_on_datasets: SEARCH quoted_string ON DATASETS quoted_string ";"
parse_dataset_docs: PARSE quoted_string OF DATASET quoted_string ";"
parse_dataset_sync: PARSE DATASET quoted_string SYNC ";"
parse_dataset_async: PARSE DATASET quoted_string ASYNC ";"
benchmark: BENCHMARK NUMBER NUMBER sql_command

action_list: identifier (COMMA identifier)*

identifier: WORD
quoted_string: QUOTED_STRING
status: ON | WORD

QUOTED_STRING: /'[^']+'/ | /"[^"]+"/
WORD: /[a-zA-Z0-9_\-\.]+/
NUMBER: /[0-9]+/

%import common.WS
%ignore WS
"""


class MultiRAGCLITransformer(Transformer):

    def start(self, items):
        return items[0]

    def command(self, items):
        return items[0]

    def list_services(self, items):
        result = {"type": "list_services"}
        return result

    def show_service(self, items):
        service_id = int(items[2])
        return {"type": "show_service", "number": service_id}

    def startup_service(self, items):
        service_id = int(items[2])
        return {"type": "startup_service", "number": service_id}

    def shutdown_service(self, items):
        service_id = int(items[2])
        return {"type": "shutdown_service", "number": service_id}

    def restart_service(self, items):
        service_id = int(items[2])
        return {"type": "restart_service", "number": service_id}

    def list_users(self, items):
        return {"type": "list_users"}

    def show_user(self, items):
        user_name = items[2]
        return {"type": "show_user", "user_name": user_name}

    def drop_user(self, items):
        user_name = items[2]
        return {"type": "drop_user", "user_name": user_name}

    def alter_user(self, items):
        user_name = items[3]
        new_password = items[4]
        return {"type": "alter_user", "user_name": user_name, "password": new_password}

    def create_user(self, items):
        user_name = items[2]
        password = items[3]
        return {"type": "create_user", "user_name": user_name, "password": password, "role": "user"}

    def activate_user(self, items):
        user_name = items[3]
        activate_status = items[4]
        return {"type": "activate_user", "activate_status": activate_status, "user_name": user_name}

    def list_datasets(self, items):
        user_name = items[3]
        return {"type": "list_datasets", "user_name": user_name}

    def list_agents(self, items):
        user_name = items[3]
        return {"type": "list_agents", "user_name": user_name}

    def create_role(self, items):
        role_name = items[2]
        if len(items) > 4:
            description = items[4]
            return {"type": "create_role", "role_name": role_name, "description": description}
        else:
            return {"type": "create_role", "role_name": role_name}

    def drop_role(self, items):
        role_name = items[2]
        return {"type": "drop_role", "role_name": role_name}

    def alter_role(self, items):
        role_name = items[2]
        description = items[5]
        return {"type": "alter_role", "role_name": role_name, "description": description}

    def list_roles(self, items):
        return {"type": "list_roles"}

    def show_role(self, items):
        role_name = items[2]
        return {"type": "show_role", "role_name": role_name}

    def grant_permission(self, items):
        action_list = items[1]
        resource = items[3]
        role_name = items[6]
        return {"type": "grant_permission", "role_name": role_name, "resource": resource, "actions": action_list}

    def revoke_permission(self, items):
        action_list = items[1]
        resource = items[3]
        role_name = items[6]
        return {
            "type": "revoke_permission",
            "role_name": role_name,
            "resource": resource, "actions": action_list
        }

    def alter_user_role(self, items):
        user_name = items[2]
        role_name = items[5]
        return {"type": "alter_user_role", "user_name": user_name, "role_name": role_name}

    def show_user_permission(self, items):
        user_name = items[3]
        return {"type": "show_user_permission", "user_name": user_name}

    def show_version(self, items):
        return {"type": "show_version"}

    def grant_admin(self, items):
        user_name = items[2]
        return {"type": "grant_admin", "user_name": user_name}

    def revoke_admin(self, items):
        user_name = items[2]
        return {"type": "revoke_admin", "user_name": user_name}

    def set_variable(self, items):
        var_name = items[2]
        var_value = items[3]
        return {"type": "set_variable", "var_name": var_name, "var_value": var_value}

    def show_variable(self, items):
        var_name = items[2]
        return {"type": "show_variable", "var_name": var_name}

    def list_variables(self, items):
        return {"type": "list_variables"}

    def list_configs(self, items):
        return {"type": "list_configs"}

    def list_environments(self, items):
        return {"type": "list_environments"}

    def show_fingerprint(self, items):
        return {"type": "show_fingerprint"}

    def set_license(self, items):
        license = items[2].children[0].strip("'\"")
        return {"type": "set_license", "license": license}

    def set_license_config(self, items):
        value1: int = int(items[3])
        value2: int = int(items[4])
        return {"type": "set_license_config", "value1": value1, "value2": value2}

    def show_license(self, items):
        return {"type": "show_license"}

    def check_license(self, items):
        return {"type": "check_license"}

    def generate_key(self, items):
        user_name = items[4]
        return {"type": "generate_key", "user_name": user_name}

    def list_keys(self, items):
        user_name = items[3]
        return {"type": "list_keys", "user_name": user_name}

    def drop_key(self, items):
        key = items[2]
        user_name = items[4]
        return {"type": "drop_key", "key": key, "user_name": user_name}

    def create_token(self, items):
        name = items[2]
        return {"type": "create_token", "name": name}

    def list_tokens(self, items):
        return {"type": "list_tokens"}

    def drop_token(self, items):
        token = items[2]
        return {"type": "drop_token", "token": token}

    def get_chunk(self, items):
        chunk_id = items[2]
        return {"type": "get_chunk", "chunk_id": chunk_id}

    def list_chunks(self, items):
        doc_id = items[4]
        result = {"type": "list_chunks", "doc_id": doc_id}
        # Optional params: PAGE NUMBER / SIZE NUMBER / KEYWORDS quoted_string / AVAILABLE NUMBER
        for i, item in enumerate(items):
            tok = str(item).upper()
            if tok == "PAGE":
                result["page"] = int(items[i + 1])
            elif tok == "SIZE":
                result["size"] = int(items[i + 1])
            elif tok == "KEYWORDS":
                result["keywords"] = items[i + 1]
            elif tok == "AVAILABLE":
                result["available_int"] = int(items[i + 1])
        return result

    def list_user_datasets(self, items):
        return {"type": "list_user_datasets"}

    def list_user_agents(self, items):
        return {"type": "list_user_agents"}

    def list_user_chats(self, items):
        return {"type": "list_user_chats"}

    def list_user_model_providers(self, items):
        return {"type": "list_user_model_providers"}

    def list_user_default_models(self, items):
        return {"type": "list_user_default_models"}

    def login_user(self, items):
        email = items[2].children[0].strip("'\"")
        return {"type": "login_user", "email": email}

    def ping_server(self, items):
        return {"type": "ping_server"}

    def register_user(self, items):
        user_name = items[2].children[0].strip("'\"")
        nickname = items[4].children[0].strip("'\"")
        password = items[6].children[0].strip("'\"")
        return {"type": "register_user", "user_name": user_name, "nickname": nickname, "password": password}

    def show_current_user(self, items):
        return {"type": "show_current_user"}

    def create_model_provider(self, items):
        provider_name = items[3].children[0].strip("'\"")
        provider_key = items[4].children[0].strip("'\"")
        return {"type": "create_model_provider", "provider_name": provider_name, "provider_key": provider_key}

    def drop_model_provider(self, items):
        provider_name = items[3].children[0].strip("'\"")
        return {"type": "drop_model_provider", "provider_name": provider_name}

    def set_default_llm(self, items):
        model_id = items[3].children[0].strip("'\"")
        return {"type": "set_default_model", "model_type": "llm_id", "model_id": model_id}

    def set_default_vlm(self, items):
        model_id = items[3].children[0].strip("'\"")
        return {"type": "set_default_model", "model_type": "img2txt_id", "model_id": model_id}

    def set_default_embedding(self, items):
        model_id = items[3].children[0].strip("'\"")
        return {"type": "set_default_model", "model_type": "embd_id", "model_id": model_id}

    def set_default_reranker(self, items):
        model_id = items[3].children[0].strip("'\"")
        return {"type": "set_default_model", "model_type": "rerank_id", "model_id": model_id}

    def set_default_asr(self, items):
        model_id = items[3].children[0].strip("'\"")
        return {"type": "set_default_model", "model_type": "asr_id", "model_id": model_id}

    def set_default_tts(self, items):
        model_id = items[3].children[0].strip("'\"")
        return {"type": "set_default_model", "model_type": "tts_id", "model_id": model_id}

    def reset_default_llm(self, items):
        return {"type": "reset_default_model", "model_type": "llm_id"}

    def reset_default_vlm(self, items):
        return {"type": "reset_default_model", "model_type": "img2txt_id"}

    def reset_default_embedding(self, items):
        return {"type": "reset_default_model", "model_type": "embd_id"}

    def reset_default_reranker(self, items):
        return {"type": "reset_default_model", "model_type": "rerank_id"}

    def reset_default_asr(self, items):
        return {"type": "reset_default_model", "model_type": "asr_id"}

    def reset_default_tts(self, items):
        return {"type": "reset_default_model", "model_type": "tts_id"}

    def create_user_dataset_with_parser(self, items):
        dataset_name = items[2].children[0].strip("'\"")
        embedding = items[5].children[0].strip("'\"")
        parser_type = items[7].children[0].strip("'\"")
        return {"type": "create_user_dataset", "dataset_name": dataset_name, "embedding": embedding, "parser_type": parser_type}

    def create_user_dataset_with_pipeline(self, items):
        dataset_name = items[2].children[0].strip("'\"")
        embedding = items[5].children[0].strip("'\"")
        pipeline = items[7].children[0].strip("'\"")
        return {"type": "create_user_dataset", "dataset_name": dataset_name, "embedding": embedding, "pipeline": pipeline}

    def drop_user_dataset(self, items):
        dataset_name = items[2].children[0].strip("'\"")
        return {"type": "drop_user_dataset", "dataset_name": dataset_name}

    def list_user_dataset_files(self, items):
        dataset_name = items[4].children[0].strip("'\"")
        return {"type": "list_user_dataset_files", "dataset_name": dataset_name}

    def list_user_dataset_documents(self, items):
        dataset_name = items[4].children[0].strip("'\"")
        return {"type": "list_user_dataset_documents", "dataset_name": dataset_name}

    def list_user_datasets_metadata(self, items):
        dataset_names = [items[4].children[0].strip("'\"")]
        for item in items[5:]:
            if item and hasattr(item, "children") and item.children:
                dataset_names.append(item.children[0].strip("'\""))
        return {"type": "list_user_datasets_metadata", "dataset_names": dataset_names}

    def list_user_documents_metadata_summary(self, items):
        dataset_name = items[5].children[0].strip("'\"")
        doc_ids = []
        for item in items[6:]:
            if item and hasattr(item, "children") and item.children:
                doc_ids.append(item.children[0].strip("'\""))
        return {
            "type": "list_user_documents_metadata_summary",
            "dataset_name": dataset_name,
            "document_ids": doc_ids,
        }

    def create_user_chat(self, items):
        chat_name = items[2].children[0].strip("'\"")
        return {"type": "create_user_chat", "chat_name": chat_name}

    def drop_user_chat(self, items):
        chat_name = items[2].children[0].strip("'\"")
        return {"type": "drop_user_chat", "chat_name": chat_name}

    def create_index(self, items):
        # items: CREATE, INDEX, FOR, DATASET, quoted_string, VECTOR_SIZE, NUMBER, ";"
        dataset_name = None
        vector_size = None
        for i, item in enumerate(items):
            if hasattr(item, 'data') and item.data == 'quoted_string':
                dataset_name = item.children[0].strip("'\"")
            if hasattr(item, 'type') and item.type == 'NUMBER':
                if i > 0 and items[i - 1].type == 'VECTOR_SIZE':
                    vector_size = int(item)
        return {"type": "create_index", "dataset_name": dataset_name, "vector_size": vector_size}

    def drop_index(self, items):
        dataset_name = None
        for item in items:
            if hasattr(item, 'data') and item.data == 'quoted_string':
                dataset_name = item.children[0].strip("'\"")
        return {"type": "drop_index", "dataset_name": dataset_name}

    def create_doc_meta_index(self, items):
        return {"type": "create_doc_meta_index"}

    def drop_doc_meta_index(self, items):
        return {"type": "drop_doc_meta_index"}

    def create_chat_session(self, items):
        chat_name = items[2].children[0].strip("'\"")
        return {"type": "create_chat_session", "chat_name": chat_name}

    def drop_chat_session(self, items):
        chat_name = items[2].children[0].strip("'\"")
        session_id = items[4].children[0].strip("'\"")
        return {"type": "drop_chat_session", "chat_name": chat_name, "session_id": session_id}

    def list_chat_sessions(self, items):
        chat_name = items[2].children[0].strip("'\"")
        return {"type": "list_chat_sessions", "chat_name": chat_name}

    def chat_on_session(self, items):
        message = items[1].children[0].strip("'\"")
        chat_name = items[3].children[0].strip("'\"")
        session_id = items[5].children[0].strip("'\"")
        return {"type": "chat_on_session", "message": message, "chat_name": chat_name, "session_id": session_id}

    def import_docs_into_dataset(self, items):
        document_list_str = items[1].children[0].strip("'\"")
        document_paths = [p.strip() for p in document_list_str.split(",")]
        dataset_name = items[4].children[0].strip("'\"")
        return {"type": "import_docs_into_dataset", "dataset_name": dataset_name, "document_paths": document_paths}

    def insert_dataset_from_file(self, items):
        file_path = items[4].children[0].strip("'\"")
        return {"type": "insert_dataset_from_file", "file_path": file_path}

    def insert_metadata_from_file(self, items):
        file_path = items[4].children[0].strip("'\"")
        return {"type": "insert_metadata_from_file", "file_path": file_path}

    def search_on_datasets(self, items):
        question = items[1].children[0].strip("'\"")
        datasets_str = items[4].children[0].strip("'\"")
        datasets = [d.strip() for d in datasets_str.split(",")]
        return {"type": "search_on_datasets", "datasets": datasets, "question": question}

    def parse_dataset_docs(self, items):
        document_list_str = items[1].children[0].strip("'\"")
        document_names = [d.strip() for d in document_list_str.split(",")]
        dataset_name = items[4].children[0].strip("'\"")
        return {"type": "parse_dataset_docs", "dataset_name": dataset_name, "document_names": document_names}

    def parse_dataset_sync(self, items):
        dataset_name = items[2].children[0].strip("'\"")
        return {"type": "parse_dataset", "dataset_name": dataset_name, "method": "sync"}

    def parse_dataset_async(self, items):
        dataset_name = items[2].children[0].strip("'\"")
        return {"type": "parse_dataset", "dataset_name": dataset_name, "method": "async"}

    def benchmark(self, items):
        concurrency = int(str(items[1]))
        iterations = int(str(items[2]))
        command = items[3] if isinstance(items[3], dict) else items[3].children[0]
        return {"type": "benchmark", "concurrency": concurrency, "iterations": iterations, "command": command}

    def action_list(self, items):
        return [item for item in items if str(item) != ","]

    def meta_command(self, items):
        command_name = str(items[0]).lower()
        args = items[1:] if len(items) > 1 else []

        # handle quoted parameter
        parsed_args = []
        for arg in args:
            if hasattr(arg, "value"):
                parsed_args.append(arg.value)
            else:
                parsed_args.append(str(arg))

        return {"type": "meta", "command": command_name, "args": parsed_args}

    def meta_command_name(self, items):
        return items[0]

    def meta_args(self, items):
        return items
