import asyncio
import socket
import time
from types import SimpleNamespace

import httpcore
import httpx
import pytest

from tracefix.runtime.contracts import Phase
from tracefix.runtime.tools import ToolPipeline, ToolRegistry, ToolRejected, ToolSpec
from tracefix.runtime.web_tools import (
    WebFetchInput,
    WebFetchOutput,
    WebSearchInput,
    WebSearchOutput,
    _fetch_url,
    _filter_search_results,
    _pinned_transport,
    _resolve_public,
    register_web_tools,
)


class _Engine:
    web_search = None


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, *chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


def _response(request, status=200, *, body=b"ok", headers=None):
    return httpx.Response(status, headers=headers or {"content-type": "text/plain"},
                          stream=_Chunks(body), request=request)


def _registered(context=None, engine=None):
    bound = []

    def bind(name, description, input_model, handler, **options):
        bound.append((name, handler, options))

    register_web_tools(engine or _Engine(), None, context or {}, bind)
    return {name: (handler, options) for name, handler, options in bound}


def test_supervisor_only_registration_and_contract_metadata(monkeypatch):
    monkeypatch.delenv("TRACEFIX_WEB_SEARCH_API_KEY", raising=False)
    assert _registered({"worker_depth": 1}) == {}
    tools = _registered()
    assert set(tools) == {"WebFetch"}
    _, options = tools["WebFetch"]
    assert options["side_effect"] == "read"
    assert options["parallel_safe"] is True
    assert options["output_model"] is WebFetchOutput
    assert options["aliases"] == ("web.fetch", "web_fetch")


def test_search_registration_requires_provider_or_key(monkeypatch):
    monkeypatch.delenv("TRACEFIX_WEB_SEARCH_API_KEY", raising=False)
    assert "WebSearch" not in _registered()

    class Engine:
        async def web_search(self, arguments):
            return []

    tools = _registered(engine=Engine())
    assert "WebSearch" in tools
    assert tools["WebSearch"][1]["output_model"] is WebSearchOutput
    assert _registered(engine=SimpleNamespace(subagent_depth=1, web_search=Engine().web_search)) == {}
    monkeypatch.setenv("TRACEFIX_WEB_SEARCH_API_KEY", "test-provider-key")
    assert "WebSearch" in _registered()


@pytest.mark.asyncio
async def test_fetch_html_strips_scripts_and_reports_untrusted(monkeypatch):
    async def resolve(host, port):
        assert host == "example.com"
        return ["93.184.216.34"]

    def handler(request):
        assert request.url.host == "example.com"
        return _response(request, headers={"content-type": "text/html; charset=utf-8"},
                         body=b"<h1>Hello</h1><script>ignore()</script><p>world</p>")

    monkeypatch.setattr("tracefix.runtime.web_tools._resolve_public", resolve)
    monkeypatch.setattr("tracefix.runtime.web_tools._pinned_transport", lambda host, addresses: httpx.MockTransport(handler))
    output = await _fetch_url("https://example.com/page#fragment")
    assert output.final_url == "https://example.com/page"
    assert output.body == "Hello world"
    assert output.untrusted is True
    assert output.prompt_applied is False


@pytest.mark.asyncio
async def test_fetch_rejects_private_dns_and_redirect_target(monkeypatch):
    async def private_dns(host, port):
        raise ToolRejected("WebFetch 拒绝解析到非公有 IP")

    monkeypatch.setattr("tracefix.runtime.web_tools._resolve_public", private_dns)
    with pytest.raises(ToolRejected, match="非公有 IP"):
        await _fetch_url("https://example.com")

    calls = []

    async def public_dns(host, port):
        calls.append(host)
        if host == "example.com":
            return ["93.184.216.34"]
        raise ToolRejected("WebFetch 拒绝内部主机")

    def redirect(request):
        return httpx.Response(302, headers={"location": "http://localhost/secret"}, request=request)

    monkeypatch.setattr("tracefix.runtime.web_tools._resolve_public", public_dns)
    monkeypatch.setattr("tracefix.runtime.web_tools._pinned_transport", lambda host, addresses: httpx.MockTransport(redirect))
    with pytest.raises(ToolRejected, match="内部主机"):
        await _fetch_url("https://example.com")
    assert calls == ["example.com"]


