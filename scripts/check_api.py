"""Small live connectivity test. Key comes from environment and is never logged."""
import asyncio
import json
import struct
import zlib
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel
from tracefix.model.gateway import Gateway


def red_image():
    def chunk(t,data):return struct.pack('!I',len(data))+t+data+struct.pack('!I',zlib.crc32(t+data)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',64,64,8,2,0,0,0))+chunk(b'IDAT',zlib.compress((b'\0'+b'\xff\0\0'*64)*64))+chunk(b'IEND',b'')


class TextResult(BaseModel):
    ok: bool
    sum: int


class VisionResult(BaseModel):
    color: str


async def main():
    load_dotenv('.env')
    gateway=Gateway(max_output_tokens=128,timeout=45)
    result=[]
    for schema,context,image in [(TextResult,{'instruction':'Return ok=true and sum=2+2.'},None),
                                  (VisionResult,{'instruction':'Name the dominant image color in English.'},red_image())]:
        response=await gateway.generate(schema,context,image=image)
        result.append({'model':response.model_revision,'output':response.value.model_dump(),'usage':response.usage,
            'message':'视觉模型连通性检查已完成' if image else '文本模型连通性检查已完成'})
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':asyncio.run(main())
