# exceptions.py
from enum import Enum
from typing import Optional, Dict, Any


class ErrorCode(Enum):
    """错误码枚举"""
    UNKNOWN_ERROR = 1000
    TIMEOUT_ERROR = 1001
    INVALID_INPUT = 1002
    NODE_NOT_FOUND = 1003
    DEPENDENCY_ERROR = 1004
    LLM_SERVICE_ERROR = 1005
    GRAPH_CYCLE_ERROR = 1006
    VALIDATION_ERROR = 1007


class WorkflowError(Exception):
    """工作流基础异常类"""

    def __init__(self,
                 message: str,
                 error_code: ErrorCode = ErrorCode.UNKNOWN_ERROR,
                 node_id: Optional[str] = None,
                 node_title: Optional[str] = None,
                 details: Optional[Dict[str, Any]] = None):
        self.message = message
        self.error_code = error_code
        self.node_id = node_id
        self.node_title = node_title
        self.details = details or {}
        super().__init__(message)


class NodeError(WorkflowError):
    """节点相关的基础异常类"""

    def __init__(self,
                 node_id: str,
                 node_title: str,
                 message: str,
                 error_code: ErrorCode = ErrorCode.UNKNOWN_ERROR,
                 details: Optional[Dict[str, Any]] = None):
        super().__init__(
            message=message,
            error_code=error_code,
            node_id=node_id,
            node_title=node_title,
            details=details
        )


class NodeExecutionError(NodeError):
    """节点执行异常"""

    def __init__(self, node_id: str, node_title: str, message: str, details: Optional[Dict[str, Any]] = None,
                 workflow_exe_data: Optional[Dict[str, Any]] = None):
        super().__init__(
            node_id=node_id,
            node_title=node_title,
            message=f"node: {node_id} {node_title} execution failed: {message}",
            error_code=ErrorCode.UNKNOWN_ERROR,
            details=details
        )
        self.workflow_exe_data = workflow_exe_data


class NodeTimeoutError(NodeError):
    """节点执行超时异常"""

    def __init__(self, node_id: str, node_title: str, timeout: int, details: Optional[Dict[str, Any]] = None):
        super().__init__(
            node_id=node_id,
            node_title=node_title,
            message=f"execution timed out after {timeout} seconds",
            error_code=ErrorCode.TIMEOUT_ERROR,
            details=details
        )


class NodeInputError(NodeError):
    """节点输入参数异常"""

    def __init__(self, node_id: str, node_title: str, param_name: str, message: str):
        super().__init__(
            node_id=node_id,
            node_title=node_title,
            message=f"invalid input parameter '{param_name}': {message}",
            error_code=ErrorCode.INVALID_INPUT,
            details={'parameter': param_name}
        )


class DependencyError(NodeError):
    """节点依赖异常"""

    def __init__(self, node_id: str, node_title: str, dep_node_id: str, message: str):
        super().__init__(
            node_id=node_id,
            node_title=node_title,
            message=f"dependency error with node {dep_node_id}: {message}",
            error_code=ErrorCode.DEPENDENCY_ERROR,
            details={'dependent_node': dep_node_id}
        )


class LLMServiceError(NodeError):
    """LLM服务调用异常"""

    def __init__(self, node_id: str, node_title: str, service_name: str, message: str):
        super().__init__(
            node_id=node_id,
            node_title=node_title,
            message=f"LLM service '{service_name}' error: {message}",
            error_code=ErrorCode.LLM_SERVICE_ERROR,
            details={'service_name': service_name}
        )


class WorkflowValidationError(WorkflowError):
    """工作流验证异常"""

    def __init__(self, message: str):
        self.status = "error"
        self.message = message


class CyclicDependencyError(WorkflowError):
    """工作流环形依赖异常"""

    def __init__(self, cycle_nodes: list):
        super().__init__(
            message=f"Cyclic dependency detected: {' -> '.join(cycle_nodes)}",
            error_code=ErrorCode.GRAPH_CYCLE_ERROR,
            details={'cycle_nodes': cycle_nodes}
        )
