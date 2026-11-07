from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from .security import validate_provider_url


def validate_model_options(options):
    if options is None:
        return options
    allowed = {"temperature", "maxTokens", "contextWindow", "batchSize", "timeout", "sendDimensions", "protocol"}
    if set(options) - allowed:
        raise ValueError("包含不支持或只读的模型选项")
    bounds = {"temperature": (0, 2), "maxTokens": (1, 8192), "contextWindow": (1024, 1000000), "batchSize": (1, 64), "timeout": (1, 300)}
    for key, (lo, hi) in bounds.items():
        if key in options:
            value = options[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
                raise ValueError(f"{key} 不在允许范围内")
            if key in {"maxTokens", "contextWindow", "batchSize"} and not isinstance(value, int):
                raise ValueError(f"{key} 必须为整数")
    if "sendDimensions" in options and not isinstance(options["sendDimensions"], bool):
        raise ValueError("sendDimensions 必须为布尔值")
    if "protocol" in options and options["protocol"] not in {"dashscope", "compatible", "dashscope-compatible"}:
        raise ValueError("不支持的排序协议")
    return options


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)


class KBCreate(Payload):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=2000)
    category: str = Field(default="通用资料", max_length=80)
    color: Literal["violet", "blue", "green", "orange"] = "violet"
    model: str
    enabled: bool = True


class KBUpdate(Payload):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=2000)
    category: str | None = Field(default=None, max_length=80)
    model: str | None = None
    enabled: bool | None = None


class ModelCreate(Payload):
    name: str = Field(min_length=1, max_length=80)
    provider: Literal["OpenRouter", "AIHubMix", "通义百炼"]
    type: Literal["LLM", "Embedding", "Rerank"]
    modelId: str = Field(min_length=1, max_length=200)
    base: str = Field(min_length=1, max_length=500)
    dim: int | None = Field(default=None, ge=1, le=65536)
    enabled: bool = True
    apiKey: str = Field(default="", max_length=2000)
    options: dict = Field(default_factory=dict)

    _options = field_validator("options")(validate_model_options)

    @field_validator("base")
    @classmethod
    def check_url(cls, value):
        return validate_provider_url(value)


class ModelUpdate(Payload):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    provider: Literal["OpenRouter", "AIHubMix", "通义百炼"] | None = None
    type: Literal["LLM", "Embedding", "Rerank"] | None = None
    modelId: str | None = Field(default=None, min_length=1, max_length=200)
    base: str | None = Field(default=None, min_length=1, max_length=500)
    dim: int | None = Field(default=None, ge=1, le=65536)
    enabled: bool | None = None
    apiKey: str | None = Field(default=None, max_length=2000)
    options: dict | None = None

    _options = field_validator("options")(validate_model_options)

    @field_validator("base")
    @classmethod
    def check_url(cls, value):
        return validate_provider_url(value) if value else value


def validate_assistant_options(options):
    if options is None:
        return options
    if set(options) - {"topK", "rerankModelId"}:
        raise ValueError("不支持的助手配置项")
    if "topK" in options and (type(options["topK"]) is not int or not 1 <= options["topK"] <= 12):
        raise ValueError("topK 必须是 1–12 的整数")
    if options.get("rerankModelId") is not None and not isinstance(options["rerankModelId"], str):
        raise ValueError("rerankModelId 必须是模型标识")
    return options


class AssistantCreate(Payload):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=2000)
    kbIds: list[str] = Field(min_length=1, max_length=100)
    model: str
    prompt: str = Field(default="请根据检索资料回答并引用来源，资料不足时明确说明。", max_length=10000)
    greeting: str = Field(default="你好，有什么可以帮你？", max_length=1000)
    rerank: bool = False
    published: bool = False
    options: dict = Field(default_factory=dict)
    _options = field_validator("options")(validate_assistant_options)


class AssistantUpdate(Payload):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=2000)
    kbIds: list[str] | None = Field(default=None, min_length=1, max_length=100)
    model: str | None = None
    prompt: str | None = Field(default=None, max_length=10000)
    greeting: str | None = Field(default=None, max_length=1000)
    rerank: bool | None = None
    published: bool | None = None
    options: dict | None = None
    _options = field_validator("options")(validate_assistant_options)


class OCRUpdate(Payload):
    region: str = Field(default="cn-hangzhou", min_length=2, max_length=80)
    accessKeyId: str = Field(default="", max_length=2000)
    accessKeySecret: str = Field(default="", max_length=2000)
    endpoint: str = Field(default="", max_length=500)
