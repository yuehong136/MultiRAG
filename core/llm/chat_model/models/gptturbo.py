from dataclasses import dataclass, field
from typing import Optional
from core.llm.chat_model.base import Base

@dataclass
class GptTurbo(Base):
    key: str
    model_name: str = "gpt-3.5-turbo"
    base_url: Optional[str] = "https://api.openai.com/v1"

    def __post_init__(self):
        if not self.base_url:
            self.base_url = "https://api.openai.com/v1"
        super().__post_init__()
