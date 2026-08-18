# utils/serializer_util.py - 最终生产方案
from typing import Type, TypeVar, List, Dict, Any, Optional
from sqlalchemy.orm import class_mapper
from datetime import datetime

from app.core.common.logger import getLogger

logger = getLogger()
T = TypeVar('T')


class SerializerUtil:
    """全自动序列化器 - 生产环境使用"""

    @staticmethod
    def serialize(obj, exclude: Optional[List[str]] = None) -> Dict[str, Any]:
        """单个对象序列化"""
        exclude = exclude or []
        result = {}
        for column in class_mapper(obj.__class__).columns:
            if column.key in exclude:
                continue
            value = getattr(obj, column.key)
            if isinstance(value, datetime):
                value = value.isoformat()
            result[column.key] = value
        return result

    @staticmethod
    def serialize_list(objs, exclude: Optional[List[str]] = None) -> List[Dict]:
        """列表序列化"""
        return [SerializerUtil.serialize(obj, exclude) for obj in objs]

    @staticmethod
    def deserialize(cls: Type[T], data: Dict[str, Any]) -> T:
        """反序列化单个"""
        return cls(**data)

    @staticmethod
    def deserialize_list(cls: Type[T], data_list: List[Dict[str, Any]]) -> List[T]:
        """反序列化列表"""
        return [cls(**data) for data in data_list]
