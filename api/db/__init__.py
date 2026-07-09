from enum import IntEnum, StrEnum


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
