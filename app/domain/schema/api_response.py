from typing import Any, Generic, TypeVar, Optional
from datetime import datetime
from pydantic import BaseModel, Field, field_serializer

T = TypeVar("T")

class ApiResponse(BaseModel, Generic[T]):
    code: int = Field(default=200)
    message: str = Field(default="success")
    data: Optional[T] = None

    model_config = {"arbitrary_types_allowed": True}

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

    # # 只对 data 字段做序列化，不覆盖整个对象
    # @field_serializer("data")
    # def serialize_data(self, value: Any) -> Any:
    #     return self._format(value)
    #
    # def _format(self, obj: Any) -> Any:
    #     if isinstance(obj, datetime):
    #         return obj.strftime("%Y-%m-%d %H:%M:%S")
    #     elif isinstance(obj, dict):
    #         return {k: self._format(v) for k, v in obj.items()}
    #     elif isinstance(obj, list):
    #         return [self._format(i) for i in obj]
    #     elif isinstance(obj, BaseModel):
    #         return self._format(obj.model_dump())
    #     return obj