from types import SimpleNamespace

import pytest

from tracefix.execution.browser import MCPBrowser, resolve_action_locator
from tracefix.execution.policy import Policy
from tracefix.runtime.contracts import BrowserAction, Locator


def text_block(text):
    return SimpleNamespace(type='text', text=text)


def image_block(data=b'png'):
    import base64
    return SimpleNamespace(type='image', data=base64.b64encode(data).decode())


def browser_with_tools(monkeypatch, snapshot_texts, console='console line', network='GET /api => 200'):
    browser = MCPBrowser([], Policy(['http://app:3000']))
    browser.connection_state = 'ready'
    browser.tools = {
        'browser_snapshot': {},
        'browser_take_screenshot': {},
        'browser_console_messages': {'required': []},
        'browser_network_requests': {'required': []},
        'browser_click': {},
    }
    snapshots = iter(snapshot_texts)

    async def call(kind, args=None):
        if kind == 'snapshot':
            return [text_block(next(snapshots))]
        if kind == 'screenshot':
            return [image_block()]
        if kind == 'console':
            return [text_block(console)]
        if kind == 'network':
            return [text_block(network)]
        if kind == 'click':
            return []
        raise AssertionError(kind)

    monkeypatch.setattr(browser, 'call', call)
    return browser


@pytest.mark.asyncio
async def test_observation_keeps_complete_channels_and_reports_availability(monkeypatch):
    middle = 'middle-node-' + ('x' * 41000)
    snapshot = 'URL: http://app:3000\n- heading "Start" [ref=start]\n' + middle
    browser = browser_with_tools(monkeypatch, [snapshot], console='Authorization: Bearer secret', network='GET /api => 200')

    result = await browser.observe()

    assert middle in result['snapshot']
    assert result['console'] == 'Authorization: Bearer [REDACTED]'
    assert result['network'] == 'GET /api => 200'
    assert result['collection']['channels']['snapshot']['status'] == 'available'
    assert result['collection']['channels']['a11y']['content_version']
    assert result['collection']['channels']['console']['status'] == 'available'
    assert result['collection']['channels']['network']['status'] == 'available'
    assert result['collection']['channels']['snapshot']['collector_truncated'] is False
    assert result['observed_at'] <= result['collection']['channels']['snapshot']['captured_at']


@pytest.mark.asyncio
async def test_upstream_truncation_is_preserved_as_channel_status(monkeypatch):
    snapshot = 'URL: http://app:3000\n[... content truncated by provider ...]\n- button "Save" [ref=save]'
    browser = browser_with_tools(monkeypatch, [snapshot])

    result = await browser.observe()

    metadata = result['collection']['channels']['snapshot']
    assert metadata['status'] == 'truncated'
    assert metadata['upstream_truncated'] is True
    assert metadata['collector_truncated'] is False


def test_modal_blocks_background_semantic_target():
    snapshot = ('URL: http://app:3000\n'
                '- button "Background" [ref=background]\n'
                '- dialog "Confirm" [ref=dialog]\n'
                '  - button "Confirm" [ref=confirm]')

    with pytest.raises(ValueError, match='弹窗之外'):
        resolve_action_locator(snapshot, Locator(role='button', name='Background'))
    assert resolve_action_locator(snapshot, Locator(role='button', name='Confirm')) == 'confirm'


@pytest.mark.asyncio
async def test_click_fresh_snapshot_regrounds_changed_reference(monkeypatch):
    first = 'URL: http://app:3000\n- button "Save" [ref=old]'
    changed = 'URL: http://app:3000\n- button "Save" [ref=new]'
    browser = browser_with_tools(monkeypatch, [first, changed, changed])
    initial = await browser.observe()
    action = BrowserAction(kind='click', observation_id=initial['id'],
                           page_generation=initial['page_generation'],
                           element_ref='old', locator=Locator(role='button', name='Save'))

    result = await browser.action(action)

    assert result['id'] != initial['id']
    assert browser.last_action['status'] == 'DONE'
    assert browser.last_action['dispatched_element_ref'] == 'new'
    assert browser.last_action['grounding_observation_id'] != initial['id']
    assert browser.last_action['pre_page_generation'] == initial['page_generation']
    assert browser.last_action['post_page_generation'] == result['page_generation']
    assert browser.last_action['business_status'] == 'unknown'


@pytest.mark.asyncio
async def test_click_rejects_ambiguous_fresh_snapshot(monkeypatch):
    first = 'URL: http://app:3000\n- button "Save" [ref=save]'
    ambiguous = ('URL: http://app:3000\n- button "Save" [ref=save-a]\n'
                 '- button "Save" [ref=save-b]')
    browser = browser_with_tools(monkeypatch, [first, ambiguous])
    initial = await browser.observe()
    action = BrowserAction(kind='click', observation_id=initial['id'],
                           element_ref='save', locator=Locator(role='button', name='Save'))

    with pytest.raises(PermissionError, match='唯一重定位'):
        await browser.action(action)
