"""按宿主任务选择模型和连接；模型档案不改变工具权限，也不保存真实凭据。"""

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import os

from anthropic import Anthropic

from coding_agent.runtime.budget import ContextBudgetError, RequestBudget
from coding_agent.runtime.model_settings import ModelSettingsError


@dataclass(frozen=True)
class ModelSelection:
    #配置可能保留旧入口的 api_key；禁止默认 repr 把它带进日志或异常展示。
    client: object = field(repr=False)
    config: dict = field(repr=False)

    @property
    def profile(self):
        return self.config.get("profile")


class ModelRouter:
    """同步宿主连接按端点/凭据复用；异步连接由 model.py 按 Turn 和宿主连接隔离。"""

    def __init__(self, client, config, settings=None):
        self.client = client
        self.config = deepcopy(config)
        self.settings = settings
        self._clients = {}

    def select(self, task, fallback=None):
        if self.settings is not None:
            effective = self.settings.resolve(task, fallback=fallback)
        else:
            if (fallback is None and "supports_images" in self.config
                    and type(self.config["supports_images"]) is not bool):
                raise ModelSettingsError("supports_images 必须是 true 或 false；未声明时省略此字段")
            effective = deepcopy(self.config if fallback is None else fallback)
            legacy = {"memory": "memory_model", "goal_evaluator": "goal_evaluator_model"}.get(task)
            if fallback is None and legacy and self.config.get(legacy):
                effective["model"] = self.config[legacy]
                if effective["model"] != self.config.get("model"):
                    effective.pop("supports_images", None)
            effective.setdefault("max_tokens", 8000)
            effective.setdefault("context_token_limit", 16000)
            effective.update(task=task)
        if not isinstance(effective.get("model"), str) or not effective["model"].strip():
            raise ModelSettingsError("尚未配置有效模型，请使用 /models 添加并选择默认档案")
        if type(effective["max_tokens"]) is not int or effective["max_tokens"] < 1:
            raise ModelSettingsError("max_tokens 必须是大于 0 的整数")
        if type(effective["context_token_limit"]) is not int or effective["context_token_limit"] < 1000:
            raise ModelSettingsError("context_token_limit 必须是不小于 1000 的整数")
        capability = effective.get("supports_images")
        if capability is not None and type(capability) is not bool:
            raise ModelSettingsError("supports_images 必须是 true 或 false；未声明时省略此字段")
        #None 只在内部表示未声明，不能当作图片支持；不会根据模型名称猜测。
        effective.setdefault("supports_images", None)
        return ModelSelection(self._client(effective), effective)

    def _client(self, effective):
        original = str(self.config.get("base_url") or "https://api.anthropic.com").rstrip("/")
        endpoint = str(effective.get("base_url") or original).rstrip("/")
        variable = effective.get("api_key_env")
        if endpoint != original and not variable:
            #即使调用者提供了未经过 ModelSettings 的 fallback，也不能把旧密钥送往新地址。
            raise ModelSettingsError("不同 base_url 必须显式配置 api_key_env，不能继承旧端点密钥")
        if variable:
            token = os.getenv(variable)
            if not token or not token.strip():
                raise ModelSettingsError("模型档案引用的密钥环境变量未设置；不会回退到旧密钥")
        elif self.client is not None:
            return self.client
        else:
            token = effective.get("api_key")
            if not isinstance(token, str) or not token.strip():
                raise ModelSettingsError("模型缺少凭据，请配置 api_key_env 并设置对应环境变量")
        #缓存只保存散列键，不把密钥放进 profile、运行记录或可展示配置中。
        signature = hashlib.sha256(token.encode("utf-8")).digest()
        key = (endpoint, variable, signature)
        if key not in self._clients:
            try:
                self._clients[key] = Anthropic(auth_token=token, base_url=endpoint)
            except Exception:
                raise ModelSettingsError("无法创建模型连接，请检查宿主模型档案和凭据环境变量") from None
        return self._clients[key]

    def close(self):
        """只关闭本路由器创建的宿主连接；注入的旧客户端仍由原调用方管理。"""
        clients, self._clients = list(self._clients.values()), {}
        errors = []
        for client in clients:
            try:
                client.close()
            except Exception as error:
                errors.append(type(error).__name__)
        if errors:
            raise RuntimeError("部分模型连接未能关闭：" + ", ".join(errors))


def bounded_auxiliary_request(selection, system, messages, max_tokens):
    """辅助任务仍有用途上限；档案只可进一步收紧输出，不把记忆/验收变成长回复。"""
    output = min(max_tokens, selection.config["max_tokens"])
    budget = RequestBudget.from_request(selection.config["context_token_limit"], system, [], output)
    size = budget.history_tokens(messages)
    if size > budget.history_limit:
        raise ContextBudgetError("辅助模型请求无法放入所选档案预算；" + budget.describe(size))
    return output
