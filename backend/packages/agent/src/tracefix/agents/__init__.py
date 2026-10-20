"""Optional agent adapters; importing the SDK worker avoids legacy imports."""

__all__ = ['PydanticAIAdapter', 'PydanticAIAdapterError']


def __getattr__(name):
    if name in __all__:
        from tracefix.agents.pydantic_ai_adapter import PydanticAIAdapter, PydanticAIAdapterError
        return {'PydanticAIAdapter': PydanticAIAdapter,
                'PydanticAIAdapterError': PydanticAIAdapterError}[name]
    raise AttributeError(name)
