import logging

from typing import Any

from api.common.exceptions import AdminException


class RoleMgr:
    @staticmethod
    def create_role(role_name: str, description: str):
        error_msg = f"not implement: create role: {role_name}, description: {description}"
        logging.error(error_msg)
        raise AdminException(error_msg)

    @staticmethod
    def update_role_description(role_name: str, description: str) -> dict[str, Any]:
        error_msg = f"not implement: update role: {role_name} with description: {description}"
        logging.error(error_msg)
        raise AdminException(error_msg)

    @staticmethod
    def delete_role(role_name: str) -> dict[str, Any]:
        error_msg = f"not implement: drop role: {role_name}"
        logging.error(error_msg)
        raise AdminException(error_msg)

    @staticmethod
    def list_roles() -> dict[str, Any]:
        error_msg = "not implement: list roles"
        logging.error(error_msg)
        raise AdminException(error_msg)

    @staticmethod
    def get_role_permission(role_name: str) -> dict[str, Any]:
        error_msg = f"not implement: show role {role_name}"
        logging.error(error_msg)
        raise AdminException(error_msg)

    @staticmethod
    def grant_role_permission(role_name: str, actions: list, resource: str) -> dict[str, Any]:
        error_msg = f"not implement: grant role {role_name} actions: {actions} on {resource}"
        logging.error(error_msg)
        raise AdminException(error_msg)

    @staticmethod
    def revoke_role_permission(role_name: str, actions: list, resource: str) -> dict[str, Any]:
        error_msg = f"not implement: revoke role {role_name} actions: {actions} on {resource}"
        logging.error(error_msg)
        raise AdminException(error_msg)

    @staticmethod
    def update_user_role(user_name: str, role_name: str) -> dict[str, Any]:
        error_msg = f"not implement: update user role: {user_name} to role {role_name}"
        logging.error(error_msg)
        raise AdminException(error_msg)

    @staticmethod
    def get_user_permission(user_name: str) -> dict[str, Any]:
        error_msg = f"not implement: get user permission: {user_name}"
        logging.error(error_msg)
        raise AdminException(error_msg)
