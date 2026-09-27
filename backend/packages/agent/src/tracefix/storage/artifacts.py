"""Readable, integrity-checked artifacts. No arbitrary-path read API."""
import json
import os
import re
import tempfile
import threading
from pathlib import Path

from tracefix.runtime.contracts import digest

ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]")
SECRET = re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?|(?:api[_-]?key|password|token|secret)\s*[:=]\s*)[^\s,\"'}]+")
KEY_TOKEN = re.compile(r'\bsk-[A-Za-z0-9_-]{20,}\b')


def sanitize(text: str) -> str:
    text = ANSI.sub("", text)
    text = "".join(c for c in text if c in "\n\t" or ord(c) >= 32 and ord(c) != 127)
    return KEY_TOKEN.sub('[REDACTED]', SECRET.sub(r"\1[REDACTED]", text))


def redact(value):
    if isinstance(value, dict):
        secret_keys = {'api_key','apikey','password','authorization','proxy-authorization',
                       'secret','access_token','refresh_token','cookie','set-cookie','x-api-key'}
        return {k: '[REDACTED]' if str(k).lower() in secret_keys else redact(v) for k,v in value.items()}
    if isinstance(value, (list,tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return sanitize(value)
    return value


class Artifacts:
    def __init__(self, root: Path, notify=None):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.notify = notify
        self._lock = threading.RLock()

    def _notify(self, scope, run, ref, reason):
        if self.notify:
            try:
                self.notify({'scope': scope, 'run': run, 'ref': ref, 'reason': reason})
            except Exception:
                pass

    def _damage(self, scope, run, ref) -> str | None:
        """副本不可复用的原因，完好则为 None。"""
        try:
            self.read(scope, run, ref)
        except PermissionError:
            return '证据路径不可信'
        except FileNotFoundError:
            return '证据文件缺失'
        except ValueError:
            return '证据校验和不匹配'
        except OSError:
            return '证据文件不可读'
        return None

    def _path(self, scope: str, run: str, ref: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", scope) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run):
            raise PermissionError("证据归属无效")
        if not re.fullmatch(r"(?:[a-f0-9]{64}|[0-9]{4,}_[A-Za-z0-9_\-\u4e00-\u9fff]{1,100})\.(json|txt|png|html|diff)", ref):
            raise PermissionError("证据引用无效")
        p = self.root / scope / run / ref
        if not p.resolve().is_relative_to(self.root / scope / run) or p.is_symlink():
            raise PermissionError("证据路径通过符号链接逃逸")
        return p

    def _index_path(self, scope, run):
        path = self._path(scope, run, '0000_索引.json').with_name('文件索引.json')
        if path.is_symlink():
            raise PermissionError('文件索引不能是符号链接')
        return path

    def _index(self, scope, run):
        path = self._index_path(scope, run)
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}

    def _write(self, path, raw):
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            try:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            except BaseException:
                handle.close()
                temporary.unlink(missing_ok=True)
                raise
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def put(self, scope: str, run: str, value, ext: str = "json", *, label: str | None = None) -> str:
        label = label or {'json': '数据记录', 'txt': '文本记录', 'png': '页面截图',
                          'html': '修复报告', 'diff': '补丁差异'}.get(ext, '记录')
        if not re.fullmatch(r'[A-Za-z0-9_\-\u4e00-\u9fff]{1,100}', label):
            raise ValueError('文件用途只能包含中文、字母、数字、下划线和短横线')
        self._path(scope, run, f'0000_{label}.{ext}')
        if ext == "json":
            raw = json.dumps(redact(value), ensure_ascii=False, indent=2, default=str).encode('utf-8')
        elif isinstance(value, bytes):
            raw = value
        else:
            raw = sanitize(str(value)).encode()
        checksum = digest(raw)
        with self._lock:
            index = self._index(scope, run)
            for ref, entry in index.items():
                if entry['SHA256'] == checksum and entry['用途'] == label and ref.endswith('.'+ext):
                    # 旧副本损坏或丢失时不能去重复用，继续找完好副本，否则另写一份新证据
                    reason = self._damage(scope, run, ref)
                    if reason is None:
                        return ref
                    self._notify(scope, run, ref, reason)
            sequence = max((int(ref.split('_', 1)[0]) for ref in index), default=0) + 1
            while True:
                ref = f'{sequence:04d}_{label}.{ext}'
                path = self._path(scope, run, ref)
                if not path.exists():
                    break
                sequence += 1
            path.parent.mkdir(parents=True, exist_ok=True)
            self._write(path, raw)
            index[ref] = {'用途': label, 'SHA256': checksum, '字节数': len(raw)}
            self._write(self._index_path(scope, run),
                        json.dumps(index, ensure_ascii=False, indent=2).encode('utf-8'))
            return ref

    def read(self, scope: str, run: str, ref: str) -> bytes:
        raw = self._path(scope, run, ref).read_bytes()
        expected = ref.split('.')[0] if re.fullmatch(r'[a-f0-9]{64}\.[a-z]+', ref) else self._index(scope, run).get(ref, {}).get('SHA256')
        if digest(raw) != expected:
            raise ValueError("证据完整性校验失败")
        return raw

    def json(self, scope: str, run: str, ref: str):
        return json.loads(self.read(scope, run, ref))

    def exists(self, scope: str, run: str, ref: str) -> bool:
        return self._damage(scope, run, ref) is None
