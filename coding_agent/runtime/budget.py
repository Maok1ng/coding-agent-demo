"""单次模型请求的预算；与供应商无关，只使用项目现有的保守 token 估算。"""

from dataclasses import dataclass

from coding_agent.runtime.model import cached_arguments
from coding_agent.runtime.tokens import json_tokens


class ContextBudgetError(ValueError):
    """在发请求之前发现预算不足；不能靠丢掉当前请求来继续。"""


@dataclass(frozen=True)
class RequestBudget:
    total_limit: int
    fixed_tokens: int
    output_tokens: int
    protocol_tokens: int
    history_limit: int
    soft_history_limit: int

    @classmethod
    def from_request(cls, total_limit: int, system, tools, max_output_tokens: int):
        #bool 也是 int 的子类，所以用 type(...) is int，不接受 True 或字符串转换。
        if type(total_limit) is not int or total_limit < 1000:
            raise ValueError("context_token_limit 必须是不小于 1000 的整数")
        if type(max_output_tokens) is not int or max_output_tokens <= 0:
            raise ValueError("max_tokens 必须是大于 0 的整数")
        fixed = json_tokens(cached_arguments({"system": system, "tools": tools}))
        #消息封装/字段等协议开销预留；不是精确 tokenizer，也不是供应商容量承诺。
        protocol = 128
        history = total_limit - fixed - max_output_tokens - protocol
        budget = cls(total_limit, fixed, max_output_tokens, protocol, history,
                     int(total_limit * 0.8) - fixed - max_output_tokens - protocol)
        if history <= 0:
            raise ContextBudgetError("系统提示词、工具和输出预留已占满上下文预算；" + budget.describe())
        return budget

    @staticmethod
    def history_tokens(messages: list) -> int:
        """展开实际文本块/缓存标记；json_tokens 为图片预留视觉预算，不把 base64 当文字。"""
        return json_tokens(cached_arguments({"messages": messages})["messages"])

    def describe(self, history_tokens: int | None = None) -> str:
        """供 CLI/运行事件展示；字段也可由 dataclasses.asdict() 直接保存。"""
        details = (f"context_limit={self.total_limit}, fixed_tokens={self.fixed_tokens}, "
                   f"output_reserve={self.output_tokens}, protocol_reserve={self.protocol_tokens}, "
                   f"history_budget={self.history_limit}")
        return details if history_tokens is None else f"{details}, history_tokens={history_tokens}"
