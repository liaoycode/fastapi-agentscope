# app/schemas/base.py
from pydantic import BaseModel, Field, computed_field
from typing import Generic, TypeVar, Optional

T = TypeVar('T')


from typing import Any, Generic, TypeVar, Optional
from datetime import datetime
from pydantic import BaseModel, Field, field_serializer

# T = TypeVar("T")

class ApiResponse(BaseModel, Generic[T]):
    code: int = Field(default=200)
    message: str = Field(default="success")
    data: Optional[T] = None

    @classmethod
    def ok(cls, data: T = None):
        return cls(data=data)

    @classmethod
    def error(cls, code: int = -1, message: str = "error"):
        return cls(code=code, message=message)

    model_config = {
        "arbitrary_types_allowed": True,
        "json_encoders": {
            datetime: lambda v: v.strftime("%Y-%m-%d %H:%M:%S")
        }
    }


class PageParams(BaseModel):
    """分页请求参数"""
    page: int = Field(default=1, ge=1, description="页码，从1开始")
    size: int = Field(default=10, ge=1, le=100, description="每页大小")

    @property
    def skip(self) -> int:
        return (self.page - 1) * self.size


class PageResult(BaseModel, Generic[T]):
    """通用分页响应模型"""
    items: list[T] = Field(..., description="数据列表")
    current_page: int = Field(..., ge=1, description="当前页码")
    page_size: int = Field(..., ge=1, description="每页大小")
    total_count: int = Field(..., ge=0, description="总记录数")

    # computed_field 会出现在 docs 响应结构里
    @computed_field(description="总页数")
    @property
    def total_pages(self) -> int:
        if self.page_size <= 0:
            return 0
        return (self.total_count + self.page_size - 1) // self.page_size

    @computed_field(description="是否有上一页")
    @property
    def has_prev(self) -> bool:
        return self.current_page > 1

    @computed_field(description="是否有下一页")
    @property
    def has_next(self) -> bool:
        return self.current_page < self.total_pages

    @computed_field(description="上一页页码")
    @property
    def prev_page(self) -> Optional[int]:
        return self.current_page - 1 if self.has_prev else None

    @computed_field(description="下一页页码")
    @property
    def next_page(self) -> Optional[int]:
        return self.current_page + 1 if self.has_next else None

    @classmethod
    def of(cls, items: list, total_count: int, page: PageParams) -> dict:
        """返回 dict 配合 ApiResponse.ok 使用"""
        return {
            "items": items,
            "total_count": total_count,
            "current_page": page.page,
            "page_size": page.size,
        }