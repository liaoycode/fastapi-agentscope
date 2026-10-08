import asyncio
from typing import Any

from pydantic import BaseModel, Field

from agentscope.tool import FunctionTool, Toolkit, ToolBase, ToolChunk
from agentscope.message import TextBlock
from agentscope.permission import (
    PermissionContext,
    PermissionDecision,
    PermissionBehavior,
)


# ========== 1. 定义 Pydantic 输入模型，把约束写死 ==========
class TransferInput(BaseModel):
    """转账参数校验模型。"""

    from_account: str = Field(
        description="转出账户号，必须为纯数字字符串，例如 '6222020200112233445'",
        pattern=r"^\d+$",  # 强制纯数字
    )
    to_account: str = Field(
        description="转入账户号，必须为纯数字字符串，例如 '6222020200556677889'",
        pattern=r"^\d+$",
    )
    amount: float = Field(
        description="转账金额，单位为元，必须大于 0",
        gt=0,  # 必须大于 0
    )
    remark: str = Field(
        default="",
        description="转账备注，可选",
    )


# ========== 2. 继承 ToolBase，在 call 里做容错 ==========
class TransferTool(ToolBase):
    name = "transfer"
    description = "执行银行转账操作。转账前请确认账户号和金额准确无误。"
    input_schema = TransferInput.model_json_schema()  # 用 Pydantic 模型生成 schema
    is_concurrency_safe = False
    is_read_only = False

    def _build_confirm_text(self, tool_input: dict) -> str:
        """生成转账确认提示文案。check_permissions 和 confirmation_message 都调它,
        保证 engine 内部 message 和最终展示给用户的提示是同一份。"""
        return f"即将转账 {tool_input.get('amount')} 元，是否确认？"

    async def check_permissions(
        self, tool_input: dict, context: PermissionContext
    ) -> PermissionDecision:
        # 转账是写操作，要求用户确认
        return PermissionDecision(
            behavior=PermissionBehavior.ASK,
            message=self._build_confirm_text(tool_input),
        )

    def confirmation_message(self, tool_input: dict) -> str:
        """展示给用户的确认提示。chat_stream 拿到 RequireUserConfirmEvent
        后会按 tool name 找到本实例并调这个方法——文本和 check_permissions
        的 message 共用 _build_confirm_text,避免两处漂移。"""
        return self._build_confirm_text(tool_input)

    async def call(
        self,
        from_account: str,
        to_account: str,
        amount: float,
        remark: str = "",
    ) -> ToolChunk:
        # 二次容错：即使 schema 校验通过，也再手动检查一遍
        # 因为模型可能生成 "1000.00" 这种字符串形式的数字，被 schema 的强制转换处理了
        # 但如果你用的是严格模式，这里可能拿到的是字符串

        # 容错 1：账户号可能带了空格或连字符
        from_account = str(from_account).replace(" ", "").replace("-", "")
        to_account = str(to_account).replace(" ", "").replace("-", "")

        # 容错 2：金额可能是字符串（如果 schema 的强制转换没生效）
        try:
            amount = float(amount)
        except (ValueError, TypeError):
            return ToolChunk(
                content=[TextBlock(text=f"错误：金额 '{amount}' 不是有效数字。")]
            )

        # 容错 3：金额为负数或零
        if amount <= 0:
            return ToolChunk(
                content=[TextBlock(text=f"错误：转账金额必须大于 0，当前为 {amount}。")]
            )

        # 执行转账逻辑（此处为模拟）
        result = f"转账成功：从 {from_account} 转出 {amount} 元至 {to_account}"
        if remark:
            result += f"，备注：{remark}"

        print(result)

        return ToolChunk(content=[TextBlock(text=result)])


transfer = TransferTool()

