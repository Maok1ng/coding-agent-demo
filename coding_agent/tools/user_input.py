"""显式提问与回答交付；只有终端的真实用户输入可以解除等待。"""

import json
from dataclasses import dataclass


ASK_USER_TOOL = {
    "name": "ask_user",
    "description": (
        "Ask the human a question and wait for their reply. Use when missing information, "
        "a consequential preference, or authorization requires the user's answer. "
        "Ask one self-contained question per response. Do not call other tools in the "
        "same response. Options are suggestions; the user may answer freely."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "minLength": 1, "maxLength": 2000},
            "options": {"type": "array", "maxItems": 6,
                        "items": {"type": "string", "minLength": 1, "maxLength": 200}},
        },
        "required": ["question"],
        "additionalProperties": False,
    },
}


def validate_question(question, options=None):
    if not isinstance(question, str) or not question.strip() or len(question) > 2000:
        raise ValueError("question 必须是 1 到 2000 字符的非空字符串")
    options = [] if options is None else options
    if not isinstance(options, list) or len(options) > 6:
        raise ValueError("options 必须是最多 6 项的列表")
    if any(not isinstance(option, str) or not option.strip() or len(option) > 200
           for option in options):
        raise ValueError("每个 option 必须是 1 到 200 字符的非空字符串")
    return question.strip(), tuple(option.strip() for option in options)


@dataclass(frozen=True)
class PendingQuestion:
    question: str
    options: tuple[str, ...] = ()
    tool_use_id: str | None = None


class UserInput:
    def __init__(self):
        self.pending: PendingQuestion | None = None

    def ask(self, question, options=None, tool_use_id=None):
        question, options = validate_question(question, options)
        if self.pending is not None:
            raise ValueError("已有问题等待用户回答")
        self.pending = PendingQuestion(question, options, tool_use_id)
        return json.dumps({"status": "waiting_user", "question": question,
                           "options": list(options)}, ensure_ascii=False)

    def _update_tool_result(self, messages, status, answer=None):
        if self.pending is None or self.pending.tool_use_id is None:
            return
        for message in reversed(messages):
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if (isinstance(block, dict) and block.get("type") == "tool_result"
                        and block.get("tool_use_id") == self.pending.tool_use_id):
                    payload = {"status": status, "question": self.pending.question}
                    if answer is not None:
                        payload["answer"] = answer
                    block["content"] = json.dumps(payload, ensure_ascii=False)
                    return
        raise ValueError("找不到待回答问题的 tool_result")

    def validate_answer(self, text):
        """纯校验并解析答案；CLI 可提前提示错误，但这里不清除问题或修改历史。"""
        if self.pending is None:
            return text
        if not text.strip():
            raise ValueError("回答不能为空")
        #数字仅在提供选项时表示选项编号；其他文本照原样交给 worker。
        index = text.strip()
        if index.isascii() and index.isdigit() and self.pending.options:
            number = int(index)
            if not 1 <= number <= len(self.pending.options):
                raise ValueError("选项编号超出范围，请选择有效编号或直接输入文字")
            text = self.pending.options[number - 1]
        return text

    def answer(self, messages, text):
        if self.pending is None:
            return text
        text = self.validate_answer(text)
        self._update_tool_result(messages, "answered", text)
        self.pending = None
        return text

    def cancel(self, messages):
        self._update_tool_result(messages, "cancelled")
        self.pending = None

    def render(self):
        if self.pending is None:
            return ""
        lines = [f"[等待你的回答] {self.pending.question}"]
        lines.extend(f"  {i}. {option}" for i, option in enumerate(self.pending.options, 1))
        if self.pending.options:
            lines.append("输入选项编号或直接输入文字。")
        return "\n".join(lines)
