
from ..element import Element


class AnalysisContext:
    """分析上下文，用于在分析过程中传递和存储上下文信息"""

    def __init__(self, elements: list[Element]):
        self.elements = elements
        self.current_index: int = -1

    def set_current_element_index(self, index: int) -> None:
        """设置当前正在处理的元素索引"""
        self.current_index = index

    def get_previous_element(self, offset: int = 1) -> Element | None:
        """获取前面的元素

        Args:
            offset: 向前偏移量，默认为1（即前一个元素）

        Returns:
            Optional[Element]: 如果存在则返回对应的元素，否则返回 None
        """
        target_index = self.current_index - offset
        if 0 <= target_index < len(self.elements):
            return self.elements[target_index]
        return None

    def get_next_element(self, offset: int = 1) -> Element | None:
        """获取后面的元素

        Args:
            offset: 向后偏移量，默认为1（即后一个元素）

        Returns:
            Optional[Element]: 如果存在则返回对应的元素，否则返回 None
        """
        target_index = self.current_index + offset
        if 0 <= target_index < len(self.elements):
            return self.elements[target_index]
        return None

    def get_surrounding_elements(self, window_size: int = 1) -> list[Element]:
        """获取当前元素周围的元素

        Args:
            window_size: 窗口大小，表示向前和向后各取多少个元素

        Returns:
            List[Element]: 周围元素的列表
        """
        start_index = max(0, self.current_index - window_size)
        end_index = min(len(self.elements), self.current_index + window_size + 1)
        return self.elements[start_index:end_index]
