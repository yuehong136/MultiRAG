from typing import List, Optional, Dict, Any
from .base import Element, ElementType
from .run import Run


class ParagraphElement(Element):
    """段落元素"""

    def __init__(
            self,
            content: str,
            style: str = 'Normal',
            alignment: Optional[int] = None,
            runs: Optional[List[Run]] = None
    ):
        super().__init__(ElementType.PARAGRAPH)
        self.content = content
        self.style = style
        self.alignment = alignment
        self.runs = runs or []

    def to_dict(self) -> Dict[str, Any]:
        return {
            'type': self.type.value,
            'content': self.content,
            'style': self.style,
            'alignment': self.alignment,
            'runs': [run.to_dict() for run in self.runs]
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ParagraphElement':
        runs = [Run.from_dict(run_data) for run_data in data.get('runs', [])]
        return cls(
            content=data['content'],
            style=data.get('style', 'Normal'),
            alignment=data.get('alignment'),
            runs=runs
        )