@pytest.mark.asyncio
async def test_fetch_rejects_header_and_body_limits(monkeypatch):
    async def resolve(host, port):
        return ["93.184.216.34"]

    monkeypatch.setattr("tracefix.runtime.web_tools._resolve_public", resolve)
    monkeypatch.setattr("tracefix.runtime.web_tools._pinned_transport", lambda host, addresses: httpx.MockTransport(
        lambda request: _response(request, headers={"x-huge": "x" * 70000})))
    with pytest.raises(ToolRejected, match="too_large"):
        await _fetch_url("https://example.com")

    monkeypatch.setattr("tracefix.runtime.web_tools._pinned_transport", lambda host, addresses: httpx.MockTransport(
        lambda request: _response(request, body=b"x" * (1024 * 1024 + 1))))
    with pytest.raises(ToolRejected, match="too_large"):
        await _fetch_url("https://example.com")


@pytest.mark.asyncio
async def test_search_domain_filter_rejects_private_and_invalid_results(monkeypatch):
    async def public_dns(host, port):
        assert host == "example.com"
        return ["93.184.216.34"]

    monkeypatch.setattr("tracefix.runtime.web_tools._resolve_public", public_dns)
    arguments = WebSearchInput(query="tracefix", allowed_domains=["example.com"])
    output = await _filter_search_results([
        {"title": "ok", "url": "https://example.com/a", "snippet": "yes"},
        {"title": "wrong domain", "url": "https://other.example/a", "snippet": "no"},
        {"title": "private", "url": "http://127.0.0.1/metadata", "snippet": "no"},
        {"title": "not a url", "url": "javascript:alert(1)", "snippet": "no"},
    ], arguments)
    assert [result.title for result in output.results] == ["ok"]

    with pytest.raises(ValueError):
        WebSearchInput(query="x", allowed_domains=["example.com"], blocked_domains=["bad.example"])


@pytest.mark.parametrize("url", ["ftp://example.com/a", "http://localhost/a", "http://localhost./a",
                                  "http://metadata.google.internal", "http://instance-data/", "http://[::1]/",
                                  "http://user:password@example.com", "http://@example.com",
                                  "http://127.0.0.1", "http://[fe80::1%25eth0]/", "http://example.com/\npath"])
@pytest.mark.asyncio
async def test_fetch_rejects_unsafe_url_before_network(monkeypatch, url):
    async def forbidden_dns(host, port):
        raise AssertionError("invalid URL reached DNS")

    monkeypatch.setattr("tracefix.runtime.web_tools._resolve_public", forbidden_dns)
    with pytest.raises(ToolRejected):
        await _fetch_url(url)


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "100.64.0.1", "0.0.0.0",
                                      "224.0.0.1", "::1", "fc00::1", "fe80::1", "::ffff:127.0.0.1",
                                      "2001:db8::1", "64:ff9b::7f00:1"])
async def test_resolver_rejects_non_public_addresses(monkeypatch, address):
    monkeypatch.setattr("tracefix.runtime.web_tools.socket.getaddrinfo",
                        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))])
    with pytest.raises(ToolRejected, match="非公有 IP"):
        await _resolve_public("public.example", 443)


@pytest.mark.asyncio
async def test_resolver_checks_all_addresses_and_fails_closed(monkeypatch):
    monkeypatch.setattr("tracefix.runtime.web_tools.socket.getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
    ])
    with pytest.raises(ToolRejected, match="非公有 IP"):
        await _resolve_public("public.example", 443)


@pytest.mark.asyncio
async def test_pinned_connection_preserves_original_host_and_tls_name(monkeypatch):
    connected, tls, writes = [], [], []

    class Stream(httpcore.AsyncNetworkStream):
        def __init__(self):
            self.sent = False

        async def read(self, max_bytes, timeout=None):
            if self.sent:
                return b""
            self.sent = True
            return b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nok"

        async def write(self, buffer, timeout=None):
            writes.append(buffer)

        async def aclose(self):
            pass

        async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            tls.append(server_hostname)
            return self

        def get_extra_info(self, info):
            return None

    async def connect(self, host, port, timeout=None, local_address=None, socket_options=None):
        connected.append((host, port))
        return Stream()

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9090")
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    transport = _pinned_transport("example.com", ["93.184.216.34"])
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        response = await client.get("https://example.com/path")
    assert response.text == "ok"
    assert connected == [("93.184.216.34", 443)]
    assert tls == ["example.com"]
    assert b"Host: example.com\r\n" in b"".join(writes)


