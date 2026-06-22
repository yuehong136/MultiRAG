import json
import time
import urllib.parse
from pathlib import Path
from typing import Any

from http_client import HttpClient
from lark import Tree

try:
    from requests_toolbelt import MultipartEncoder
except Exception:
    MultipartEncoder = None


def show_help():
    """Help info"""
    help_text = """
Commands:
LIST SERVICES
SHOW SERVICE <service>
STARTUP SERVICE <service>
SHUTDOWN SERVICE <service>
RESTART SERVICE <service>
LIST USERS
SHOW USER <user>
DROP USER <user>
CREATE USER <user> <password>
ALTER USER PASSWORD <user> <new_password>
ALTER USER ACTIVE <user> <on/off>
LIST DATASETS OF <user>
LIST AGENTS OF <user>
CREATE ROLE <role>
DROP ROLE <role>
ALTER ROLE <role> SET DESCRIPTION <description>
LIST ROLES
SHOW ROLE <role>
GRANT <action_list> ON <function> TO ROLE <role>
REVOKE <action_list> ON <function> TO ROLE <role>
ALTER USER <user> SET ROLE <role>
SHOW USER PERMISSION <user>
SHOW VERSION
GRANT ADMIN <user>
REVOKE ADMIN <user>
GENERATE KEY FOR USER <user>
LIST KEYS OF <user>
DROP KEY <key> OF <user>
LIST DATASETS
LIST AGENTS
LIST CHATS
LIST MODEL PROVIDERS
LIST DEFAULT MODELS
PING
SHOW CURRENT USER
REGISTER USER <email> AS <nickname> PASSWORD <password>
CREATE MODEL PROVIDER <provider_name> <api_key>
DROP MODEL PROVIDER <provider_name>
SET DEFAULT LLM <model_id>
SET DEFAULT VLM <model_id>
SET DEFAULT EMBEDDING <model_id>
SET DEFAULT RERANKER <model_id>
SET DEFAULT ASR <model_id>
SET DEFAULT TTS <model_id>
RESET DEFAULT LLM
RESET DEFAULT VLM
RESET DEFAULT EMBEDDING
RESET DEFAULT RERANKER
RESET DEFAULT ASR
RESET DEFAULT TTS
CREATE DATASET <name> WITH EMBEDDING <embd_id> PARSER <parser_type>
CREATE DATASET <name> WITH EMBEDDING <embd_id> PIPELINE <pipeline_id>
DROP DATASET <name>
LIST FILES OF DATASET <name>
LIST DOCUMENTS OF DATASET <name>
LIST METADATA OF DATASETS <dataset_name>[, <dataset_name>]*
LIST METADATA SUMMARY OF DATASET <dataset_name> [DOCUMENTS <doc_id>[, <doc_id>]*]
CREATE CHAT <name>
DROP CHAT <name>
CREATE CHAT <name> SESSION
DROP CHAT <name> SESSION <session_id>
LIST CHAT <name> SESSIONS
CHAT <message> ON <chat_name> SESSION <session_id>
IMPORT <doc_paths> INTO DATASET <dataset_name>
SEARCH <question> ON DATASETS <dataset_names>
PARSE <doc_names> OF DATASET <dataset_name>
PARSE DATASET <name> SYNC
PARSE DATASET <name> ASYNC
BENCHMARK <concurrency> <iterations> <command>
SET VAR <name> <value>
SHOW VAR <name>
LIST VARS
LIST CONFIGS
LIST ENVS

Meta Commands:
\\?, \\h, \\help     Show this help
\\q, \\quit, \\exit   Quit the CLI
    """
    print(help_text)


