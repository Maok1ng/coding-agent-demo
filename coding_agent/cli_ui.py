"""CLI 的提问卡片；只处理展示，不读取答案、不改变权限或会话状态。"""

import os
import shutil
import sys
import unicodedata


def _color_enabled():
    #管道、日志、NO_COLOR 和简易终端不输出 ANSI 颜色转义序列。
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"


def _paint(text, style, color):
    return f"\033[{style}m{text}\033[0m" if color else text


def _safe_text(text):
    #问题和选项可能来自模型；不能让其中的控制字符清屏、移动光标或伪造输入提示。
    return "".join(char if char.isprintable() or char == "\n" else " " for char in text)


def _width(text):
    #中文通常占两个终端列，组合音标不额外占列；len() 不能用于卡片右边框对齐。
    return sum(0 if unicodedata.combining(char) else
               2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
               for char in text)


def _wrap(text, width):
    for paragraph in _safe_text(text).split("\n"):
        line, size = "", 0
        for char in paragraph:
            columns = _width(char)
            if line and size + columns > width:
                yield line
                line, size = "", 0
            line += char
            size += columns
        yield line


def format_question(question, options=(), *, columns=None, color=None):
    """按当前终端宽度排版；纯文本降级仍保留问题、编号和自由输入提示。"""
    columns = shutil.get_terminal_size(fallback=(80, 24)).columns if columns is None else columns
    color = _color_enabled() if color is None else color
    width = min(88, max(2, columns))
    hint = "输入编号选择，也可以直接输入其他答案。" if options else "直接输入你的回答。"
    help_text = "Esc / Ctrl+C 取消 · Ctrl+D 退出"
    if width < 32:
        #窄终端不强行画边框；编号和操作说明仍能正常换行。
        lines = ["等待你的回答", "", question]
        lines.extend(f"[{number}] {option}" for number, option in enumerate(options, 1))
        lines.extend(["", hint, help_text])
        return "\n" + "\n".join(line for text in lines for line in _wrap(text, width))

    inner = width - 4
    title = " 等待你的回答 "
    border = lambda text: _paint(text, "36", color)
    lines = ["", border("╭─" + title + "─" * (width - _width(title) - 3) + "╮")]

    def row(text="", style=None):
        padding = " " * (inner - _width(text))
        content = _paint(text, style, color) if style else text
        lines.append(border("│") + " " + content + padding + " " + border("│"))

    row()
    for line in _wrap(question, inner):
        row(line, "1")
    if options:
        row()
        for number, option in enumerate(options, 1):
            #续行与选项正文对齐，避免长答案看起来像另一个选项。
            for index, line in enumerate(_wrap(option, inner - 5)):
                row((f"[{number}]  " if index == 0 else "     ") + line, "36")
    row()
    for text in (hint, help_text):
        for line in _wrap(text, inner):
            row(line, "2")
    lines.append(border("╰" + "─" * (width - 2) + "╯"))
    return "\n".join(lines)


def format_change_preview(path, diff, *, truncated=False, reference=None,
                          approval_action="confirm", columns=None, color=None):
    """展示宿主生成的候选 diff；这里只排版，展示和确认都不会自行写入文件。"""
    columns = shutil.get_terminal_size(fallback=(80, 24)).columns if columns is None else columns
    color = _color_enabled() if color is None else color
    width = min(88, max(2, columns))
    #字符数有界仍可能包含很多短行；限制显示行数，完整 diff 由宿主保存在引用中。
    diff_lines = diff.split("\n")
    truncated = truncated or len(diff_lines) > 120
    diff_lines = diff_lines[:120]
    footer = ("确认后才会写入以上修改。" if approval_action == "confirm" else
              "将按当前审批设置写入以上修改。")
    content = [(f"文件：{path}", "1"), ("", None)]
    for line in diff_lines:
        #文件头不是增删的代码行；保持独立颜色，方便检查修改前后的路径。
        style = ("36" if line.startswith(("--- ", "+++ ", "@@")) else
                 "32" if line.startswith("+") else "31" if line.startswith("-") else None)
        content.append((line, style))
    content.append(("", None))
    if truncated:
        content.append(("预览已截断；完整 diff 保存在本次运行记录中。", "33"))
        if reference:
            content.append((f"完整 diff 记录：{reference}", "2"))
    content.append((footer, "2"))
    title = " 修改预览 · 未写入 "
    if width < 32:
        lines = [""]
        lines.extend(_wrap(title.strip(), width))
        for text, style in content:
            lines.extend(_paint(line, style, color) if style else line
                         for line in _wrap(text, width))
        return "\n".join(lines)

    inner = width - 4
    border = lambda text: _paint(text, "36", color)
    lines = ["", border("╭─" + title + "─" * (width - _width(title) - 3) + "╮")]
    for text, style in content:
        for line in _wrap(text, inner):
            padding = " " * (inner - _width(line))
            shown = _paint(line, style, color) if style else line
            lines.append(border("│") + " " + shown + padding + " " + border("│"))
    lines.append(border("╰" + "─" * (width - 2) + "╯"))
    return "\n".join(lines)


def answer_prompt():
    return _paint("回答 › ", "1;36", _color_enabled())
