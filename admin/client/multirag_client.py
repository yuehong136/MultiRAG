#
#  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
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

import urllib.parse
from typing import Any

from http_client import HttpClient
from lark import Tree


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
        from multirag_cli import encrypt
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
        from multirag_cli import encrypt
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

    def list_user_datasets(self, command: dict):
        if self.server_type != "user":
            print("This command is only allowed in USER mode")
            return
        response = self.http_client.request("POST", "kb/list", json_body={}, use_api_base=False, auth_kind="web")
        res_json = response.json()
        if response.status_code == 200:
            self._print_table_simple(res_json.get("data", []))
        else:
            print(f"Fail to list datasets, code: {res_json.get('retcode')}, message: {res_json.get('retmsg')}")

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
        response = self.http_client.request("POST", "dialog/next", json_body={}, use_api_base=False, auth_kind="web")
        res_json = response.json()
        if response.status_code == 200:
            data = res_json.get("data", {})
            self._print_table_simple(data.get("dialogs", []))
        else:
            print(f"Fail to list chats, code: {res_json.get('retcode')}, message: {res_json.get('retmsg')}")

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
        case "generate_key":
            client.generate_key(command_dict)
        case "list_keys":
            client.list_keys(command_dict)
        case "drop_key":
            client.drop_key(command_dict)
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
