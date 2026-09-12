# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Shared data types for the provider abstraction."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# --- errors -------------------------------------------------------------

class ProviderError(Exception):
    """Base class.  `kind` is a stable machine-readable category used for
    usage records and user-facing messages."""

    kind = "error"
    user_message = "The AI provider returned an error."

    def __init__(self, message: str = "", *, provider: str = "", status: Optional[int] = None, retry_after: Optional[float] = None):
        super().__init__(message or self.user_message)
        self.provider = provider
        self.status = status
        self.retry_after = retry_after
        self.detail = message

    def friendly(self) -> str:
        base = self.user_message
        if self.detail and self.detail != base:
            return f"{base} ({self.detail[:200]})"
        return base


class AuthenticationError(ProviderError):
    kind = "auth"
    user_message = "The API key for this provider was rejected. Check it in Settings → AI Providers."


class RateLimitedError(ProviderError):
    kind = "rate_limited"
    user_message = "The provider is rate-limiting requests. Please wait a moment and retry."


class InsufficientCreditsError(ProviderError):
    kind = "credits"
    user_message = "Your account with this provider is out of credits / quota."


class ModelNotFoundError(ProviderError):
    kind = "model_not_found"
    user_message = "The selected model is not available on this provider."


class ProviderUnavailableError(ProviderError):
    kind = "unavailable"
    user_message = "The provider is currently unavailable. Try again later."


class BadRequestError(ProviderError):
    kind = "bad_request"
    user_message = "The provider rejected the request."


class UnsupportedCapabilityError(ProviderError):
    kind = "unsupported"
    user_message = "This provider/model does not support that capability."


class StructuredOutputError(ProviderError):
    kind = "structured_output"
    user_message = "The model did not return valid structured output."


class NoProviderConfiguredError(ProviderError):
    kind = "not_configured"
    user_message = "No AI provider is configured. Add one in Settings → AI Providers."


# --- capabilities / models ---------------------------------------------

@dataclass
class Capabilities:
    chat: bool = True
    streaming: bool = True
    tools: bool = False  # function calling
    json_mode: bool = False  # provider-native JSON / structured output
    vision: bool = False
    reasoning: bool = False
    embeddings: bool = False
    image_generation: bool = False
    list_models: bool = True

    def labels(self) -> List[str]:
        out = []
        if self.chat:
            out.append("Chat")
        if self.reasoning:
            out.append("Reasoning")
        if self.vision:
            out.append("Vision")
        if self.tools:
            out.append("Tool use")
        if self.json_mode:
            out.append("JSON")
        if self.embeddings:
            out.append("Embeddings")
        if self.image_generation:
            out.append("Image")
        if self.streaming:
            out.append("Streaming")
        return out

    def to_dict(self) -> Dict[str, bool]:
        return dict(self.__dict__)


@dataclass
class ModelInfo:
    id: str  # provider-native model id
    name: str = ""
    family: str = ""  # grouping label for the UI ("GPT", "o-series", "Claude", ...)
    capabilities: Optional[Capabilities] = None  # None => inherit provider defaults
    context_window: Optional[int] = None
    owned_by: str = ""
    tags: List[str] = field(default_factory=list)  # "fast", "reasoning", "vision", "image", "embedding"

    @property
    def display_name(self) -> str:
        return self.name or self.id

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.display_name,
            "family": self.family,
            "context_window": self.context_window,
            "tags": self.tags,
            "capabilities": self.capabilities.to_dict() if self.capabilities else None,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ModelInfo":
        caps = d.get("capabilities")
        return cls(
            id=d["id"],
            name=d.get("name", ""),
            family=d.get("family", ""),
            context_window=d.get("context_window"),
            tags=list(d.get("tags") or []),
            capabilities=Capabilities(**caps) if caps else None,
        )


# --- credential schema ------------------------------------------------

@dataclass
class CredentialField:
    key: str
    label: str
    secret: bool = True
    required: bool = True
    placeholder: str = ""
    help: str = ""
    default: str = ""


# --- chat ---------------------------------------------------------------

@dataclass
class ChatMessage:
    role: str  # system|user|assistant|tool
    content: str = ""
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None  # assistant tool calls (normalised: {id,name,arguments})


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: Dict[str, Any]  # JSON schema


@dataclass
class Usage:
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


@dataclass
class ChatResult:
    content: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)  # [{id, name, arguments(dict)}]
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    finish_reason: str = ""
    raw: Optional[Dict[str, Any]] = None


@dataclass
class ChatRequest:
    messages: List[ChatMessage]
    model: str
    temperature: Optional[float] = 0.2
    max_tokens: Optional[int] = 1024
    tools: Optional[List[ToolSpec]] = None
    tool_choice: Optional[str] = None  # auto|required|none|<tool name>
    json_schema: Optional[Dict[str, Any]] = None  # request native structured output when supported
    json_mode: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EmbeddingResult:
    vectors: List[List[float]]
    usage: Usage = field(default_factory=Usage)
    model: str = ""


@dataclass
class ImageResult:
    images: List[bytes]  # raw PNG/JPEG bytes
    urls: List[str] = field(default_factory=list)
    model: str = ""
