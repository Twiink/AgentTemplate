"""通用响应结构。"""

# 不使用延迟注解：PEP 695 的类型参数 T 需要在类作用域内直接求值，pydantic 才能识别泛型字段。
from pydantic import BaseModel


class Page[T](BaseModel):
    """列表响应；items 按创建时间倒序排列。"""

    items: list[T]