@pytest.mark.asyncio
async def test_fetch_validates_each_redirect_and_maximum_hops(monkeypatch):
    resolved, requests = [], []

    async def resolve(host, port):
        resolved.append(host)
        return ["93.184.216.34"]

    def redirect(request):
        requests.append(str(request.url))
        return _response(request, 302, headers={"location": "https://next.example/path"})

    monkeypatch.setattr("tracefix.runtime.web_tools._resolve_public", resolve)
    monkeypatch.setattr("tracefix.runtime.web_tools._pinned_transport", lambda host, addresses: httpx.MockTransport(redirect))
    with pytest.raises(ToolRejected, match="重定向次数超限"):
        await _fetch_url("https://example.com")
    assert len(requests) == 6
    assert resolved == ["example.com"] + ["next.example"] * 5


@pytest.mark.asyncio
@pytest.mark.parametrize("failure, message", [(httpx.ReadTimeout("late"), "超时"),
                                              (httpx.ConnectError("broken"), "网络请求失败")])
async def test_fetch_network_errors_are_recoverable(monkeypatch, failure, message):
    async def resolve(host, port):
        return ["93.184.216.34"]

    def fail(request):
        raise failure

    monkeypatch.setattr("tracefix.runtime.web_tools._resolve_public", resolve)
    monkeypatch.setattr("tracefix.runtime.web_tools._pinned_transport", lambda host, addresses: httpx.MockTransport(fail))
    with pytest.raises(ToolRejected, match=message):
        await _fetch_url("https://example.com")


@pytest.mark.asyncio
async def test_fetch_rejects_binary_and_compressed_bodies(monkeypatch):
    async def resolve(host, port):
        return ["93.184.216.34"]

    monkeypatch.setattr("tracefix.runtime.web_tools._resolve_public", resolve)
    for headers, message in [({"content-type": "image/png"}, "只允许文本"),
                             ({"content-type": "text/plain", "content-encoding": "gzip"}, "压缩响应")]:
        monkeypatch.setattr("tracefix.runtime.web_tools._pinned_transport", lambda host, addresses: httpx.MockTransport(
            lambda request: _response(request, headers=headers)))
        with pytest.raises(ToolRejected, match=message):
            await _fetch_url("https://example.com")


@pytest.mark.asyncio
async def test_unknown_length_stream_limit_stops_and_closes(monkeypatch):
    async def resolve(host, port):
        return ["93.184.216.34"]

    stream = _Chunks(b"x" * (1024 * 1024), b"y", b"must not be consumed")
    monkeypatch.setattr("tracefix.runtime.web_tools._resolve_public", resolve)
    monkeypatch.setattr("tracefix.runtime.web_tools._pinned_transport", lambda host, addresses: httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "text/plain"}, stream=stream)))
    with pytest.raises(ToolRejected, match="too_large"):
        await _fetch_url("https://example.com")
    assert stream.closed


@pytest.mark.asyncio
async def test_search_blocked_domain_strict_shape_and_private_dns(monkeypatch):
    async def resolve(host, port):
        if host == "public-name.example":
            raise ToolRejected("非公有 IP")
        return ["93.184.216.34"]

    monkeypatch.setattr("tracefix.runtime.web_tools._resolve_public", resolve)
    output = await _filter_search_results([
        {"title": "blocked", "url": "https://sub.blocked.example", "snippet": "no"},
        {"title": "dns private", "url": "https://public-name.example", "snippet": "no"},
        {"title": 123, "url": "https://example.com", "snippet": "invalid"},
        {"title": "valid", "url": "https://blocked.example.attacker.com", "snippet": "yes"},
    ], WebSearchInput(query="tracefix", blocked_domains=["blocked.example"]))
    assert [item.title for item in output.results] == ["valid"]


