"""Host-compatible facade; PydanticAI is the sole model execution backend."""
import math
import os
from urllib.parse import urlsplit

from tracefix.model.contracts import ModelError, ModelOutputError, ModelResult
from tracefix.model.history import ModelProtocol


class Gateway(ModelProtocol):
    supports_tool_executor = True
    supports_streaming = True
    supports_context_assembler = True
    supports_tool_history_projection = True

    def __init__(self, base_url=None, key=None, text_model=None, vision_model=None,
                 max_output_tokens=20480, timeout=None, max_attempts=None,
                 max_retry_delay=60, tool_executor=None,
                 max_tool_rounds=40, thinking=None, stream=None, additional_tools=None):
        self.base_url = (base_url or os.getenv('TRACEFIX_BASE_URL', 'https://api.deepseek.com')).rstrip('/')
        self.key = key or os.getenv('TRACEFIX_API_KEY', '')
        self.text_model = text_model or os.getenv('TRACEFIX_TEXT_MODEL', 'deepseek-chat')
        self.vision_model = (vision_model if vision_model is not None else os.getenv('TRACEFIX_VISION_MODEL', '')).strip()
        self.max_output_tokens = max_output_tokens
        try:
            self.timeout = float(timeout if timeout is not None else os.getenv('TRACEFIX_MODEL_TIMEOUT', '240'))
        except (ValueError, TypeError) as error:
            raise ValueError('timeout must be a positive finite number') from error
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError('timeout must be a positive finite number')
        configured_stream = os.getenv('TRACEFIX_STREAM', 'true') if stream is None else stream
        if isinstance(configured_stream, str) and configured_stream.lower() in {'true', 'false'}:
            configured_stream = configured_stream.lower() == 'true'
        if type(configured_stream) is not bool:
            raise ValueError('stream must be true or false')
        self.stream = configured_stream
        # thinking 默认开启；只有显式传入 disabled 或配置环境变量才关闭。
        default_thinking = 'enabled' if urlsplit(self.base_url).hostname == 'api.deepseek.com' else None
        self.thinking = thinking if thinking is not None else os.getenv('TRACEFIX_THINKING', default_thinking)
        if self.thinking is not None and self.thinking not in {'enabled', 'disabled'}:
            raise ValueError('thinking must be enabled or disabled')
        self.additional_tools = list(additional_tools or [])
        self.tool_executor = tool_executor
        try:
            parsed_rounds = int(max_tool_rounds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError('max_tool_rounds must be a positive integer') from exc
        if (isinstance(max_tool_rounds, bool) or parsed_rounds < 1
                or isinstance(max_tool_rounds, float) and not max_tool_rounds.is_integer()):
            raise ValueError('max_tool_rounds must be a positive integer')
        self.max_tool_rounds = parsed_rounds
        configured_attempts = (os.getenv('TRACEFIX_MODEL_MAX_ATTEMPTS', '3')
                               if max_attempts is None else max_attempts)
        if isinstance(configured_attempts, bool):
            raise ValueError('max_attempts must be a positive integer')
        try:
            parsed_attempts = int(configured_attempts)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError('max_attempts must be a positive integer') from exc
        if isinstance(configured_attempts, float) and not configured_attempts.is_integer():
            raise ValueError('max_attempts must be a positive integer')
        if parsed_attempts < 1:
            raise ValueError('max_attempts must be a positive integer')
        self.max_attempts = parsed_attempts
        self.max_retry_delay = max(0.0, float(max_retry_delay))


    async def generate(self, schema, context, **runtime_options):
        from tracefix.agents.pydantic_ai_adapter import PydanticAIAdapter, PydanticAIAdapterError
        options = dict(runtime_options)
        if 'message_history' not in options and 'messages' in options:
            options['message_history'] = options['messages']
        options.pop('messages', None)
        if 'tool_executor' not in options and self.tool_executor is not None:
            options['tool_executor'] = self.tool_executor
        try:
            return await PydanticAIAdapter(self, output_retries=self.max_attempts - 1).generate(
                schema, context, **options)
        except PydanticAIAdapterError as error:
            error_type = ModelOutputError if error.category in {'output_validation', 'tool_protocol'} else ModelError
            raise error_type(str(error), category=error.category, status=error.status,
                             details=error.details) from error

    @staticmethod
    def delegation_tools():
        from tracefix.tools.supervisor import supervisor_tools
        return supervisor_tools()
