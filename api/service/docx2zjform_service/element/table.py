from typing import List, Optional, Dict, Any
from .base import Element, ElementType


class TableElement(Element):
    """表格元素"""

    def __init__(
            self,
            content: List[List[str]],
            style: str = 'TableNormal',
            row_count: int = 0,
            column_count: int = 0,
            html: Optional[str] = None
    ):
        super().__init__(ElementType.TABLE)
        self.content = content
        self.style = style
        self.row_count = row_count or len(content)
        self.column_count = column_count or len(content[0]) if content else 0
        self.html = html

    def to_dict(self) -> Dict[str, Any]:
        return {
            'type': self.type.value,
            'content': self.content,
            'style': self.style,
            'row_count': self.row_count,
            'column_count': self.column_count,
            'html': self.html
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TableElement':
        return cls(
            content=data['content'],
            style=data.get('style', 'TableNormal'),
            row_count=data.get('row_count', 0),
            column_count=data.get('column_count', 0),
            html=data.get('html')
        )
