from dataclasses import dataclass
from core.llm.chat_model.base import Base

@dataclass
class GptTurbo(Base):
    key: str
    model_name: str = "gpt-4o-mini"
    base_url: str | None = "https://api.openai.com/v1"

    def __post_init__(self):
        if not self.base_url:
            self.base_url = "https://api.openai.com/v1"
        super().__post_init__()
