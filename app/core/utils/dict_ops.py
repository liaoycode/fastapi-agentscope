class DictOps:

    @staticmethod
    def rename_keys(data, field_map: dict):
        """递归将字典（或字典列表）的英文 key 替换为中文，字段不在映射表中保留原 key。"""
        if isinstance(data, list):
            return [DictOps.rename_keys(item, field_map) for item in data]
        return {field_map.get(k, k): v for k, v in data.items()}