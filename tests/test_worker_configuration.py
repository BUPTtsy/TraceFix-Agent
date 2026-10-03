from pathlib import Path

import pytest
from dotenv import dotenv_values

from tracefix.cli.main import configured_worker_model


WORKER_ENVIRONMENT = (
    'TRACEFIX_WORKER_BASE_URL',
    'TRACEFIX_WORKER_API_KEY',
    'TRACEFIX_WORKER_TEXT_MODEL',
    'TRACEFIX_WORKER_VISION_MODEL',
)


@pytest.fixture(autouse=True)
def isolated_model_configuration(monkeypatch):
    for name in WORKER_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('TRACEFIX_BASE_URL', 'https://supervisor.example/v1')
    monkeypatch.setenv('TRACEFIX_API_KEY', 'supervisor-test-key')
    monkeypatch.setenv('TRACEFIX_TEXT_MODEL', 'supervisor-text')
    monkeypatch.setenv('TRACEFIX_VISION_MODEL', 'supervisor-vision')


def test_no_worker_overrides_reuses_supervisor():
    assert configured_worker_model() is None


def test_empty_worker_example_reuses_supervisor(monkeypatch):
    example = dotenv_values(Path(__file__).resolve().parents[1] / '.env.example')
    for name in WORKER_ENVIRONMENT:
        assert example[name] == ''
        monkeypatch.setenv(name, example[name])
    assert configured_worker_model() is None


def test_worker_overrides_use_separate_provider_and_models(monkeypatch):
    monkeypatch.setenv('TRACEFIX_WORKER_BASE_URL', 'https://workers.example/v1/')
    monkeypatch.setenv('TRACEFIX_WORKER_API_KEY', 'worker-test-key')
    monkeypatch.setenv('TRACEFIX_WORKER_TEXT_MODEL', 'worker-text')
    monkeypatch.setenv('TRACEFIX_WORKER_VISION_MODEL', 'worker-vision')
    model = configured_worker_model()
    assert model.base_url == 'https://workers.example/v1'
    assert model.key == 'worker-test-key'
    assert model.text_model == 'worker-text'
    assert model.vision_model == 'worker-vision'


def test_partial_worker_override_inherits_supervisor_settings(monkeypatch):
    monkeypatch.setenv('TRACEFIX_WORKER_TEXT_MODEL', 'worker-text')
    model = configured_worker_model()
    assert model.base_url == 'https://supervisor.example/v1'
    assert model.key == 'supervisor-test-key'
    assert model.text_model == 'worker-text'
    assert model.vision_model == 'supervisor-vision'


def test_worker_can_disable_images_for_a_separate_text_model(monkeypatch):
    monkeypatch.setenv('TRACEFIX_WORKER_TEXT_MODEL', 'worker-text')
    monkeypatch.setenv('TRACEFIX_WORKER_VISION_MODEL', '')
    assert configured_worker_model().vision_model == ''


def test_worker_vision_override_alone_creates_gateway(monkeypatch):
    monkeypatch.setenv('TRACEFIX_WORKER_VISION_MODEL', 'worker-vision')
    model = configured_worker_model()
    assert model.text_model == 'supervisor-text'
    assert model.vision_model == 'worker-vision'
