import os
from agentscope.tool._base import ToolMiddlewareBase
from agentscope.message import TextBlock

class HostPathGuardMiddleware(ToolMiddlewareBase):
    """拦截 Bash 命令中的宿主机特征路径，提前以结构化错误返回。

    触发模式（任一匹配即拦截）：
    - `/home/...`（Linux 宿主家目录）
    - `~/<...>` 或 `~/`（任意 shell 展开到宿主机家目录）
    - `os.path.abspath(sandbox.host_workdir)` 本身或其下子路径
    """

    def __init__(self, host_workdir: str):
        import re
        self._host_root = os.path.abspath(host_workdir or ".")
        self._patterns: list[re.Pattern] = [
            re.compile(r"/home/[^/\s'\"]+"),
            re.compile(r"(^|[\s\"'])~/(?!/)"),
            re.compile(re.escape(self._host_root)),
        ]

    def _match(self, command: str) -> str | None:
        for pat in self._patterns:
            m = pat.search(command)
            if m:
                return m.group(0)
        return None

    async def on_tool_call(self, tool, input_kwargs, next_handler):
        from agentscope.tool._response import ToolChunk
        from agentscope.message import ToolResultState

        command = input_kwargs.get("command", "") or ""
        bad = self._match(command)
        if bad is not None:
            yield ToolChunk(
                content=[TextBlock(
                    type="text",
                    text=(
                        f"❌ 命令被宿主机路径守卫拦截：检测到 `{bad}` 这种宿主机路径，"
                        f"在沙箱内不存在。\n"
                        f"👉 改用相对路径或先在 cwd 下 `ls` / `find` 找正确位置，"
                        f"再用 `python <path>` 跑脚本。"
                    ),
                )],
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return
        async for chunk in next_handler(**input_kwargs):
            yield chunk