@pytest.mark.asyncio
async def test_registered_web_tools_execute_with_output_validation(monkeypatch):
    async def resolve(host, port):
        return ["93.184.216.34"]

    async def search(arguments):
        return [{"title": "Reference", "url": "https://example.com/docs", "snippet": "helpful"}]

    registry, handlers = ToolRegistry(), {}

    def bind(name, description, input_model, handler, **options):
        registry.register(ToolSpec(name, description, input_model,
                                  phases=frozenset(options.pop("phases")), **options))
        handlers[name] = handler

    monkeypatch.setattr("tracefix.runtime.web_tools._resolve_public", resolve)
    monkeypatch.setattr("tracefix.runtime.web_tools._pinned_transport", lambda host, addresses: httpx.MockTransport(
        lambda request: _response(request)))
    register_web_tools(SimpleNamespace(web_search=search), None, {}, bind)
    pipeline = ToolPipeline(registry, handlers, Phase.EXPLORE)
    fetch = await pipeline.execute("web.fetch", {"url": "https://example.com", "prompt": "summarize"}, "fetch")
    search = await pipeline.execute("WebSearch", {"query": "reference"}, "search")
    assert fetch.executed and not fetch.is_error
    assert fetch.result["body"] == "ok"
    assert fetch.result["prompt_applied"] is False
    assert "evidence_refs" not in fetch.result
    assert search.executed and not search.is_error
    assert search.result["results"][0]["title"] == "Reference"
    assert search.result["untrusted"] is True
    assert "evidence_refs" not in search.result


@pytest.mark.asyncio
async def test_brave_provider_uses_fixed_endpoint_key_and_domain_query(monkeypatch):
    requests = []

    async def resolve(host, port):
        assert host in {"api.search.brave.com", "docs.example.com"}
        return ["93.184.216.34"]

    def provider(request):
        requests.append(request)
        return _response(request, headers={"content-type": "application/json"},
            body=b'{"web":{"results":[{"title":"Docs","url":"https://docs.example.com","description":"Reference"}]}}')

    monkeypatch.setenv("TRACEFIX_WEB_SEARCH_API_KEY", "test-provider-key")
    monkeypatch.setattr("tracefix.runtime.web_tools._resolve_public", resolve)
    monkeypatch.setattr("tracefix.runtime.web_tools._pinned_transport", lambda host, addresses: httpx.MockTransport(provider))
    handler, _ = _registered()["WebSearch"]
    output = await handler(WebSearchInput(query="tracefix", allowed_domains=["example.com"]), "brave")
    assert [item.title for item in output.results] == ["Docs"]
    assert str(requests[0].url).startswith("https://api.search.brave.com/res/v1/web/search?")
    assert requests[0].url.params["q"] == "tracefix (site:example.com)"
    assert requests[0].headers["X-Subscription-Token"] == "test-provider-key"


@pytest.mark.asyncio
async def test_provider_failure_is_recoverable_and_invalid_shape_rejected(monkeypatch):
    async def failed_provider(arguments):
        raise httpx.ReadTimeout("late")

    handler, _ = _registered(engine=SimpleNamespace(web_search=failed_provider))["WebSearch"]
    with pytest.raises(ToolRejected, match="provider 请求失败"):
        await handler(WebSearchInput(query="tracefix"), "failed")

    handler, _ = _registered(engine=SimpleNamespace(web_search=lambda arguments: "invalid"))["WebSearch"]
    with pytest.raises(ToolRejected, match="格式无效"):
        await handler(WebSearchInput(query="tracefix"), "invalid")


@pytest.mark.asyncio
async def test_sync_provider_does_not_block_timeout():
    def slow_provider(arguments):
        time.sleep(0.15)
        return []

    handler, _ = _registered(engine=SimpleNamespace(web_search=slow_provider))["WebSearch"]
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(handler(WebSearchInput(query="tracefix"), "slow"), timeout=0.01)


@pytest.mark.parametrize("domains", [["https://example.com"], ["*.example.com"], [""], ["example.com/path"]])
def test_search_rejects_ambiguous_domain_filters(domains):
    with pytest.raises(ValueError, match="域过滤器"):
        WebSearchInput(query="tracefix", allowed_domains=domains)


def test_web_models_reject_evidence_and_prompt_claims():
    with pytest.raises(ValueError):
        WebFetchOutput(source="https://example.com", final_url="https://example.com", status_code=200,
                       body="text", prompt_applied=True)
    with pytest.raises(ValueError):
        WebSearchOutput(results=[], evidence_refs=["external-reference"])
    with pytest.raises(ValueError):
        WebFetchInput(url="https://example.com", unexpected="value")

