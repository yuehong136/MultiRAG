from dataclasses import dataclass
from typing import Optional


@dataclass
class Run:
    """段落中的文本运行"""
    text: str
    # 使用 Optional[bool] 并提供默认值
    bold: Optional[bool] = False
    italic: Optional[bool] = False
    underline: Optional[bool] = False
    font_name: Optional[str] = None
    font_size: Optional[float] = None

    def __post_init__(self):
        # 确保布尔值属性不为 None
        self.bold = False if self.bold is None else self.bold
        self.italic = False if self.italic is None else self.italic
        self.underline = False if self.underline is None else self.underline

    def to_dict(self) -> dict:
        return {
            'text': self.text,
            'bold': self.bold,
            'italic': self.italic,
            'underline': self.underline,
            'font_name': self.font_name,
            'font_size': self.font_size
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'Run':
        return cls(**data)
