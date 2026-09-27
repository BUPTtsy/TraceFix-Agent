"""Check the configured DeepSeek Chat Completions connection and wire protocol."""
import asyncio
import json
import struct
import zlib

from dotenv import load_dotenv
from pydantic import BaseModel

from tracefix.model.gateway import Gateway
from tracefix.runtime.contracts import BrowserAction


def red_image():
    def chunk(kind, data):
        return (struct.pack("!I", len(data)) + kind + data
                + struct.pack("!I", zlib.crc32(kind + data) & 0xffffffff))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack("!2I5B", 64, 64, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\0" + b"\xff\0\0" * 64) * 64))
            + chunk(b"IEND", b""))


class TextResult(BaseModel):
    ok: bool
    sum: int


class VisionResult(BaseModel):
    color: str


async def check_native_protocol(gateway):
    requests = []
    usage = []
    executed_calls = []

    async def diagnostic_snapshot(name, arguments, call_id):
        if name != "browser_snapshot" or arguments != {} or executed_calls:
            raise RuntimeError("工具诊断只接受一次 browser_snapshot({})，未执行浏览器操作")
        executed_calls.append(call_id)
        return {"diagnostic": True, "browser_executed": False,
                "observation": {"id": "diagnostic-only", "snapshot": "Synthetic diagnostic observation."}}

    response = await gateway.generate(
        BrowserAction,
        {"instruction": "Call browser_snapshot exactly once with {}, then return JSON kind=finish. "
         "This is a protocol diagnostic with a synthetic observation and no real browser."},
        tool_executor=diagnostic_snapshot,
        on_attempt=lambda model, request, attempt: requests.append(request),
        on_usage=usage.append,
    )
    if len(executed_calls) != 1 or response.value.kind != "finish":
        raise RuntimeError("未完成 native 工具协议诊断：需要一次工具调用及最终 finish")
    call_id = executed_calls[0]
    paired = False
    for request in requests:
        messages = request["json"]["messages"]
        for index, message in enumerate(messages):
            if message.get("role") == "tool" and message.get("tool_call_id") == call_id:
                paired = any(
                    previous.get("role") == "assistant"
                    and any(call.get("id") == call_id for call in previous.get("tool_calls", []))
                    for previous in messages[:index]
                )
    if not paired:
        raise RuntimeError("native 工具结果未以原始 tool_call_id 回传 role=tool 消息")
    return {"kind": "native_protocol", "model": response.model_revision,
            "output": response.value.model_dump(), "usage": usage,
            "browser_executed": False,
            "message": "已验证 tools / tool_calls / role=tool；工具回执为本地合成数据，未验证 MCP 浏览器"}


async def run_checks(gateway=None):
    gateway = gateway or Gateway(max_output_tokens=256, timeout=45,
                                 max_attempts=1, max_tool_rounds=1)
    results = []
    text = await gateway.generate(TextResult,
                                  {"instruction": "Return ok=true and sum=2+2."})
    results.append({"kind": "text", "model": text.model_revision,
                    "output": text.value.model_dump(), "usage": text.usage})

    if gateway.tool_mode == "native":
        results.append(await check_native_protocol(gateway))

    if gateway.vision_model:
        vision = await gateway.generate(
            VisionResult,
            {"instruction": "Name the dominant image color in English."},
            image=red_image(),
        )
        results.append({"kind": "vision", "model": vision.model_revision,
                        "output": vision.value.model_dump(), "usage": vision.usage})

    else:
        results.append({"kind": "vision", "status": "skipped",
                        "message": "未配置 TRACEFIX_VISION_MODEL，未发送图片请求"})
    return results


async def main():
    load_dotenv(".env")
    result = await run_checks()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
