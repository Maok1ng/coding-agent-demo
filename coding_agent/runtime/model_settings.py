"""宿主模型设置：保存模型档案与任务绑定，不保存密钥，也不是模型可调用的工具。"""

from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
from urllib.parse import urlsplit
import uuid


MODEL_TASKS = (
    "orchestrator", "codebase", "planning", "implementation", "review_research",
    "memory", "goal_evaluator", "compaction",
)
PROFILE_FIELDS = {"model", "max_tokens", "context_token_limit", "base_url", "api_key_env", "supports_images"}
MAX_SETTINGS_BYTES = 128 * 1024
_ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,39}\Z")
_ENVIRONMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")


class ModelSettingsError(ValueError):
    """可向用户显示的宿主设置错误；不要夹带配置原文或环境变量的值。"""


def default_path():
    """默认目录属于当前用户，不能跟随目标工作区自动加载同名文件。"""
    return Path.home() / ".coding-agent" / "models.json"


def _alias(value):
    if not isinstance(value, str) or _ALIAS.fullmatch(value) is None:
        raise ModelSettingsError("模型别名需以英文字母开头，只含字母、数字、下划线和短横线，最多 40 字符")
    return value


def _endpoint(value):
    if (not isinstance(value, str) or len(value) > 2000 or value != value.strip() or "\\" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise ModelSettingsError("base_url 必须是 HTTP(S) API 地址")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ModelSettingsError("base_url 地址无效") from None
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or port == 0):
        raise ModelSettingsError("base_url 需为无认证信息、查询串和片段的 HTTP(S) API 地址")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ModelSettingsError("远程 base_url 必须使用 HTTPS；HTTP 只允许本机回环地址")
    return value.rstrip("/")


def _profile(value):
    if not isinstance(value, dict) or set(value) - PROFILE_FIELDS:
        raise ModelSettingsError("模型档案包含未知字段；密钥请使用 api_key_env 引用")
    result = dict(value)
    model = result.get("model")
    if (not isinstance(model, str) or not model.strip() or len(model) > 200
            or any(ord(character) < 32 or ord(character) == 127 for character in model)):
        raise ModelSettingsError("model 必须是非空模型 ID，最多 200 字符")
    result["model"] = model.strip()
    for name, minimum in (("max_tokens", 1), ("context_token_limit", 1000)):
        if name in result and (type(result[name]) is not int or result[name] < minimum):
            raise ModelSettingsError(f"{name} 必须是不小于 {minimum} 的整数")
    if "supports_images" in result and type(result["supports_images"]) is not bool:
        raise ModelSettingsError("supports_images 必须是 true 或 false；未声明时省略此字段")
    if "base_url" in result:
        result["base_url"] = _endpoint(result["base_url"])
    if "api_key_env" in result and (not isinstance(result["api_key_env"], str)
                                       or _ENVIRONMENT.fullmatch(result["api_key_env"]) is None):
        raise ModelSettingsError("api_key_env 必须是环境变量名称，不能填写实际密钥")
    if ("max_tokens" in result and "context_token_limit" in result
            and result["max_tokens"] >= result["context_token_limit"]):
        raise ModelSettingsError("max_tokens 必须小于 context_token_limit，还需为提示词和历史保留空间")
    return result


def _empty():
    return {"version": 1, "default": None, "profiles": {}, "bindings": {}}


