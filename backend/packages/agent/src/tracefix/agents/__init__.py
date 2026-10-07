"""Optional agent adapters; importing this package does not select a backend."""

from tracefix.agents.pydantic_ai_adapter import PydanticAIAdapter, PydanticAIAdapterError

__all__ = ['PydanticAIAdapter', 'PydanticAIAdapterError']
