"""Host-facing model results and classified errors, independent of the backend."""
from dataclasses import dataclass
from typing import Any


@dataclass
class ModelResult:
    value: Any
    usage: dict
    model_revision: str
    finish_reason: str


class ModelError(RuntimeError):
    def __init__(self, message, *, status='FAILED', category='model', details=None):
        super().__init__(message)
        self.status = status
        self.category = category
        self.details = {**(details or {}), 'status': status, 'category': category}


class ModelOutputError(ModelError):
    pass
