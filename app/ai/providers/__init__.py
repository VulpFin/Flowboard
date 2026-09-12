# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Adapter implementations.  `ADAPTERS` maps the `ProviderSpec.adapter` key to
a class; add a new module here when a provider needs a native protocol."""
from .openai_compat import OpenAICompatAdapter
from .anthropic import AnthropicAdapter
from .gemini import GeminiAdapter
from .cohere import CohereAdapter
from .stability import StabilityAdapter
from .replicate import ReplicateAdapter
from .azure_openai import AzureOpenAIAdapter

ADAPTERS = {
    "openai_compat": OpenAICompatAdapter,
    "anthropic": AnthropicAdapter,
    "gemini": GeminiAdapter,
    "cohere": CohereAdapter,
    "stability": StabilityAdapter,
    "replicate": ReplicateAdapter,
    "azure_openai": AzureOpenAIAdapter,
}