class ModelSettings:
    def __init__(self, config, path=None, *, load=False):
        #普通内核/测试构造不隐式读取真实 home；CLI 明确选择系统配置或传入宿主路径。
        self.config = deepcopy(config)
        selected = path or config.get("models_config") or os.getenv("AGENT_MODELS_CONFIG")
        self.path = Path(os.path.abspath(Path(selected or default_path()).expanduser()))
        self.data = _empty()
        self._fingerprint = None
        if selected is not None or load:
            self.reload()

    @contextmanager
    def _parent(self, *, create=False):
        """从根目录逐层打开，不跟随途中被替换的链接；只有保存时创建所需目录。"""
        descriptor = os.open(self.path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in self.path.parent.parts[1:]:
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            yield descriptor
        finally:
            os.close(descriptor)

    def _read(self, parent):
        try:
            descriptor = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=parent)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SETTINGS_BYTES:
                raise ModelSettingsError("模型设置必须是大小不超过 128 KiB 的普通文件")
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = None
                raw = handle.read(MAX_SETTINGS_BYTES + 1)
            if len(raw) > MAX_SETTINGS_BYTES:
                raise ModelSettingsError("模型设置超过大小上限")
            return raw
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _validate(self, value):
        if (not isinstance(value, dict) or set(value) != {"version", "default", "profiles", "bindings"}
                or type(value["version"]) is not int or value["version"] != 1
                or not isinstance(value["profiles"], dict) or len(value["profiles"]) > 50
                or not isinstance(value["bindings"], dict)):
            raise ModelSettingsError("模型设置格式无效：需要 version、default、profiles、bindings")
        profiles = {_alias(name): _profile(profile) for name, profile in value["profiles"].items()}
        default = value["default"]
        if default is not None and (not isinstance(default, str) or default not in profiles):
            raise ModelSettingsError("默认模型引用了不存在的档案")
        for task, name in value["bindings"].items():
            if task not in MODEL_TASKS or not isinstance(name, str) or name not in profiles:
                raise ModelSettingsError("任务绑定引用了未知任务或不存在的模型档案")
        #全局档案可被不同宿主配置管理；与旧端点的凭据兼容性在真正选择档案时检查。
        return {"version": 1, "default": default, "profiles": profiles,
                "bindings": dict(value["bindings"])}

    def reload(self):
        """只在宿主显式加载时读取；文件不存在代表继续使用原模型配置。"""
        try:
            with self._parent() as parent:
                raw = self._read(parent)
        except FileNotFoundError:
            raw = None
        except OSError:
            raise ModelSettingsError("模型设置路径不安全或无法读取") from None
        try:
            data = _empty() if raw is None else self._validate(json.loads(raw))
        except (UnicodeError, json.JSONDecodeError, RecursionError):
            raise ModelSettingsError("模型设置不是有效 JSON") from None
        self.data = data
        self._fingerprint = hashlib.sha256(raw).digest() if raw is not None else None

    def snapshot(self):
        """为一个 Turn 冻结配置，不读取文件、不解析环境变量、不继承历史审批。"""
        #不用构造函数，避免 config/env 指定路径时再次读取磁盘。
        result = object.__new__(ModelSettings)
        result.config = deepcopy(self.config)
        result.path = self.path
        result.data = deepcopy(self.data)
        result._fingerprint = self._fingerprint
        return result

    def clone(self):
        return self.snapshot()

    def resolve(self, task, fallback=None):
        """显式任务绑定优先；摘要未绑定时可通过 fallback 继承当前角色配置。"""
        if task not in MODEL_TASKS:
            raise ModelSettingsError("未知模型任务")
        name = self.data["bindings"].get(task)
        #显式绑定选择一份完整档案；不能意外继承父角色档案的端点或 api_key_env。
        effective = deepcopy(self.config if fallback is None or name is not None else fallback)
        legacy = {"memory": "memory_model", "goal_evaluator": "goal_evaluator_model"}.get(task)
        if name is None and fallback is None:
            if legacy is not None and self.config.get(legacy):
                effective["model"] = self.config[legacy]
                if effective["model"] != self.config.get("model"):
                    effective.pop("supports_images", None)
            else:
                name = self.data["default"]
        if name is not None:
            profile = self.data["profiles"][name]
            self._check_endpoint(profile)
            effective.update(profile)
            #图片能力属于具体模型；切换档案时不能继承旧模型的 true 声明。
            effective["supports_images"] = profile.get("supports_images")
            if "api_key_env" in profile:
                #只回传变量名称，真正创建客户端时再解析；缺失变量不能退回旧 api_key。
                effective.pop("api_key", None)
        model = effective.get("model")
        if name is None and fallback is None and "supports_images" in self.config:
            _profile({"model": model, "supports_images": self.config["supports_images"]})
        selected = {"model": model,
                    "max_tokens": effective.get("max_tokens", 8000),
                    "context_token_limit": effective.get("context_token_limit", 16000),
                    "supports_images": effective.get("supports_images")}
        #继承旧配置时仍严格检查类型；预留挤满总窗口由执行内核报告预算阻塞。
        _profile({"model": model, "max_tokens": selected["max_tokens"]})
        _profile({"model": model, "context_token_limit": selected["context_token_limit"]})
        if selected["supports_images"] is not None:
            _profile({"model": model, "supports_images": selected["supports_images"]})
        effective.update(selected)
        effective.update(profile=name or effective.get("profile"), task=task)
        return effective

    def _check_endpoint(self, profile):
        previous = str(self.config.get("base_url") or "https://api.anthropic.com").rstrip("/")
        if profile.get("base_url", previous) != previous and not profile.get("api_key_env"):
            raise ModelSettingsError("不同 base_url 必须显式配置 api_key_env，不能继承旧端点密钥")

    def _save(self, value):
        data = self._validate(value)
        raw = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if len(raw) > MAX_SETTINGS_BYTES:
            raise ModelSettingsError("模型设置超过大小上限")
        try:
            with self._parent(create=True) as parent:
                #所有本程序写入者使用同一私有锁；保存前比对快照，拒绝覆盖外部更新。
                lock = os.open(f".{self.path.name}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                               0o600, dir_fd=parent)
                try:
                    if not stat.S_ISREG(os.fstat(lock).st_mode):
                        raise ModelSettingsError("模型设置锁文件不安全")
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    current = self._read(parent)
                    fingerprint = hashlib.sha256(current).digest() if current is not None else None
                    if fingerprint != self._fingerprint:
                        raise ModelSettingsError("模型设置已被其他进程修改；请重启或重新加载后再保存")
                    temporary = f".{self.path.name}.{uuid.uuid4().hex}.tmp"
                    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                         0o600, dir_fd=parent)
                    try:
                        with os.fdopen(descriptor, "wb") as handle:
                            handle.write(raw)
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.replace(temporary, self.path.name, src_dir_fd=parent, dst_dir_fd=parent)
                        os.fsync(parent)
                    finally:
                        try:
                            os.unlink(temporary, dir_fd=parent)
                        except FileNotFoundError:
                            pass
                finally:
                    os.close(lock)
        except OSError:
            raise ModelSettingsError("模型设置路径不安全或无法保存") from None
        #原子替换成功之后才替换内存，失败不能让 CLI 显示一个尚未持久化的配置。
        self.data = data
        self._fingerprint = hashlib.sha256(raw).digest()

    def describe(self):
        """只显示非敏感摘要；不显示 API 地址、环境变量值或旧配置的 api_key。"""
        lines = [f"系统模型设置：{self.path}",
                 f"默认档案：{self.data['default'] or '原模型配置'}"]
        for name, profile in self.data["profiles"].items():
            output = profile.get("max_tokens", "继承")
            context = profile.get("context_token_limit", "继承")
            images = {True: "支持", False: "不支持", None: "未声明"}[profile.get("supports_images")]
            lines.append(f"  {name}: {profile['model']} | 输出 {output} | 上下文 {context} | 图片 {images}")
        if not self.data["profiles"]:
            lines.append("  尚无模型档案；现有模型配置继续生效。")
        lines.append("任务绑定：")
        for task in MODEL_TASKS:
            name = self.data["bindings"].get(task)
            lines.append(f"  {task}: {name or ('继承所属角色' if task == 'compaction' else '默认/旧配置')}")
        lines.append("/models help 查看管理命令；更改从下一轮任务生效。")
        return "\n".join(lines)

    def command(self, argument):
        """仅由真实用户的 CLI 命令入口调用；没有模型工具、历史重放或自动建档入口。"""
        try:
            parts = shlex.split(argument)
        except ValueError:
            raise ModelSettingsError("模型命令的引号不完整") from None
        if not parts or parts == ["list"]:
            return self.describe()
        if parts == ["help"]:
            return (
                "/models                       查看档案和任务绑定\n"
                "/models add 别名 模型ID [--max-tokens N --context-limit N]\n"
                "    [--base-url HTTPS地址 --api-key-env 环境变量名 --supports-images true|false]\n"
                "/models use 别名               设置系统默认模型\n"
                "/models assign 任务 别名        为指定任务选择模型\n"
                "/models reset [任务]            解除一个绑定；不带任务时解除默认及全部绑定\n"
                "/models remove 别名             删除未被引用的档案\n"
                "任务：" + ", ".join(MODEL_TASKS) +
                "\n档案只保存环境变量名称，不填写或显示实际 API 密钥。"
            )
        data = deepcopy(self.data)
        action, arguments = parts[0], parts[1:]
        if action == "add":
            if len(arguments) < 2:
                raise ModelSettingsError("用法：/models add <别名> <模型ID> [选项]")
            name, model = _alias(arguments[0]), arguments[1]
            if name in data["profiles"]:
                raise ModelSettingsError("档案已存在；请使用新别名，或解除引用后删除旧档案")
            fields = {"--max-tokens": "max_tokens", "--context-limit": "context_token_limit",
                      "--base-url": "base_url", "--api-key-env": "api_key_env",
                      "--supports-images": "supports_images"}
            options = arguments[2:]
            if len(options) % 2:
                raise ModelSettingsError("每个模型选项都需要一个值；使用 /models help 查看用法")
            profile = {"model": model}
            for option, value in zip(options[::2], options[1::2]):
                key = fields.get(option)
                if key is None or key in profile:
                    raise ModelSettingsError("模型选项未知或重复；使用 /models help 查看用法")
                if key in {"max_tokens", "context_token_limit"}:
                    if re.fullmatch(r"[0-9]+", value) is None or len(value) > 10:
                        raise ModelSettingsError(f"{key} 必须是整数")
                    value = int(value)
                elif key == "supports_images":
                    if value not in {"true", "false"}:
                        raise ModelSettingsError("--supports-images 只接受 true 或 false")
                    value = value == "true"
                profile[key] = value
            profile = _profile(profile)
            self._check_endpoint(profile)
            data["profiles"][name] = profile
            message = f"已添加模型档案 {name}。"
        elif action == "use" and len(arguments) == 1:
            name = arguments[0]
            if name not in data["profiles"]:
                raise ModelSettingsError("模型档案不存在")
            data["default"] = name
            message = f"系统默认模型已设为 {name}。"
        elif action == "assign" and len(arguments) == 2:
            task, name = arguments
            if task not in MODEL_TASKS or name not in data["profiles"]:
                raise ModelSettingsError("任务或模型档案不存在；使用 /models help 查看任务名称")
            data["bindings"][task] = name
            message = f"任务 {task} 已绑定到 {name}。"
        elif action == "reset" and len(arguments) <= 1:
            if arguments:
                if arguments[0] not in MODEL_TASKS:
                    raise ModelSettingsError("未知模型任务")
                data["bindings"].pop(arguments[0], None)
                message = f"已解除 {arguments[0]} 的专用模型绑定。"
            else:
                data["default"], data["bindings"] = None, {}
                message = "已解除系统默认模型和全部任务绑定；保留模型档案。"
        elif action == "remove" and len(arguments) == 1:
            name = arguments[0]
            if name not in data["profiles"]:
                raise ModelSettingsError("模型档案不存在")
            if data["default"] == name or name in data["bindings"].values():
                raise ModelSettingsError("该档案仍被默认设置或任务引用；请先解除引用")
            del data["profiles"][name]
            message = f"已删除模型档案 {name}。"
        else:
            raise ModelSettingsError("模型命令无效；使用 /models help 查看用法")
        self._save(data)
        return message + "更改从下一轮任务生效。"