class MultiRAGClient:
    def __init__(self, http_client: HttpClient, server_type: str):
        self.http_client = http_client
        self.server_type = server_type

    def _format_service_detail_table(self, data):
        # If data is a list, return directly
        if isinstance(data, list):
            return data
        # If data is not a dict, return directly
        if not isinstance(data, dict):
            return data

        # Check if all values are lists (task_executor heartbeats format)
        if not all([isinstance(v, list) for v in data.values()]):
            # Not heartbeats format, return original data
            return data

        # Handle task_executor heartbeats map
        # heartbeats format: {'name': [{'done': 2, 'now': timestamp1}, ...]}
        task_executor_list = []
        for k, v in data.items():
            heartbeats = v
            if heartbeats and isinstance(heartbeats[0], dict) and "now" in heartbeats[0]:
                # display latest status
                heartbeats = sorted(heartbeats, key=lambda x: x["now"], reverse=True)
            task_executor_list.append({
                "task_executor_name": k,
                **heartbeats[0],
            } if heartbeats else {"task_executor_name": k})
        return task_executor_list

    def _print_table_simple(self, data):
        if not data:
            print("No data to print")
            return
        if isinstance(data, dict):
            # handle single row data
            data = [data]

        columns = list(set().union(*(d.keys() for d in data)))
        columns.sort()
        col_widths = {}

        def get_string_width(text):
            half_width_chars = (
                " !\"#$%&'()*+,-./0123456789:;<=>?@"
                "ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`"
                "abcdefghijklmnopqrstuvwxyz{|}~"
                "\t\n\r"
            )
            width = 0
            for char in text:
                if char in half_width_chars:
                    width += 1
                else:
                    width += 2
            return width

        for col in columns:
            max_width = get_string_width(str(col))
            for item in data:
                value_len = get_string_width(str(item.get(col, "")))
                if value_len > max_width:
                    max_width = value_len
            col_widths[col] = max(2, max_width)

        # Generate delimiter
        separator = "+" + "+".join(["-" * (col_widths[col] + 2) for col in columns]) + "+"

        # Print header
        print(separator)
        header = "|" + "|".join([f" {col:<{col_widths[col]}} " for col in columns]) + "|"
        print(header)
        print(separator)

        # Print data
        for item in data:
            row = "|"
            for col in columns:
                value = str(item.get(col, ""))
                if get_string_width(value) > col_widths[col]:
                    value = value[:col_widths[col] - 3] + "..."
                row += f" {value:<{col_widths[col] - (get_string_width(value) - len(value))}} |"
            print(row)

        print(separator)

    def list_services(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        print("Listing all services")
        response = self.http_client.request("GET", "admin/services", use_api_base=True, auth_kind="admin")
        res_json = response.json()
        if response.status_code == 200:
            if res_json.get("code") == 0:
                data = res_json.get("data")
                if data is not None:
                    self._print_table_simple(data)
                else:
                    print("No service data available")
            else:
                print(f"Failed to get services: {res_json.get('message')}")
        else:
            print(f"Fail to get all services, code: {res_json.get('code')}, message: {res_json.get('message')}")

    def show_service(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        service_id: int = command["number"]
        print(f"Showing service details for ID: {service_id}")
        response = self.http_client.request("GET", f"admin/services/{service_id}", use_api_base=True, auth_kind="admin")
        res_json = response.json()
        if response.status_code == 200:
            res_data = res_json.get("data")
            if not res_data:
                print("Failed to get service details: no data returned")
                return

            service_name = res_data.get("service_name") or res_data.get("name", "Unknown")
            status = res_data.get("status", "unknown")

            if status == "alive":
                print(f"Service {service_name} is alive")
                # Print extra info
                if "extra" in res_data:
                    extra_info = res_data["extra"]
                    if "message" in extra_info:
                        if isinstance(extra_info["message"], str):
                            print(extra_info["message"])
                        else:
                            data = self._format_service_detail_table(extra_info["message"])
                            self._print_table_simple(data)
            else:
                print(f"Service {service_name} is {status}")
                if "extra" in res_data and "message" in res_data["extra"]:
                    print(f"Message: {res_data['extra']['message']}")
        else:
            print(f"Fail to show service, code: {res_json.get('code')}, message: {res_json.get('message')}")

    def restart_service(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        service_id: int = command["number"]
        print(f"Restarting service {service_id}...")
        response = self.http_client.request("PUT", f"admin/services/{service_id}", use_api_base=True, auth_kind="admin")
        if response.status_code == 200:
            res_json = response.json()
            if res_json.get("code") == 0:
                print(f"Service {service_id} restarted successfully")
            else:
                print(f"Failed to restart service: {res_json.get('message')}")
        else:
            print(f"Request failed with status code: {response.status_code}")

    def shutdown_service(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        service_id: int = command["number"]
        print(f"Shutting down service {service_id}...")
        response = self.http_client.request("DELETE", f"admin/services/{service_id}", use_api_base=True, auth_kind="admin")
        if response.status_code == 200:
            res_json = response.json()
            if res_json.get("code") == 0:
                print(f"Service {service_id} shut down successfully")
            else:
                print(f"Failed to shut down service: {res_json.get('message')}")
        else:
            print(f"Request failed with status code: {response.status_code}")

    def startup_service(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        service_id: int = command["number"]
        print(f"Starting up service {service_id}...")
        print("Note: STARTUP SERVICE is not yet implemented on the server side")

    def list_users(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        print("Listing all users")
        response = self.http_client.request("GET", "admin/users", use_api_base=True, auth_kind="admin")
        if response.status_code == 200:
            res_json = response.json()
            if res_json.get("code") == 0:
                data = res_json.get("data")
                if data is not None:
                    self._print_table_simple(data)
                else:
                    print("No user data available")
            else:
                print(f"Failed to get users: {res_json.get('message')}")
        else:
            print(f"Request failed with status code: {response.status_code}")

    def show_user(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        username_tree: Tree = command["user_name"]
        user_name: str = username_tree.children[0].strip("'\"")
        print(f"Showing user details for: {user_name}")
        response = self.http_client.request("GET", f"admin/users/{user_name}", use_api_base=True, auth_kind="admin")
        res_json = response.json()
        if response.status_code == 200:
            if res_json.get("code") == 0:
                data = res_json.get("data")
                if data is not None:
                    data.pop("avatar", None)
                    self._print_table_simple(data)
                else:
                    print(f"No data available for user {user_name}")
            else:
                print(f"Fail to get user {user_name}, code: {res_json.get('code', -1)}, message: {res_json.get('message', 'Unknown error')}")
        else:
            print(f"HTTP error, status: {response.status_code}")

    def drop_user(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        username_tree: Tree = command["user_name"]
        user_name: str = username_tree.children[0].strip("'\"")
        print(f"Dropping user: {user_name}")
        response = self.http_client.request("DELETE", f"admin/users/{user_name}", use_api_base=True, auth_kind="admin")
        res_json = response.json()
        if response.status_code == 200:
            if res_json.get("code") == 0:
                print(res_json.get("message", "User dropped successfully"))
            else:
                print(f"Fail to drop user, code: {res_json.get('code', -1)}, message: {res_json.get('message', 'Unknown error')}")
        else:
            print(f"HTTP error, status: {response.status_code}")

    def alter_user(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        user_name_tree: Tree = command["user_name"]
        user_name: str = user_name_tree.children[0].strip("'\"")
        password_tree: Tree = command["password"]
        password: str = password_tree.children[0].strip("'\"")
        print(f"Alter user: {user_name}, password: ******")
        from user import encrypt_password as encrypt
        response = self.http_client.request(
            "PUT", f"admin/users/{user_name}/password",
            json_body={"new_password": encrypt(password)},
            use_api_base=True, auth_kind="admin"
        )
        res_json = response.json()
        if response.status_code == 200:
            if res_json.get("code") == 0:
                print(res_json.get("message", "Password changed successfully"))
            else:
                print(f"Fail to alter password, code: {res_json.get('code', -1)}, message: {res_json.get('message', 'Unknown error')}")
        else:
            print(f"HTTP error, status: {response.status_code}")

    def create_user(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        user_name_tree: Tree = command["user_name"]
        user_name: str = user_name_tree.children[0].strip("'\"")
        password_tree: Tree = command["password"]
        password: str = password_tree.children[0].strip("'\"")
        role: str = command["role"]
        print(f"Create user: {user_name}, password: ******, role: {role}")
        from user import encrypt_password as encrypt
        response = self.http_client.request(
            "POST", "admin/users",
            json_body={"user_name": user_name, "password": encrypt(password), "role": role},
            use_api_base=True, auth_kind="admin"
        )
        res_json = response.json()
        if response.status_code == 200:
            if res_json.get("code") == 0:
                data = res_json.get("data")
                if data is not None:
                    self._print_table_simple(data)
                else:
                    print(f"User {user_name} created successfully (no data returned)")
            else:
                print(f"Fail to create user {user_name}, code: {res_json.get('code', -1)}, message: {res_json.get('message', 'Unknown error')}")
        else:
            print(f"HTTP error when creating user {user_name}, status: {response.status_code}")

    def activate_user(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        user_name_tree: Tree = command["user_name"]
        user_name: str = user_name_tree.children[0].strip("'\"")
        activate_tree: Tree = command["activate_status"]
        activate_status: str = activate_tree.children[0].strip("'\"")
        if activate_status.lower() in ["on", "off"]:
            print(f"Alter user {user_name} activate status, turn {activate_status.lower()}.")
            response = self.http_client.request(
                "PUT", f"admin/users/{user_name}/activate",
                json_body={"activate_status": activate_status},
                use_api_base=True, auth_kind="admin"
            )
            res_json = response.json()
            if response.status_code == 200:
                if res_json.get("code") == 0:
                    print(res_json.get("message", "Activate status changed successfully"))
                else:
                    print(f"Fail to alter activate status, code: {res_json.get('code', -1)}, message: {res_json.get('message', 'Unknown error')}")
            else:
                print(f"HTTP error, status: {response.status_code}")
        else:
            print(f"Unknown activate status: {activate_status}.")

    def grant_admin(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        user_name_tree: Tree = command["user_name"]
        user_name: str = user_name_tree.children[0].strip("'\"")
        response = self.http_client.request("PUT", f"admin/users/{user_name}/admin", use_api_base=True, auth_kind="admin")
        res_json = response.json()
        if response.status_code == 200:
            if res_json.get("code") == 0:
                print(res_json.get("message", "Grant successfully!"))
            else:
                print(f"Fail to grant {user_name} admin authorization, code: {res_json.get('code')}, message: {res_json.get('message')}")
        else:
            print(f"HTTP error, status: {response.status_code}")

    def revoke_admin(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        user_name_tree: Tree = command["user_name"]
        user_name: str = user_name_tree.children[0].strip("'\"")
        response = self.http_client.request("DELETE", f"admin/users/{user_name}/admin", use_api_base=True, auth_kind="admin")
        res_json = response.json()
        if response.status_code == 200:
            if res_json.get("code") == 0:
                print(res_json.get("message", "Revoke successfully!"))
            else:
                print(f"Fail to revoke {user_name} admin authorization, code: {res_json.get('code')}, message: {res_json.get('message')}")
        else:
            print(f"HTTP error, status: {response.status_code}")

    def list_datasets(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        user_name_tree: Tree = command["user_name"]
        user_name: str = user_name_tree.children[0].strip("'\"")
        print(f"Listing all datasets of user: {user_name}")
        response = self.http_client.request("GET", f"admin/users/{user_name}/datasets", use_api_base=True, auth_kind="admin")
        res_json = response.json()
        if response.status_code == 200:
            if res_json.get("code") == 0:
                data = res_json.get("data")
                if data is not None:
                    for t in data:
                        t.pop("avatar", None)
                    self._print_table_simple(data)
                else:
                    print(f"No datasets available for user {user_name}")
            else:
                print(f"Fail to get all datasets of {user_name}, code: {res_json.get('code', -1)}, message: {res_json.get('message', 'Unknown error')}")
        else:
            print(f"HTTP error, status: {response.status_code}")

    def list_agents(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        user_name_tree: Tree = command["user_name"]
        user_name: str = user_name_tree.children[0].strip("'\"")
        print(f"Listing all agents of user: {user_name}")
        response = self.http_client.request("GET", f"admin/users/{user_name}/agents", use_api_base=True, auth_kind="admin")
        res_json = response.json()
        if response.status_code == 200:
            if res_json.get("code") == 0:
                data = res_json.get("data")
                if data is not None:
                    for t in data:
                        t.pop("avatar", None)
                    self._print_table_simple(data)
                else:
                    print(f"No agents available for user {user_name}")
            else:
                print(f"Fail to get all agents of {user_name}, code: {res_json['code']}, message: {res_json['message']}")
        else:
            print(f"HTTP error, status: {response.status_code}")

    def create_role(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        role_name_tree: Tree = command["role_name"]
        role_name: str = role_name_tree.children[0].strip("'\"")
        desc_str: str = ""
        if "description" in command:
            desc_tree: Tree = command["description"]
            desc_str = desc_tree.children[0].strip("'\"")
        print(f"create role name: {role_name}, description: {desc_str}")
        response = self.http_client.request(
            "POST", "admin/roles",
            json_body={"role_name": role_name, "description": desc_str},
            use_api_base=True, auth_kind="admin"
        )
        res_json = response.json()
        if response.status_code == 200:
            self._print_table_simple(res_json["data"])
        else:
            print(f"Fail to create role {role_name}, code: {res_json['code']}, message: {res_json['message']}")

    def drop_role(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        role_name_tree: Tree = command["role_name"]
        role_name: str = role_name_tree.children[0].strip("'\"")
        print(f"drop role name: {role_name}")
        response = self.http_client.request("DELETE", f"admin/roles/{role_name}", use_api_base=True, auth_kind="admin")
        res_json = response.json()
        if response.status_code == 200:
            self._print_table_simple(res_json["data"])
        else:
            print(f"Fail to drop role {role_name}, code: {res_json['code']}, message: {res_json['message']}")

    def alter_role(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        role_name_tree: Tree = command["role_name"]
        role_name: str = role_name_tree.children[0].strip("'\"")
        desc_tree: Tree = command["description"]
        desc_str: str = desc_tree.children[0].strip("'\"")
        print(f"alter role name: {role_name}, description: {desc_str}")
        response = self.http_client.request(
            "PUT", f"admin/roles/{role_name}",
            json_body={"description": desc_str},
            use_api_base=True, auth_kind="admin"
        )
        res_json = response.json()
        if response.status_code == 200:
            self._print_table_simple(res_json["data"])
        else:
            print(f"Fail to update role {role_name} with description: {desc_str}, code: {res_json['code']}, message: {res_json['message']}")

    def list_roles(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        print("Listing all roles")
        response = self.http_client.request("GET", "admin/roles", use_api_base=True, auth_kind="admin")
        res_json = response.json()
        if response.status_code == 200:
            self._print_table_simple(res_json["data"])
        else:
            print(f"Fail to list roles, code: {res_json['code']}, message: {res_json['message']}")

    def show_role(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        role_name_tree: Tree = command["role_name"]
        role_name: str = role_name_tree.children[0].strip("'\"")
        print(f"show role: {role_name}")
        response = self.http_client.request("GET", f"admin/roles/{role_name}/permission", use_api_base=True, auth_kind="admin")
        res_json = response.json()
        if response.status_code == 200:
            self._print_table_simple(res_json["data"])
        else:
            print(f"Fail to list roles, code: {res_json['code']}, message: {res_json['message']}")

    def grant_permission(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        role_name_tree: Tree = command["role_name"]
        role_name_str: str = role_name_tree.children[0].strip("'\"")
        resource_tree: Tree = command["resource"]
        resource_str: str = resource_tree.children[0].strip("'\"")
        action_tree_list: list = command["actions"]
        actions: list = []
        for action_tree in action_tree_list:
            action_str: str = action_tree.children[0].strip("'\"")
            actions.append(action_str)
        print(f"grant role_name: {role_name_str}, resource: {resource_str}, actions: {actions}")
        response = self.http_client.request(
            "POST", f"admin/roles/{role_name_str}/permission",
            json_body={"actions": actions, "resource": resource_str},
            use_api_base=True, auth_kind="admin"
        )
        res_json = response.json()
        if response.status_code == 200:
            self._print_table_simple(res_json["data"])
        else:
            print(f"Fail to grant role {role_name_str} with {actions} on {resource_str}, code: {res_json['code']}, message: {res_json['message']}")

    def revoke_permission(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        role_name_tree: Tree = command["role_name"]
        role_name_str: str = role_name_tree.children[0].strip("'\"")
        resource_tree: Tree = command["resource"]
        resource_str: str = resource_tree.children[0].strip("'\"")
        action_tree_list: list = command["actions"]
        actions: list = []
        for action_tree in action_tree_list:
            action_str: str = action_tree.children[0].strip("'\"")
            actions.append(action_str)
        print(f"revoke role_name: {role_name_str}, resource: {resource_str}, actions: {actions}")
        response = self.http_client.request(
            "DELETE", f"admin/roles/{role_name_str}/permission",
            json_body={"actions": actions, "resource": resource_str},
            use_api_base=True, auth_kind="admin"
        )
        res_json = response.json()
        if response.status_code == 200:
            self._print_table_simple(res_json["data"])
        else:
            print(f"Fail to revoke role {role_name_str} with {actions} on {resource_str}, code: {res_json['code']}, message: {res_json['message']}")

    def alter_user_role(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        role_name_tree: Tree = command["role_name"]
        role_name_str: str = role_name_tree.children[0].strip("'\"")
        user_name_tree: Tree = command["user_name"]
        user_name_str: str = user_name_tree.children[0].strip("'\"")
        print(f"alter_user_role user_name: {user_name_str}, role_name: {role_name_str}")
        response = self.http_client.request(
            "PUT", f"admin/users/{user_name_str}/role",
            json_body={"role_name": role_name_str},
            use_api_base=True, auth_kind="admin"
        )
        res_json = response.json()
        if response.status_code == 200:
            self._print_table_simple(res_json["data"])
        else:
            print(f"Fail to alter user: {user_name_str} to role {role_name_str}, code: {res_json['code']}, message: {res_json['message']}")

    def show_user_permission(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        user_name_tree: Tree = command["user_name"]
        user_name_str: str = user_name_tree.children[0].strip("'\"")
        print(f"show_user_permission user_name: {user_name_str}")
        response = self.http_client.request("GET", f"admin/users/{user_name_str}/permission", use_api_base=True, auth_kind="admin")
        res_json = response.json()
        if response.status_code == 200:
            self._print_table_simple(res_json["data"])
        else:
            print(f"Fail to show user: {user_name_str} permission, code: {res_json['code']}, message: {res_json['message']}")

    def show_version(self, command: dict):
        if self.server_type == "admin":
            response = self.http_client.request("GET", "admin/version", use_api_base=True, auth_kind="admin")
        else:
            response = self.http_client.request("GET", "system/version", use_api_base=False, auth_kind="web")

        res_json = response.json()
        if response.status_code == 200:
            if self.server_type == "admin":
                self._print_table_simple(res_json["data"])
            else:
                self._print_table_simple({"version": res_json.get("data")})
        else:
            print(f"Fail to show version, code: {res_json.get('code')}, message: {res_json.get('message')}")

    def set_variable(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        var_name_tree: Tree = command["var_name"]
        var_name = var_name_tree.children[0].strip("'\"")
        var_value_tree: Tree = command["var_value"]
        var_value = var_value_tree.children[0].strip("'\"")
        response = self.http_client.request(
            "PUT", "admin/variables",
            json_body={"var_name": var_name, "var_value": var_value},
            use_api_base=True, auth_kind="admin"
        )
        res_json = response.json()
        if response.status_code == 200 and res_json.get("code") == 0:
            print(res_json.get("message", "Set variable successfully"))
        else:
            print(f"Fail to set variable {var_name} to {var_value}, code: {res_json.get('code')}, message: {res_json.get('message')}")

    def show_variable(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        var_name_tree: Tree = command["var_name"]
        var_name = var_name_tree.children[0].strip("'\"")
        response = self.http_client.request(
            "GET", "admin/variables",
            params={"var_name": var_name},
            use_api_base=True, auth_kind="admin"
        )
        res_json = response.json()
        if response.status_code == 200 and res_json.get("code") == 0:
            self._print_table_simple(res_json.get("data"))
        else:
            print(f"Fail to get variable {var_name}, code: {res_json.get('code')}, message: {res_json.get('message')}")

    def list_variables(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        response = self.http_client.request("GET", "admin/variables", use_api_base=True, auth_kind="admin")
        res_json = response.json()
        if response.status_code == 200 and res_json.get("code") == 0:
            self._print_table_simple(res_json.get("data"))
        else:
            print(f"Fail to list variables, code: {res_json.get('code')}, message: {res_json.get('message')}")

    def list_configs(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        response = self.http_client.request("GET", "admin/configs", use_api_base=True, auth_kind="admin")
        res_json = response.json()
        if response.status_code == 200 and res_json.get("code") == 0:
            self._print_table_simple(res_json.get("data"))
        else:
            print(f"Fail to list configs, code: {res_json.get('code')}, message: {res_json.get('message')}")

    def list_environments(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        response = self.http_client.request("GET", "admin/environments", use_api_base=True, auth_kind="admin")
        res_json = response.json()
        if response.status_code == 200 and res_json.get("code") == 0:
            self._print_table_simple(res_json.get("data"))
        else:
            print(f"Fail to list environments, code: {res_json.get('code')}, message: {res_json.get('message')}")

    def show_fingerprint(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        response = self.http_client.request("GET", "admin/fingerprint", use_api_base=True, auth_kind="admin")
        res_json = response.json()
        if response.status_code == 200:
            self._print_table_simple(res_json["data"])
        else:
            print(f"Fail to show fingerprint, code: {res_json['code']}, message: {res_json['message']}")

    def set_license(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        license = command["license"]
        response = self.http_client.request("POST", "admin/license", json_body={"license": license}, use_api_base=True, auth_kind="admin")
        res_json = response.json()
        if response.status_code == 200:
            print("Set license successfully")
        else:
            print(f"Fail to set license, code: {res_json['code']}, message: {res_json['message']}")

    def set_license_config(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        value1 = command["value1"]
        value2 = command["value2"]
        response = self.http_client.request("POST", "admin/license/config",
                                            json_body={"value1": value1, "value2": value2}, use_api_base=True,
                                            auth_kind="admin")
        res_json = response.json()
        if response.status_code == 200:
            print("Set license config successfully")
        else:
            print(f"Fail to set license config, code: {res_json['code']}, message: {res_json['message']}")

    def show_license(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        response = self.http_client.request("GET", "admin/license", use_api_base=True, auth_kind="admin")
        res_json = response.json()
        if response.status_code == 200:
            self._print_table_simple(res_json["data"])
        else:
            print(f"Fail to show license, code: {res_json['code']}, message: {res_json['message']}")

    def check_license(self, command: dict):
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        response = self.http_client.request("GET", "admin/license?check=true", use_api_base=True, auth_kind="admin")
        res_json = response.json()
        if response.status_code == 200:
            print(res_json["data"])
        else:
            print(f"Fail to check license, code: {res_json['code']}, message: {res_json['message']}")

    def generate_key(self, command: dict[str, Any]) -> None:
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        username_tree: Tree = command["user_name"]
        user_name: str = username_tree.children[0].strip("'\"")
        print(f"Generating API key for user: {user_name}")
        response = self.http_client.request("POST", f"admin/users/{user_name}/keys", use_api_base=True, auth_kind="admin")
        res_json = response.json()
        if response.status_code == 200:
            self._print_table_simple(res_json["data"])
        else:
            print(f"Failed to generate key for user {user_name}, code: {res_json['code']}, message: {res_json['message']}")

    def list_keys(self, command: dict[str, Any]) -> None:
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        username_tree: Tree = command["user_name"]
        user_name: str = username_tree.children[0].strip("'\"")
        print(f"Listing API keys for user: {user_name}")
        response = self.http_client.request("GET", f"admin/users/{user_name}/keys", use_api_base=True, auth_kind="admin")
        res_json = response.json()
        if response.status_code == 200:
            self._print_table_simple(res_json["data"])
        else:
            print(f"Failed to list keys for user {user_name}, code: {res_json['code']}, message: {res_json['message']}")

    def drop_key(self, command: dict[str, Any]) -> None:
        if self.server_type != "admin":
            print("This command is only allowed in ADMIN mode")
            return
        key_tree: Tree = command["key"]
        key: str = key_tree.children[0].strip("'\"")
        username_tree: Tree = command["user_name"]
        user_name: str = username_tree.children[0].strip("'\"")
        print(f"Dropping API key for user: {user_name}")
        encoded_key: str = urllib.parse.quote(key, safe="")
        response = self.http_client.request("DELETE", f"admin/users/{user_name}/keys/{encoded_key}", use_api_base=True, auth_kind="admin")
        res_json = response.json()
        if response.status_code == 200:
            print(res_json["message"])
        else:
            print(f"Failed to drop key for user {user_name}, code: {res_json['code']}, message: {res_json['message']}")

    def create_token(self, command: dict[str, Any]) -> None:
        """USER 模式：为当前登录用户创建一个 API token。对接主 API POST /v1/system/new_token。"""
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        name_tree: Tree = command["name"]
        name: str = name_tree.children[0].strip("'\"")
        response = self.http_client.request(
            "POST", "system/new_token", use_api_base=False, auth_kind="web", json_body={"name": name}
        )
        res_json = response.json()
        if response.status_code == 200:
            self._print_table_simple(res_json["data"])
        else:
            print(f"Failed to create token, code: {res_json.get('code', res_json.get('retcode'))}, message: {res_json.get('message', res_json.get('retmsg'))}")

    def list_tokens(self, command: dict[str, Any]) -> None:
        """USER 模式：列出当前登录用户的所有 API token。对接主 API GET /v1/system/token_list。"""
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        response = self.http_client.request(
            "GET", "system/token_list", use_api_base=False, auth_kind="web"
        )
        res_json = response.json()
        if response.status_code == 200:
            self._print_table_simple(res_json["data"])
        else:
            print(f"Failed to list tokens, code: {res_json.get('code', res_json.get('retcode'))}, message: {res_json.get('message', res_json.get('retmsg'))}")

    def drop_token(self, command: dict[str, Any]) -> None:
        """USER 模式：删除当前登录用户的某个 API token。对接主 API POST /v1/system/rm。"""
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        token_tree: Tree = command["token"]
        token: str = token_tree.children[0].strip("'\"")
        response = self.http_client.request(
            "POST", "system/rm", use_api_base=False, auth_kind="web", json_body=token
        )
        res_json = response.json()
        if response.status_code == 200:
            print("Token dropped successfully")
        else:
            print(f"Failed to drop token, code: {res_json.get('code', res_json.get('retcode'))}, message: {res_json.get('message', res_json.get('retmsg'))}")

    def _print_key_value(self, data: dict) -> None:
        """Print data as key-value pairs (one per line)."""
        if not data:
            print("No data to print")
            return
        for key, value in data.items():
            print(f"{key}: {value}")

    def get_chunk(self, command: dict[str, Any]) -> None:
        """USER 模式：按 ID 获取单个 chunk。对接 Go server GET /v1/chunk/get。"""
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        chunk_id_tree: Tree = command["chunk_id"]
        chunk_id: str = chunk_id_tree.children[0].strip("'\"")
        response = self.http_client.request(
            "GET", f"chunk/get?chunk_id={chunk_id}", use_api_base=False, auth_kind="web"
        )
        res_json = response.json()
        if response.status_code == 200 and res_json.get("code") == 0:
            self._print_key_value(res_json["data"])
        else:
            print(f"Fail to get chunk, code: {res_json.get('code', res_json.get('retcode'))}, message: {res_json.get('message', res_json.get('retmsg'))}")

    def list_chunks(self, command: dict[str, Any]) -> None:
        """USER 模式：列出某文档的所有 chunk。对接 Go server POST /v1/chunk/list。"""
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        doc_id_tree: Tree = command["doc_id"]
        payload: dict[str, Any] = {"doc_id": doc_id_tree.children[0].strip("'\"")}
        if "page" in command:
            payload["page"] = command["page"]
        if "size" in command:
            payload["size"] = command["size"]
        if "keywords" in command:
            payload["keywords"] = command["keywords"].children[0].strip("'\"")
        if "available_int" in command:
            payload["available_int"] = command["available_int"]
        response = self.http_client.request(
            "POST", "chunk/list", use_api_base=False, auth_kind="web", json_body=payload
        )
        res_json = response.json()
        if response.status_code == 200 and res_json.get("code") == 0:
            chunks = res_json["data"]["chunks"]
            if chunks:
                for i, chunk in enumerate(chunks):
                    print(f"\n--- Chunk {i + 1} ---")
                    for key, value in chunk.items():
                        print(f"  {key}: {value}")
            else:
                print("No chunks found")
        else:
            print(f"Fail to list chunks, code: {res_json.get('code', res_json.get('retcode'))}, message: {res_json.get('message', res_json.get('retmsg'))}")

    def list_user_datasets(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        # 迁移至 RESTful：GET /api/v1/datasets（page_size 放大以保留旧 kb/list 列全部的行为）
        response = self.http_client.request("GET", "datasets?page_size=1000", use_api_base=True, auth_kind="web")
        res_json = response.json()
        if response.status_code == 200:
            data = res_json.get("data", {})
            datasets = data.get("kbs", []) if isinstance(data, dict) else data
            self._print_table_simple(datasets)
        else:
            print(f"Fail to list datasets, code: {res_json.get('code', res_json.get('retcode'))}, message: {res_json.get('message', res_json.get('retmsg'))}")

    def list_user_agents(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        response = self.http_client.request("GET", "canvas/list", use_api_base=False, auth_kind="web")
        res_json = response.json()
        if response.status_code == 200:
            self._print_table_simple(res_json.get("data", []))
        else:
            print(f"Fail to list agents, code: {res_json.get('retcode')}, message: {res_json.get('retmsg')}")

    def list_user_chats(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        response = self.http_client.request("GET", "chats", use_api_base=True, auth_kind="web")
        res_json = response.json()
        if response.status_code == 200:
            data = res_json.get("data", {})
            self._print_table_simple(data.get("chats", []) if isinstance(data, dict) else data)
        else:
            print(f"Fail to list chats, code: {res_json.get('code', res_json.get('retcode'))}, message: {res_json.get('message', res_json.get('retmsg'))}")

    def list_user_model_providers(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        response = self.http_client.request("GET", "llm/my_llms", use_api_base=False, auth_kind="web")
        res_json = response.json()
        if response.status_code == 200:
            new_input = []
            for key, value in res_json.get("data", {}).items():
                new_input.append({"model_provider": key, "models": value})
            self._print_table_simple(new_input)
        else:
            print(f"Fail to list model providers, code: {res_json.get('retcode')}, message: {res_json.get('retmsg')}")

    def list_user_default_models(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        response = self.http_client.request("GET", "user/tenant_info", use_api_base=False, auth_kind="web")
        res_json = response.json()
        if response.status_code == 200:
            new_input = []
            for key, value in res_json.get("data", {}).items():
                if key == "asr_id" and value != "":
                    new_input.append({"model_category": "ASR", "model_name": value})
                elif key == "embd_id" and value != "":
                    new_input.append({"model_category": "Embedding", "model_name": value})
                elif key == "llm_id" and value != "":
                    new_input.append({"model_category": "LLM", "model_name": value})
                elif key == "rerank_id" and value != "":
                    new_input.append({"model_category": "Reranker", "model_name": value})
                elif key == "tts_id" and value != "":
                    new_input.append({"model_category": "TTS", "model_name": value})
                elif key == "img2txt_id" and value != "":
                    new_input.append({"model_category": "VLM", "model_name": value})
            self._print_table_simple(new_input)
        else:
            print(f"Fail to list default models, code: {res_json.get('retcode')}, message: {res_json.get('retmsg')}")

    def login_user(self, command: dict):
        """Re-login as a different user during interactive session."""
        try:
            response = self.http_client.request("GET", "system/ping", use_api_base=False, auth_kind="web")
            if response.status_code == 200 and response.content == b"pong":
                pass
            else:
                print("Server is down")
                return
        except Exception as e:
            print(str(e))
            print("Can't access server for login (connection failed)")
            return

        from user import login_user as do_login
        email: str = command["email"]
        import getpass
        password = getpass.getpass(f"password for {email}: ").strip()
        try:
            token = do_login(self.http_client, self.server_type, email, password)
            if not token.startswith("Bearer "):
                token = f"Bearer {token}"
            self.http_client.login_token = token
            print(f"Logged in as {email}")
        except Exception as e:
            print(f"Login failed: {e}")

    def ping_server(self, command: dict):
        iterations = command.get("iterations", 1)
        if iterations > 1:
            response = self.http_client.request("GET", "system/ping", use_api_base=False, auth_kind=None,
                                                iterations=iterations)
            return response
        else:
            response = self.http_client.request("GET", "system/ping", use_api_base=False, auth_kind=None)
            is_alive = False
            if response.status_code == 200:
                if response.content == b"pong":
                    is_alive = True
                else:
                    try:
                        payload = response.json()
                    except Exception:
                        payload = None
                    if payload == ["pong", 200]:
                        is_alive = True
            if is_alive:
                print("Server is alive")
            else:
                print("Server is down")
            return None

    def register_user(self, command: dict):
        user_name: str = command["user_name"]
        nickname: str = command["nickname"]
        password: str = command["password"]
        print(f"Register user: {nickname}, email: {user_name}, password: ******")
        payload = {"email": user_name, "nickname": nickname, "password": password}
        response = self.http_client.request("POST", "user/register", json_body=payload, use_api_base=False, auth_kind=None)
        res_json = response.json()
        code = res_json.get("code", res_json.get("retcode", -1))
        if response.status_code == 200 and code == 0:
            print(f"Success to register user: {user_name}")
        else:
            msg = res_json.get("message", res_json.get("retmsg", ""))
            print(f"Fail to register user {user_name}: {msg}")

    def show_current_user(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        response = self.http_client.request("GET", "user/info", use_api_base=False, auth_kind="web")
        res_json = response.json()
        code = res_json.get("code", res_json.get("retcode", -1))
        if response.status_code == 200 and code == 0:
            data = res_json.get("data", {})
            if isinstance(data, list) and data:
                data = data[0]
            if isinstance(data, dict):
                data.pop("avatar", None)
            self._print_table_simple(data)
        else:
            msg = res_json.get("message", res_json.get("retmsg", ""))
            print(f"Fail to get current user: {msg}")

    def create_model_provider(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        llm_factory: str = command["provider_name"]
        api_key: str = command["provider_key"]
        payload = {"api_key": api_key, "llm_factory": llm_factory}
        response = self.http_client.request("POST", "llm/set_api_key", json_body=payload, use_api_base=False, auth_kind="web")
        res_json = response.json()
        code = res_json.get("code", res_json.get("retcode", -1))
        if response.status_code == 200 and code == 0:
            print(f"Success to add model provider {llm_factory}")
        else:
            msg = res_json.get("message", res_json.get("retmsg", ""))
            print(f"Fail to add model provider {llm_factory}: {msg}")

    def drop_model_provider(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        llm_factory: str = command["provider_name"]
        payload = {"llm_factory": llm_factory}
        response = self.http_client.request("POST", "llm/delete_factory", json_body=payload, use_api_base=False, auth_kind="web")
        res_json = response.json()
        code = res_json.get("code", res_json.get("retcode", -1))
        if response.status_code == 200 and code == 0:
            print(f"Success to drop model provider {llm_factory}")
        else:
            msg = res_json.get("message", res_json.get("retmsg", ""))
            print(f"Fail to drop model provider {llm_factory}: {msg}")

    def set_default_model(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        model_type: str = command["model_type"]
        model_id: str = command["model_id"]
        self._set_default_models(model_type, model_id)

    def reset_default_model(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        model_type: str = command["model_type"]
        self._set_default_models(model_type, "")

    def create_user_dataset(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        # 迁移至 RESTful：POST /api/v1/datasets（字段对齐新接口：embedding_model / chunk_method）
        payload = {
            "name": command["dataset_name"],
            "embedding_model": command["embedding"],
        }
        if "parser_type" in command:
            payload["chunk_method"] = command["parser_type"]
        if "pipeline" in command:
            # CreateDatasetRequest 暂无 pipeline_id 字段，经 ext 透传（ext 会并入 create_with_name）
            payload["ext"] = {"pipeline_id": command["pipeline"]}
        response = self.http_client.request("POST", "datasets", json_body=payload, use_api_base=True, auth_kind="web")
        res_json = response.json()
        code = res_json.get("code", res_json.get("retcode", -1))
        if response.status_code == 200 and code == 0:
            self._print_table_simple(res_json.get("data", {}))
        else:
            msg = res_json.get("message", res_json.get("retmsg", ""))
            print(f"Fail to create dataset: {msg}")

    def drop_user_dataset(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        dataset_name = command["dataset_name"]
        dataset_id = self._get_dataset_id(dataset_name)
        if dataset_id is None:
            return
        # 迁移至 RESTful：DELETE /api/v1/datasets（入参由 {kb_id} 改为 {ids:[...]}）
        payload = {"ids": [dataset_id]}
        response = self.http_client.request("DELETE", "datasets", json_body=payload, use_api_base=True, auth_kind="web")
        res_json = response.json()
        code = res_json.get("code", res_json.get("retcode", -1))
        if response.status_code == 200 and code == 0:
            print(f"Drop dataset {dataset_name} successfully")
        else:
            msg = res_json.get("message", res_json.get("retmsg", ""))
            print(f"Fail to drop dataset {dataset_name}: {msg}")

    def list_user_dataset_files(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        dataset_name = command["dataset_name"]
        dataset_id = self._get_dataset_id(dataset_name)
        if dataset_id is None:
            return
        docs = self._list_documents(dataset_name, dataset_id)
        if docs is not None:
            self._print_table_simple(docs)

    def list_user_dataset_documents(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        dataset_name = command["dataset_name"]
        dataset_id = self._get_dataset_id(dataset_name)
        if dataset_id is None:
            return
        docs = self._list_documents(dataset_name, dataset_id)
        if docs is None:
            return
        if not docs:
            print(f"No documents found in dataset {dataset_name}")
            return

        display_docs = []
        for doc in docs:
            display_doc = {
                "name": doc.get("name", ""),
                "id": doc.get("id", ""),
                "size": doc.get("size", 0),
                "status": doc.get("status", ""),
                "created_at": doc.get("created_at", doc.get("create_time", "")),
            }
            meta_fields = doc.get("meta_fields") or {}
            if meta_fields:
                display_doc["meta_fields"] = json.dumps(meta_fields, ensure_ascii=False)
            display_docs.append(display_doc)
        self._print_table_simple(display_docs)

    def list_user_datasets_metadata(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return

        dataset_ids = []
        for dataset_name in command["dataset_names"]:
            dataset_id = self._get_dataset_id(dataset_name)
            if dataset_id is None:
                continue
            dataset_ids.append(dataset_id)

        if not dataset_ids:
            print("No valid datasets found")
            return

        kb_ids = ",".join(dataset_ids)
        response = self.http_client.request("GET", f"kb/get_meta?kb_ids={kb_ids}", use_api_base=False, auth_kind="web")
        res_json = response.json()
        code = res_json.get("code", res_json.get("retcode", -1))
        if response.status_code != 200 or code != 0:
            msg = res_json.get("message", res_json.get("retmsg", ""))
            print(f"Fail to get metadata: {msg}")
            return

        meta = res_json.get("data") or {}
        if not meta:
            print("No metadata found")
            return

        rows = []
        for field_name, values in meta.items():
            if not isinstance(values, dict):
                continue
            for value, doc_ids in values.items():
                if isinstance(doc_ids, list):
                    doc_id_text = ", ".join(str(doc_id) for doc_id in doc_ids)
                else:
                    doc_id_text = str(doc_ids)
                rows.append({"field": field_name, "value": value, "doc_ids": doc_id_text})
        self._print_table_simple(rows)

    def list_user_documents_metadata_summary(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return

        dataset_name = command["dataset_name"]
        dataset_id = self._get_dataset_id(dataset_name)
        if dataset_id is None:
            return

        payload = {"kb_id": dataset_id}
        doc_ids = command.get("document_ids") or []
        if doc_ids:
            payload["doc_ids"] = doc_ids

        response = self.http_client.request("POST", "document/metadata/summary", json_body=payload,
                                            use_api_base=False, auth_kind="web")
        res_json = response.json()
        code = res_json.get("code", res_json.get("retcode", -1))
        if response.status_code != 200 or code != 0:
            msg = res_json.get("message", res_json.get("retmsg", ""))
            print(f"Fail to get metadata summary: {msg}")
            return

        summary = (res_json.get("data") or {}).get("summary") or {}
        if not summary:
            print("No metadata summary found")
            return

        rows = []
        for field_name, field_info in summary.items():
            field_type = ""
            values = []
            if isinstance(field_info, dict):
                field_type = field_info.get("type", "")
                values = field_info.get("values", [])
            elif isinstance(field_info, list):
                values = field_info
            for item in values:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    value, count = item[0], item[1]
                else:
                    value, count = item, ""
                rows.append({"field": field_name, "type": field_type, "value": value, "count": count})
        self._print_table_simple(rows)

    def create_user_chat(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        chat_name = command["chat_name"]
        default_models = self._get_default_models() or {}
        payload = {
            "name": chat_name,
            "description": "",
            "icon": "",
            "dataset_ids": [],
            "llm_setting": {},
            "prompt_config": {
                "empty_response": "",
                "prologue": "Hi! I'm your assistant. What can I do for you?",
                "quote": True,
                "keyword": False,
                "tts": False,
                "system": (
                    "You are an intelligent assistant. Please answer questions based on the provided knowledge base.\n"
                    "{knowledge}"
                ),
                "refine_multiturn": False,
                "use_kg": False,
                "reasoning": False,
                "parameters": [{"key": "knowledge", "optional": False}],
                "toc_enhance": False,
            },
            "similarity_threshold": 0.2,
            "top_n": 8,
            "top_k": 1024,
            "vector_similarity_weight": 0.3,
            "rerank_id": default_models.get("rerank_id", ""),
        }
        if default_models.get("llm_id"):
            payload["llm_id"] = default_models["llm_id"]
        response = self.http_client.request("POST", "chats", json_body=payload, use_api_base=True, auth_kind="web")
        res_json = response.json()
        code = res_json.get("code", res_json.get("retcode", -1))
        if response.status_code == 200 and code == 0:
            print(f"Success to create chat: {chat_name}")
        else:
            msg = res_json.get("message", res_json.get("retmsg", ""))
            print(f"Fail to create chat {chat_name}: {msg}")

    def drop_user_chat(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        chat_name = command["chat_name"]
        chats = self._list_chats()
        if chats is None:
            return
        to_drop_ids = [c["id"] for c in chats if c.get("name") == chat_name]
        if not to_drop_ids:
            print(f"Chat '{chat_name}' not found")
            return
        payload = {"ids": to_drop_ids}
        response = self.http_client.request("DELETE", "chats", json_body=payload, use_api_base=True, auth_kind="web")
        res_json = response.json()
        code = res_json.get("code", res_json.get("retcode", -1))
        if response.status_code == 200 and code == 0:
            print(f"Success to drop chat: {chat_name}")
        else:
            msg = res_json.get("message", res_json.get("retmsg", ""))
            print(f"Fail to drop chat {chat_name}: {msg}")

    def create_index(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        dataset_name = command["dataset_name"]
        vector_size = command.get("vector_size")
        if not vector_size:
            print("vector_size is required")
            return
        # Get dataset ID by name
        dataset_id = self._get_dataset_id(dataset_name)
        if dataset_id is None:
            return
        payload = {"kb_id": dataset_id, "vector_size": vector_size}
        response = self.http_client.request("POST", "kb/index", json_body=payload,
                                            use_api_base=False, auth_kind="web")
        res_json = response.json()
        if response.status_code == 200 and res_json.get("code") == 0:
            print(f"Success to create index for dataset: {dataset_name}")
        else:
            print(f"Fail to create index for dataset {dataset_name}, code: {res_json.get('code')}, message: {res_json.get('message')}")

    def drop_index(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        dataset_name = command["dataset_name"]
        # Get dataset ID by name
        dataset_id = self._get_dataset_id(dataset_name)
        if dataset_id is None:
            return
        payload = {"kb_id": dataset_id}
        response = self.http_client.request("DELETE", "kb/index", json_body=payload,
                                            use_api_base=False, auth_kind="web")
        res_json = response.json()
        if response.status_code == 200 and res_json.get("code") == 0:
            print(f"Success to drop index for dataset: {dataset_name}")
        else:
            print(f"Fail to drop index for dataset {dataset_name}, code: {res_json.get('code')}, message: {res_json.get('message')}")

    def create_doc_meta_index(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        response = self.http_client.request("POST", "tenant/doc_meta_index",
                                            use_api_base=False, auth_kind="web")
        res_json = response.json()
        if response.status_code == 200 and res_json.get("code") == 0:
            print("Success to create doc meta index")
        else:
            print(f"Fail to create doc meta index, code: {res_json.get('code')}, message: {res_json.get('message')}")

    def drop_doc_meta_index(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        response = self.http_client.request("DELETE", "tenant/doc_meta_index",
                                            use_api_base=False, auth_kind="web")
        res_json = response.json()
        if response.status_code == 200 and res_json.get("code") == 0:
            print("Success to drop doc meta index")
        else:
            print(f"Fail to drop doc meta index, code: {res_json.get('code')}, message: {res_json.get('message')}")

    def create_chat_session(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        chat_name = command["chat_name"]
        dialog_id = self._get_chat_id_by_name(chat_name)
        if dialog_id is None:
            return
        payload = {"name": "New session"}
        response = self.http_client.request(
            "POST",
            f"chats/{dialog_id}/sessions",
            json_body=payload,
            use_api_base=True,
            auth_kind="web",
        )
        res_json = response.json()
        code = res_json.get("code", res_json.get("retcode", -1))
        if response.status_code == 200 and code == 0:
            print(f"Success to create chat session for chat: {chat_name}")
        else:
            msg = res_json.get("message", res_json.get("retmsg", ""))
            print(f"Fail to create chat session for chat {chat_name}: {msg}")

    def drop_chat_session(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        chat_name = command["chat_name"]
        session_id = command["session_id"]
        dialog_id = self._get_chat_id_by_name(chat_name)
        if dialog_id is None:
            return
        sessions = self._list_chat_sessions(dialog_id)
        if sessions is None:
            return
        to_drop_session_ids = []
        for session in sessions:
            if session["id"] == session_id:
                to_drop_session_ids.append(session["id"])
        if not to_drop_session_ids:
            print(f"Chat session '{session_id}' not found in chat '{chat_name}'")
            return
        payload = {"ids": to_drop_session_ids}
        response = self.http_client.request(
            "DELETE",
            f"chats/{dialog_id}/sessions",
            json_body=payload,
            use_api_base=True,
            auth_kind="web",
        )
        res_json = response.json()
        code = res_json.get("code", res_json.get("retcode", -1))
        if response.status_code == 200 and code == 0:
            print(f"Success to drop chat session '{session_id}' from chat: {chat_name}")
        else:
            msg = res_json.get("message", res_json.get("retmsg", ""))
            print(f"Fail to drop chat session '{session_id}' from chat {chat_name}: {msg}")

    def list_chat_sessions(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        chat_name = command["chat_name"]
        dialog_id = self._get_chat_id_by_name(chat_name)
        if dialog_id is None:
            return
        sessions = self._list_chat_sessions(dialog_id)
        if sessions is None:
            return
        for session in sessions:
            session["chat_name"] = chat_name
        if "iterations" in command:
            return sessions
        self._print_table_simple(sessions)

    def chat_on_session(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        message = command["message"]
        session_id = command["session_id"]
        chat_name = command["chat_name"]
        dialog_id = self._get_chat_id_by_name(chat_name)
        if dialog_id is None:
            return
        payload = {"messages": [{"role": "user", "content": message}]}
        response = self.http_client.request(
            "POST",
            f"chats/{dialog_id}/sessions/{session_id}/completions",
            json_body=payload,
            use_api_base=True,
            auth_kind="web",
            stream=True,
        )
        if response.status_code != 200:
            print(f"Fail to chat on session, status code: {response.status_code}")
            return
        print("Assistant: ", end="", flush=True)
        for line in response.iter_lines():
            if not line:
                continue
            line_str = line.decode("utf-8")
            if not line_str.startswith("data:"):
                continue
            data_str = line_str[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                data_json = json.loads(data_str)
                if data_json.get("data") is True:
                    break
                answer = data_json.get("data", {}).get("answer", "")
                if answer:
                    print(answer, end="", flush=True)
            except json.JSONDecodeError:
                continue
        print()

    def import_docs_into_dataset(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        if MultipartEncoder is None:
            print("requests-toolbelt is required. Install with: uv add requests-toolbelt")
            return
        dataset_name = command["dataset_name"]
        dataset_id = self._get_dataset_id(dataset_name)
        if dataset_id is None:
            return
        document_paths = command["document_paths"]
        paths = [Path(p) for p in document_paths]
        fields = []
        file_handles = []
        try:
            for path in paths:
                fh = path.open("rb")
                fields.append(("file", (path.name, fh)))
                file_handles.append(fh)
            fields.append(("kb_id", dataset_id))
            encoder = MultipartEncoder(fields=fields)
            headers = {"Content-Type": encoder.content_type}
            response = self.http_client.request("POST", "document/upload", headers=headers, data=encoder,
                                                json_body=None, use_api_base=False, auth_kind="web")
            res_json = response.json()
            code = res_json.get("code", res_json.get("retcode", -1))
            if code == 0:
                print(f"Success to import documents into dataset {dataset_name}")
            else:
                msg = res_json.get("message", res_json.get("retmsg", ""))
                print(f"Fail to import documents: {msg}")
        except Exception as exc:
            print(f"Fail to import documents into dataset {dataset_name}: {exc}")
        finally:
            for fh in file_handles:
                fh.close()

    def search_on_datasets(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        dataset_names = command["datasets"]
        dataset_ids = []
        for name in dataset_names:
            did = self._get_dataset_id(name)
            if did is None:
                return
            dataset_ids.append(did)
        payload = {
            "question": command["question"],
            "kb_id": dataset_ids,
            "similarity_threshold": 0.2,
            "vector_similarity_weight": 0.3,
        }
        response = self.http_client.request("POST", "chunk/retrieval_test", json_body=payload, use_api_base=False, auth_kind="web")
        res_json = response.json()
        code = res_json.get("code", res_json.get("retcode", -1))
        if response.status_code == 200 and code == 0:
            self._print_table_simple(res_json.get("data", {}).get("chunks", []))
        else:
            msg = res_json.get("message", res_json.get("retmsg", ""))
            print(f"Fail to search datasets {dataset_names}: {msg}")

    def parse_dataset_docs(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        dataset_name = command["dataset_name"]
        dataset_id = self._get_dataset_id(dataset_name)
        if dataset_id is None:
            return
        docs = self._list_documents(dataset_name, dataset_id)
        if docs is None:
            return
        document_names = list(command["document_names"])
        document_ids = []
        to_parse = []
        for doc in docs:
            if doc["name"] in document_names:
                document_ids.append(doc["id"])
                to_parse.append(doc["name"])
                document_names.remove(doc["name"])
        if not document_ids:
            print(f"No matching documents found in {dataset_name}")
            return
        if document_names:
            print(f"Documents not found: {document_names}")
        payload = {"doc_ids": document_ids, "run": 1}
        response = self.http_client.request("POST", "document/run", json_body=payload, use_api_base=False, auth_kind="web")
        res_json = response.json()
        code = res_json.get("code", res_json.get("retcode", -1))
        if response.status_code == 200 and code == 0:
            print(f"Success to parse {to_parse} of {dataset_name}")
        else:
            msg = res_json.get("message", res_json.get("retmsg", ""))
            print(f"Fail to parse documents: {msg}")

    def parse_dataset(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        dataset_name = command["dataset_name"]
        method = command["method"]
        dataset_id = self._get_dataset_id(dataset_name)
        if dataset_id is None:
            return
        docs = self._list_documents(dataset_name, dataset_id)
        if docs is None:
            return
        document_ids = [doc["id"] for doc in docs]
        payload = {"doc_ids": document_ids, "run": 1}
        response = self.http_client.request("POST", "document/run", json_body=payload, use_api_base=False, auth_kind="web")
        res_json = response.json()
        code = res_json.get("code", res_json.get("retcode", -1))
        if not (response.status_code == 200 and code == 0):
            msg = res_json.get("message", res_json.get("retmsg", ""))
            print(f"Fail to start parse dataset {dataset_name}: {msg}")
            return
        if method == "async":
            print(f"Success to start parse dataset {dataset_name}")
        else:
            print(f"Start to parse dataset {dataset_name}, please wait...")
            if self._wait_parse_done(dataset_name, dataset_id):
                print(f"Success to parse dataset {dataset_name}")
            else:
                print(f"Parse dataset {dataset_name} timeout")

    # Private helper methods

    def _get_dataset_id(self, dataset_name: str) -> str | None:
        # 迁移至 RESTful：GET /api/v1/datasets?name=<精确名>。
        # 用服务端 name 精确过滤而非"列全部再循环"，与数据集总数无关，避免分页漏查、也无需魔法上限。
        encoded_name = urllib.parse.quote(dataset_name, safe="")
        response = self.http_client.request("GET", f"datasets?name={encoded_name}", use_api_base=True, auth_kind="web")
        res_json = response.json()
        if response.status_code != 200:
            msg = res_json.get("message", res_json.get("retmsg", ""))
            print(f"Fail to list datasets: {msg}")
            return None
        data = res_json.get("data", [])
        # data 可能是 list，也可能是 {"kbs": [...]}（兼容旧形态）
        if isinstance(data, dict):
            data = data.get("kbs", [])
        for dataset in data:
            if dataset.get("name") == dataset_name:
                return dataset.get("id")
        print(f"Dataset '{dataset_name}' not found")
        return None

    def _list_documents(self, dataset_name: str, dataset_id: str) -> list | None:
        response = self.http_client.request("POST", f"document/list?kb_id={dataset_id}", json_body={},
                                            use_api_base=False, auth_kind="web")
        res_json = response.json()
        if response.status_code != 200:
            msg = res_json.get("message", res_json.get("retmsg", ""))
            print(f"Fail to list files from dataset {dataset_name}: {msg}")
            return None
        data = res_json.get("data", {})
        if isinstance(data, dict):
            return data.get("docs", [])
        return data

    def _get_chat_id_by_name(self, chat_name: str) -> str | None:
        chats = self._list_chats()
        if chats is None:
            return None
        for chat in chats:
            if chat.get("name") == chat_name:
                return chat.get("id")
        print(f"Chat '{chat_name}' not found")
        return None

    def _list_chat_sessions(self, dialog_id: str) -> list | None:
        response = self.http_client.request("GET", f"chats/{dialog_id}/sessions", use_api_base=True, auth_kind="web")
        res_json = response.json()
        code = res_json.get("code", res_json.get("retcode", -1))
        if response.status_code == 200 and code == 0:
            return res_json.get("data", [])
        else:
            msg = res_json.get("message", res_json.get("retmsg", ""))
            print(f"Fail to list chat sessions: {msg}")
            return None

    def _list_chats(self) -> list | None:
        response = self.http_client.request("GET", "chats", use_api_base=True, auth_kind="web")
        res_json = response.json()
        code = res_json.get("code", res_json.get("retcode", -1))
        if response.status_code == 200 and code == 0:
            data = res_json.get("data", {})
            if isinstance(data, dict):
                return data.get("chats", [])
            return data
        msg = res_json.get("message", res_json.get("retmsg", ""))
        print(f"Fail to list chats: {msg}")
        return None

    def _get_default_models(self) -> dict | None:
        response = self.http_client.request("GET", "user/tenant_info", use_api_base=False, auth_kind="web")
        res_json = response.json()
        if response.status_code == 200:
            code = res_json.get("code", res_json.get("retcode", -1))
            if code == 0:
                return res_json.get("data", {})
            msg = res_json.get("message", res_json.get("retmsg", ""))
            print(f"Fail to get default models, code: {code}, message: {msg}")
            return None
        else:
            print(f"Fail to get default models, HTTP code: {response.status_code}, message: {res_json}")
            return None

    def _set_default_models(self, model_type: str, model_id: str):
        current = self._get_default_models()
        if current is None:
            return
        current[model_type] = model_id
        payload = {
            "tenant_id": current.get("tenant_id", ""),
            "llm_id": current.get("llm_id", ""),
            "embd_id": current.get("embd_id", ""),
            "img2txt_id": current.get("img2txt_id", ""),
            "asr_id": current.get("asr_id", ""),
            "tts_id": current.get("tts_id", ""),
            "rerank_id": current.get("rerank_id", ""),
        }
        response = self.http_client.request("POST", "user/set_tenant_info", json_body=payload,
                                            use_api_base=False, auth_kind="web")
        res_json = response.json()
        code = res_json.get("code", res_json.get("retcode", -1))
        if response.status_code == 200 and code == 0:
            print(f"Success to set {model_type} to '{model_id}'")
        else:
            msg = res_json.get("message", res_json.get("retmsg", ""))
            print(f"Fail to set {model_type}: {msg}")

    def _wait_parse_done(self, dataset_name: str, dataset_id: str) -> bool:
        start = time.monotonic()
        while True:
            docs = self._list_documents(dataset_name, dataset_id)
            if docs is None:
                return False
            all_done = all(doc.get("run") == "3" for doc in docs)
            if all_done:
                return True
            if time.monotonic() - start > 60:
                return False
            time.sleep(0.5)


def run_command(client: MultiRAGClient, command_dict: dict):
    command_type = command_dict["type"]

    match command_type:
        case "list_services":
            client.list_services(command_dict)
        case "show_service":
            client.show_service(command_dict)
        case "restart_service":
            client.restart_service(command_dict)
        case "shutdown_service":
            client.shutdown_service(command_dict)
        case "startup_service":
            client.startup_service(command_dict)
        case "list_users":
            client.list_users(command_dict)
        case "show_user":
            client.show_user(command_dict)
        case "drop_user":
            client.drop_user(command_dict)
        case "alter_user":
            client.alter_user(command_dict)
        case "create_user":
            client.create_user(command_dict)
        case "activate_user":
            client.activate_user(command_dict)
        case "list_datasets":
            client.list_datasets(command_dict)
        case "list_agents":
            client.list_agents(command_dict)
        case "create_role":
            client.create_role(command_dict)
        case "drop_role":
            client.drop_role(command_dict)
        case "alter_role":
            client.alter_role(command_dict)
        case "list_roles":
            client.list_roles(command_dict)
        case "show_role":
            client.show_role(command_dict)
        case "grant_permission":
            client.grant_permission(command_dict)
        case "revoke_permission":
            client.revoke_permission(command_dict)
        case "alter_user_role":
            client.alter_user_role(command_dict)
        case "show_user_permission":
            client.show_user_permission(command_dict)
        case "show_version":
            client.show_version(command_dict)
        case "grant_admin":
            client.grant_admin(command_dict)
        case "revoke_admin":
            client.revoke_admin(command_dict)
        case "set_variable":
            client.set_variable(command_dict)
        case "show_variable":
            client.show_variable(command_dict)
        case "list_variables":
            client.list_variables(command_dict)
        case "list_configs":
            client.list_configs(command_dict)
        case "list_environments":
            client.list_environments(command_dict)
        case "show_fingerprint":
            client.show_fingerprint(command_dict)
        case "set_license":
            client.set_license(command_dict)
        case "set_license_config":
            client.set_license_config(command_dict)
        case "show_license":
            client.show_license(command_dict)
        case "check_license":
            client.check_license(command_dict)
        case "generate_key":
            client.generate_key(command_dict)
        case "list_keys":
            client.list_keys(command_dict)
        case "drop_key":
            client.drop_key(command_dict)
        case "create_token":
            client.create_token(command_dict)
        case "list_tokens":
            client.list_tokens(command_dict)
        case "drop_token":
            client.drop_token(command_dict)
        case "get_chunk":
            client.get_chunk(command_dict)
        case "list_chunks":
            client.list_chunks(command_dict)
        case "list_user_datasets":
            client.list_user_datasets(command_dict)
        case "list_user_agents":
            client.list_user_agents(command_dict)
        case "list_user_chats":
            client.list_user_chats(command_dict)
        case "list_user_model_providers":
            client.list_user_model_providers(command_dict)
        case "list_user_default_models":
            client.list_user_default_models(command_dict)
        case "login_user":
            client.login_user(command_dict)
        case "ping_server":
            return client.ping_server(command_dict)
        case "register_user":
            client.register_user(command_dict)
        case "show_current_user":
            client.show_current_user(command_dict)
        case "create_model_provider":
            client.create_model_provider(command_dict)
        case "drop_model_provider":
            client.drop_model_provider(command_dict)
        case "set_default_model":
            client.set_default_model(command_dict)
        case "reset_default_model":
            client.reset_default_model(command_dict)
        case "create_user_dataset":
            client.create_user_dataset(command_dict)
        case "drop_user_dataset":
            client.drop_user_dataset(command_dict)
        case "list_user_dataset_files":
            client.list_user_dataset_files(command_dict)
        case "list_user_dataset_documents":
            client.list_user_dataset_documents(command_dict)
        case "list_user_datasets_metadata":
            client.list_user_datasets_metadata(command_dict)
        case "list_user_documents_metadata_summary":
            client.list_user_documents_metadata_summary(command_dict)
        case "create_user_chat":
            client.create_user_chat(command_dict)
        case "drop_user_chat":
            client.drop_user_chat(command_dict)
        case "create_index":
            client.create_index(command_dict)
        case "drop_index":
            client.drop_index(command_dict)
        case "create_doc_meta_index":
            client.create_doc_meta_index(command_dict)
        case "drop_doc_meta_index":
            client.drop_doc_meta_index(command_dict)
        case "create_chat_session":
            client.create_chat_session(command_dict)
        case "drop_chat_session":
            client.drop_chat_session(command_dict)
        case "list_chat_sessions":
            client.list_chat_sessions(command_dict)
        case "chat_on_session":
            client.chat_on_session(command_dict)
        case "import_docs_into_dataset":
            client.import_docs_into_dataset(command_dict)
        case "search_on_datasets":
            client.search_on_datasets(command_dict)
        case "parse_dataset_docs":
            client.parse_dataset_docs(command_dict)
        case "parse_dataset":
            client.parse_dataset(command_dict)
        case "benchmark":
            _run_benchmark(client, command_dict)
        case "meta":
            _handle_meta_command(command_dict)
        case _:
            print(f"Command '{command_type}' would be executed with API")


def _handle_meta_command(command: dict):
    meta_command = command["command"]
    args = command.get("args", [])

    if meta_command in ["?", "h", "help"]:
        show_help()
    elif meta_command in ["q", "quit", "exit"]:
        print("Goodbye!")
    else:
        print(f"Meta command '{meta_command}' with args {args}")


def _run_benchmark(client: MultiRAGClient, command_dict: dict):
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor, as_completed
    concurrency = command_dict.get("concurrency", 1)
    iterations = command_dict.get("iterations", 1)
    command = command_dict["command"]
    command["iterations"] = iterations
    command_type = command["type"]

    def _count_success(response_list):
        count = 0
        for resp in response_list:
            try:
                if command_type == "ping_server":
                    if resp.status_code == 200:
                        count += 1
                else:
                    rj = resp.json()
                    if resp.status_code == 200 and rj.get("code", rj.get("retcode", -1)) == 0:
                        count += 1
            except Exception:
                pass
        return count

    if concurrency == 1:
        result = run_command(client, command)
        if result and isinstance(result, dict) and "response_list" in result:
            success = _count_success(result["response_list"])
            total = iterations
            qps = total / result["duration"] if result["duration"] > 0 else None
            print(f"command: {command_type}, concurrency: {concurrency}, iterations: {iterations}")
            print(f"duration: {result['duration']:.4f}s, QPS: {qps:.2f}, SUCCESS: {success}, FAILURE: {total - success}")
    else:
        mp_context = mp.get_context("spawn")
        start_time = time.perf_counter()
        results = []
        with ProcessPoolExecutor(max_workers=concurrency, mp_context=mp_context) as executor:
            futures = [executor.submit(run_command, client, command) for _ in range(concurrency)]
            for f in as_completed(futures):
                results.append(f.result())
        total_duration = time.perf_counter() - start_time
        success = sum(_count_success(r["response_list"]) for r in results if r and "response_list" in r)
        total = iterations * concurrency
        qps = total / total_duration if total_duration > 0 else None
        print(f"command: {command_type}, concurrency: {concurrency}, iterations: {iterations}")
        print(f"duration: {total_duration:.4f}s, QPS: {qps:.2f}, SUCCESS: {success}, FAILURE: {total - success}")
