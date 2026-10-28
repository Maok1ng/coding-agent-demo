"""上下文规模的统一估算：ASCII 按 3 字符/token，UTF-8 多字节按字节加罚。

对齐 freebuff 的 token-counter 思路：不本地 BPE 分词（一次长空格串就可能卡死进程），
粗估只用于触发压缩与预算判断；真实消耗以 API 响应的 usage 为准。
多字节加罚是有意保守：中文等密集文本宁可提前压缩，也不把超限留给 API 报错。
"""

import json

from coding_agent.runtime.media import IMAGE_TOKEN_RESERVE

CHARS_PER_TOKEN = 3


def text_tokens(text: str) -> int:
    """单段文本的保守 token 估算；空串为 0。"""
    if not text:
        return 0
    extra = len(text.encode("utf-8")) - len(text)
    return -(-len(text) // CHARS_PER_TOKEN) + extra


def omit_image_data(value):
    """摘要只保留图片存在这一事实；不复制编码数据，也不假装文字占位能描述画面。"""
    if isinstance(value, dict):
        if value.get("type") == "image" and isinstance(value.get("source"), dict):
            return {"type": "text", "text":
                    "[Historical image omitted from this text summary; "
                    "do not infer its visual contents. Full image is in the archived transcript.]"}
        return {key: omit_image_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [omit_image_data(item) for item in value]
    return value


def json_tokens(value) -> int:
    """序列化后整体估算；图片按视觉预算计费，base64 不是普通文字 token。"""
    images = 0

    def counted(item):
        nonlocal images
        if isinstance(item, dict):
            if item.get("type") == "image" and isinstance(item.get("source"), dict):
                images += 1
                #入口负责图片格式/尺寸校验；这里不解码、更不访问 URL，预算检查可以反复执行。
                source = item["source"]
                return {"type": "image", "source": {"type": source.get("type", "base64"),
                        "media_type": source.get("media_type", "image/unknown"), "data": "[image]"}}
            return {key: counted(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [counted(child) for child in item]
        return item

    serialized = json.dumps(counted(value), ensure_ascii=False, default=str)
    return text_tokens(serialized) + images * IMAGE_TOKEN_RESERVE
