"""自动收集本包下的 agent 工具。

新增工具:在本目录里新建一个不以 `_` 开头的 .py 文件,两种风格都行:

  函数风格 —— 简单工具,schema/描述从签名注解和 docstring 自动抽
    from app.infrastructure.tools import tool

    @tool
    def my_func(
        message: Annotated[str, Field(description="...")],
    ) -> str:
        '''描述'''
        ...

  类风格 —— 需要自定义权限、复杂逻辑时
    from agentscope.tool import ToolBase

    class MyTool(ToolBase):
        name = "my_tool"
        ...

    my_tool = MyTool()   # 模块级实例,自动被收

任意模块级 `ToolBase` 实例都会被 import 时挑出,放进 `ALL_TOOLS`,
直接传给 `Toolkit(tools=...)` 即可。
"""
from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable

from agentscope.tool import FunctionTool, ToolBase


def tool(func: Callable) -> FunctionTool:
    """装饰器:把函数包成 `FunctionTool`,实例留在模块级,scanner 自动收。"""
    return FunctionTool(func)


def _collect() -> list[ToolBase]:
    """import 每个公开子模块,挑出模块级的 `ToolBase` 实例。"""
    found: list[ToolBase] = []
    for module_info in sorted(
        pkgutil.iter_modules(__path__),
        key=lambda m: m.name,
    ):
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{module_info.name}")
        for attr_name in sorted(dir(module)):
            attr = getattr(module, attr_name)
            if isinstance(attr, ToolBase) and attr is not ToolBase:
                found.append(attr)
    return found


ALL_TOOLS: list[ToolBase] = _collect()


__all__ = ["ALL_TOOLS", "tool"]