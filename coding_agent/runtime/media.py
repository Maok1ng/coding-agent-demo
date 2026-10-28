"""图片的宿主边界；只处理 MCP 已返回的字节，不读取路径或下载图片 URL。"""

import base64
import binascii
from io import BytesIO
import math
import warnings


#统一缩到标准视觉窗口；预算另留余量，不能把 Base64 字符数当视觉 token。
IMAGE_TOKEN_RESERVE = 2048
MAX_IMAGE_EDGE = 1568
MAX_IMAGE_PIXELS = 1_100_000
MAX_INPUT_PIXELS = 25_000_000
MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 3 * 1024 * 1024
MAX_REQUEST_IMAGES = 8
MAX_REQUEST_IMAGE_BYTES = 16 * 1024 * 1024  #按编码后的实际请求大小计数。
IMAGE_TYPES = {"image/png": "PNG", "image/jpeg": "JPEG",
               "image/gif": "GIF", "image/webp": "WEBP"}
IMAGE_SUFFIXES = {"image/png": ".png", "image/jpeg": ".jpg"}


class ImageCapabilityError(ValueError):
    """当前实际请求包含图片，但宿主尚未确认所选模型能接收图片。"""


def ensure_image_capability(messages: list, config: dict) -> None:
    """只检查协议内容块；文本、工具参数里的同名字段不算真正的图片输入。"""
    def contains_image(content):
        if not isinstance(content, list):
            return False
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "image":
                return True
            if block.get("type") == "tool_result" and contains_image(block.get("content")):
                return True
        return False

    if config.get("supports_images") is True or not any(
        isinstance(message, dict) and contains_image(message.get("content"))
        for message in messages
    ):
        return
    if config.get("supports_images") is False:
        reason = "当前角色所选模型已声明不支持图片"
    else:
        reason = "当前角色所选模型尚未声明图片能力"
    raise ImageCapabilityError(
        reason + "，本次含图片的请求未发送。请用 /models 为当前角色选择支持图片的模型档案，"
        "并由宿主配置 supports_images=true；添加档案可使用 --supports-images true。"
        "已有截图与历史会保留，不会静默丢图或自动更换模型。"
    )


def normalize_image(data: str, mime_type: str) -> dict:
    """校验实际图片并等比缩小；清掉元数据，动画只使用第一帧，不执行图内内容。"""
    if mime_type not in IMAGE_TYPES:
        raise ValueError("Unsupported MCP image format; use PNG, JPEG, GIF or WebP")
    if not isinstance(data, str) or len(data) > 4 * math.ceil(MAX_INPUT_BYTES / 3):
        raise ValueError("MCP image exceeds the input size limit")
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("MCP image has invalid base64 data") from None
    if not raw or len(raw) > MAX_INPUT_BYTES:
        raise ValueError("MCP image exceeds the input size limit or is empty")
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise ValueError("Image support requires Pillow; install the project requirements.txt") from None

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw), formats=list(IMAGE_TYPES.values())) as candidate:
                if candidate.format != IMAGE_TYPES[mime_type]:
                    raise ValueError("MCP image format does not match its MIME type")
                width, height = candidate.size
                if width <= 0 or height <= 0 or width * height > MAX_INPUT_PIXELS:
                    raise ValueError("MCP image exceeds the decoded pixel limit")
                candidate.verify()
            #verify 之后需要重新打开；限制像素后才解码，避免小文件膨胀成超大位图。
            with Image.open(BytesIO(raw), formats=list(IMAGE_TYPES.values())) as source:
                source.seek(0)
                image = ImageOps.exif_transpose(source)
                image = image.convert("RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB")
                scale = min(1, MAX_IMAGE_EDGE / max(image.size),
                            math.sqrt(MAX_IMAGE_PIXELS / (image.width * image.height)))
                width, height = max(1, int(image.width * scale)), max(1, int(image.height * scale))
                #兼容标准模型的 patch 上限，也兼容较早按面积计算的视觉模型。
                while math.ceil(width / 28) * math.ceil(height / 28) > 1568:
                    width, height = max(1, int(width * .99)), max(1, int(height * .99))
                image.thumbnail((width, height), Image.Resampling.LANCZOS)
                image.info.clear()
                buffer = BytesIO()
                image.save(buffer, format="PNG")
                payload, media_type = buffer.getvalue(), "image/png"
                if len(payload) > MAX_IMAGE_BYTES:
                    #高噪声 PNG 可能很大；必要时转 JPEG，而不是截断一份不能解码的图片。
                    if image.mode == "RGBA":
                        background = Image.new("RGB", image.size, "white")
                        background.paste(image, mask=image.getchannel("A"))
                        image = background
                    else:
                        image = image.convert("RGB")
                    buffer = BytesIO()
                    image.save(buffer, format="JPEG", quality=85)
                    payload, media_type = buffer.getvalue(), "image/jpeg"
                if len(payload) > MAX_IMAGE_BYTES:
                    raise ValueError("Normalized MCP image exceeds the output size limit")
    except ValueError:
        raise
    except Exception:
        #不输出解码器原文、图片内容或服务端携带的数据。
        raise ValueError("MCP image is invalid or cannot be decoded") from None
    return {"type": "image", "source": {"type": "base64", "media_type": media_type,
            "data": base64.b64encode(payload).decode("ascii")}}


def bound_history_images(messages: list) -> list:
    """优先保留最新截图；旧图已有磁盘引用，移出请求时明确标记，不能据占位推断画面。"""
    count, size = 0, 0

    def visit(value):
        nonlocal count, size
        if isinstance(value, dict):
            if value.get("type") == "image" and isinstance(value.get("source"), dict):
                source = value["source"]
                encoded = source.get("data", "")
                length = len(encoded) if isinstance(encoded, str) else 0
                if count < MAX_REQUEST_IMAGES and size + length <= MAX_REQUEST_IMAGE_BYTES:
                    count += 1
                    size += length
                    return value
                return {"type": "text", "text": (
                    "[Earlier image omitted to bound request size; consult its saved screenshot "
                    "reference or capture it again. This placeholder is not visual evidence.]"
                )}
            #倒序访问仅用于选择最新图片，返回对象仍保持原始消息和内容顺序。
            replaced = {key: visit(item) for key, item in reversed(list(value.items()))}
            return {key: replaced[key] for key in value}
        if isinstance(value, list):
            return list(reversed([visit(item) for item in reversed(value)]))
        return value

    return visit(messages)
