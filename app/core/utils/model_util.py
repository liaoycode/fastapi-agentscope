from pydantic import BaseModel


class ModelUtil:

    @classmethod
    def get_field_map(model_cls: type[BaseModel]) -> dict:
        """从 Pydantic 模型字段定义中提取 英文字段名 -> 中文 description 的映射。"""
        return {
            name: (field.description or name)
            for name, field in model_cls.model_fields.items()
        }