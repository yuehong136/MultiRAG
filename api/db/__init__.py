from enum import IntEnum, StrEnum


class UserAccountKind(StrEnum):
    """How a platform user can authenticate to MultiRAG."""

    LOCAL = "local"
    EXTERNAL = "external"
    HYBRID = "hybrid"


class ExternalIdentityState(StrEnum):
    """Lifecycle state for one tenant-scoped provider identity link."""

    PENDING_LINK = "pending_link"
    ACTIVE = "active"
    INACTIVE = "inactive"
    REVOKED = "revoked"
    CONFLICT = "conflict"


class ExternalIdentityAliasType(StrEnum):
    """Provider identifiers that may resolve to a canonical identity."""

    OPEN_ID = "open_id"
    UNION_ID = "union_id"


class EnterpriseSubjectType(StrEnum):
    """Supported enterprise business-identity namespaces."""

    EMPLOYEE_NO = "employee_no"
    TALENT_ID = "talent_id"
    WORKCODE = "workcode"


class EnterpriseSubjectState(StrEnum):
    """Lifecycle state for a verified enterprise subject link."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    CONFLICT = "conflict"


class IdentityEventReceiptState(StrEnum):
    """Durable processing state for a provider directory event receipt."""

    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class IdentityProviderHealthState(StrEnum):
    """Operational health of one server-owned provider installation."""

    PENDING = "pending"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    ERROR = "error"
    DISABLED = "disabled"


class UserTenantRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    NORMAL = "normal"
    INVITE = "invite"


class TenantPermission(StrEnum):
    ME = "me"
    TEAM = "team"


class SerializedType(IntEnum):
    PICKLE = 1
    JSON = 2


class FileType(StrEnum):
    PDF = "pdf"
    DOC = "doc"
    VISUAL = "visual"
    AURAL = "aural"
    VIRTUAL = "virtual"
    FOLDER = "folder"
    OTHER = "other"


VALID_FILE_TYPES = {FileType.PDF, FileType.DOC, FileType.VISUAL, FileType.AURAL, FileType.VIRTUAL, FileType.FOLDER, FileType.OTHER}


class ChatStyle(StrEnum):
    CREATIVE = "Creative"
    PRECISE = "Precise"
    EVENLY = "Evenly"
    CUSTOM = "Custom"


class InputType(StrEnum):
    LOAD_STATE = "load_state"  # e.g. loading a current full state or a save state, such as from a file
    POLL = "poll"  # e.g. calling an API to get all documents in the last hour
    EVENT = "event"  # e.g. registered an endpoint as a listener, and processing connector events
    SLIM_RETRIEVAL = "slim_retrieval"


class CanvasCategory(StrEnum):
    Agent = "agent_canvas"
    DataFlow = "dataflow_canvas"


VALID_CANVAS_CATEGORIES = {CanvasCategory.Agent, CanvasCategory.DataFlow}


KNOWLEDGEBASE_FOLDER_NAME = ".knowledgebase"
