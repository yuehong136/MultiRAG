# logging_config.py
import logging
import logging.handlers
import os


class WorkflowLogger:
    """工作流日志管理类"""

    def __init__(self,
                 log_dir: str = "logs",
                 log_level: int = logging.INFO,
                 max_bytes: int = 10 * 1024 * 1024,  # 10MB
                 backup_count: int = 5):
        self.log_dir = log_dir
        self.log_level = log_level
        self.max_bytes = max_bytes
        self.backup_count = backup_count

        # 创建日志目录
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)

        # 创建logger
        self.logger = logging.getLogger("workflow")
        self.logger.setLevel(log_level)

        # 避免重复添加handler
        if not self.logger.handlers:
            self._setup_handlers()

    def _setup_handlers(self):
        """设置日志处理器"""
        # 1. 文件处理器 - 所有日志
        all_handler = logging.handlers.RotatingFileHandler(
            filename=os.path.join(self.log_dir, "workflow.log"),
            maxBytes=self.max_bytes,
            backupCount=self.backup_count,
            encoding='utf-8'
        )
        all_handler.setLevel(self.log_level)
        all_handler.setFormatter(self._get_formatter())
        self.logger.addHandler(all_handler)

        # 2. 文件处理器 - 只记录错误
        error_handler = logging.handlers.RotatingFileHandler(
            filename=os.path.join(self.log_dir, "workflow_error.log"),
            maxBytes=self.max_bytes,
            backupCount=self.backup_count,
            encoding='utf-8'
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(self._get_formatter())
        self.logger.addHandler(error_handler)

        # 3. 控制台处理器
        console_handler = logging.StreamHandler()
        console_handler.setLevel(self.log_level)
        console_handler.setFormatter(self._get_formatter())
        self.logger.addHandler(console_handler)

    def _get_formatter(self) -> logging.Formatter:
        """获取日志格式器"""
        return logging.Formatter(
            '[%(asctime)s] %(levelname)s [%(name)s]'
            '%(message)s'
        )


class WorkflowContextLogger:
    """工作流上下文日志记录器，用于记录指定工作流实例的日志"""

    def __init__(self, workflow_id: str, base_logger: logging.Logger):
        self.workflow_id = workflow_id
        self.logger = base_logger

    def _format_message(self, message: str) -> str:
        return f"[Workflow-{self.workflow_id}] {message}"

    def debug(self, message: str, *args, **kwargs):
        self.logger.debug(self._format_message(message), *args, **kwargs)

    def info(self, message: str, *args, **kwargs):
        self.logger.info(self._format_message(message), *args, **kwargs)

    def warning(self, message: str, *args, **kwargs):
        self.logger.warning(self._format_message(message), *args, **kwargs)

    def error(self, message: str, *args, **kwargs):
        self.logger.error(self._format_message(message), *args, **kwargs)

    def critical(self, message: str, *args, **kwargs):
        self.logger.critical(self._format_message(message), *args, **kwargs)


class NodeLogger:
    """节点级别的日志记录器"""

    def __init__(self, workflow_logger: WorkflowContextLogger, node):
        self.workflow_logger = workflow_logger
        self.node = node

    def _format_message(self, message: str) -> str:
        return f"[Node-{self.node.id}:{self.node.title}] {message}"

    def debug(self, message: str, *args, **kwargs):
        self.workflow_logger.debug(self._format_message(message), *args, **kwargs)

    def info(self, message: str, *args, **kwargs):
        self.workflow_logger.info(self._format_message(message), *args, **kwargs)

    def warning(self, message: str, *args, **kwargs):
        self.workflow_logger.warning(self._format_message(message), *args, **kwargs)

    def error(self, message: str, *args, **kwargs):
        self.workflow_logger.error(self._format_message(message), *args, **kwargs)


class ComponentLogger:
    """组件级别的日志记录器"""

    def __init__(self, workflow_logger: WorkflowContextLogger, component):
        self.workflow_logger = workflow_logger
        self.component = component

    def _format_message(self, message: str) -> str:
        return f"[Component-{self.component.id}:{self.component.title}] {message}"

    def debug(self, message: str, *args, **kwargs):
        self.workflow_logger.debug(self._format_message(message), *args, **kwargs)

    def info(self, message: str, *args, **kwargs):
        self.workflow_logger.info(self._format_message(message), *args, **kwargs)

    def warning(self, message: str, *args, **kwargs):
        self.workflow_logger.warning(self._format_message(message), *args, **kwargs)

    def error(self, message: str, *args, **kwargs):
        self.workflow_logger.error(self._format_message(message), *args, **kwargs)
