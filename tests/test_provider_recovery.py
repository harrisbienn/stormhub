"""Provider transport and interrupted acquisition tests without network access."""

from __future__ import annotations

import gzip
import io
import ssl
from urllib.error import URLError
from urllib.request import Request

import pytest
from test_hrrr_acquisition import REQUEST, fake_provider
from test_publication_recovery import _parallel

from stormhub import _files
from stormhub.met import hrrr_acquisition as hrrr
from stormhub.met.precipitation_features import PrecipitationFeatureError


@pytest.mark.parametrize("phase", ["payload", "receipt"])
@pytest.mark.parametrize("partial", [False, True])
def test_interrupted_acquisition_retries_without_partial_final_files(tmp_path, monkeypatch, phase, partial):
    """Interrupted payload/receipt staging leaves only completed immutable artifacts."""
    write = _files._write_content

    def interrupted(stream, content):
        is_receipt = b'"acquisition_id"' in content
        if is_receipt == (phase == "receipt"):
            if partial:
                stream.write(content[:3])
                stream.flush()
            raise OSError("injected acquisition interruption")
        write(stream, content)

    monkeypatch.setattr(_files, "_write_content", interrupted)
    with pytest.raises(OSError, match="injected"):
        hrrr.acquire(REQUEST, tmp_path, transport=fake_provider()[0])
    assert not list(tmp_path.glob("acquisition-*.json"))
    assert not list(tmp_path.glob(".stormhub-*.tmp"))
    monkeypatch.setattr(_files, "_write_content", write)
    receipt = hrrr.acquire(REQUEST, tmp_path, transport=fake_provider()[0])
    assert hrrr.acquire(REQUEST, tmp_path, offline=True) == receipt


def _acquisition_worker(root, ready, results):
    ready.wait(timeout=15)
    receipt = hrrr.acquire(REQUEST, root, transport=fake_provider()[0])
    results.put(receipt["acquisition_id"])


def test_concurrent_acquisitions_share_one_authenticated_receipt(tmp_path):
    """Concurrent identical requests reuse the first receipt including its observation clock."""
    results = _parallel(_acquisition_worker, [(tmp_path,), (tmp_path,)])
    assert results[0] == results[1]
    assert hrrr.acquire(REQUEST, tmp_path, offline=True)["acquisition_id"] == results[0]


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test/data",
        "file:///tmp/data",
        "ftp://example.test/data",
        "https://other.test/data",
        "https://example.test:444/data",
        "https://example.test:0/data",
        "https://@example.test/data",
        "https://user:secret@example.test/data",
        "https://example.test/data?token=secret",
        "https://example.test/data#fragment",
    ],
)
def test_redirect_rejects_scheme_origin_and_credential_changes(url):
    """The urllib redirect boundary refuses unsupported destinations before opening them."""
    handler = hrrr._ProviderRedirectHandler()
    request = Request("https://example.test/data", headers={"Range": "bytes=0-9", "If-Match": '"one"'})
    with pytest.raises(PrecipitationFeatureError):
        handler.redirect_request(request, None, 302, "redirect", {}, url)


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_same_origin_redirect_preserves_method_and_conditional_headers(code, method):
    """A safe redirect never upgrades a metadata request to a body download."""
    request = Request("https://example.test/data", method=method, headers={"Range": "bytes=0-9", "If-Match": '"one"'})
    redirected = hrrr._ProviderRedirectHandler().redirect_request(
        request,
        None,
        code,
        "redirect",
        {},
        "https://example.test/renamed",
    )
    assert redirected.full_url == "https://example.test/renamed"
    assert redirected.get_method() == method
    assert redirected.get_header("Range") == "bytes=0-9"
    assert redirected.get_header("If-match") == '"one"'


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_redirect_does_not_read_an_unbounded_response_body(code):
    """Redirect handling discards its response without urllib's unbounded drain."""
    from types import SimpleNamespace

    class Body:
        closed = False

        def read(self, *args):
            pytest.fail("redirect body was read")

        def close(self):
            self.closed = True

    body = Body()
    handler = hrrr._ProviderRedirectHandler()
    handler.parent = SimpleNamespace(open=lambda request, timeout: request)
    request = Request("https://example.test/data", method="HEAD")
    request.timeout = 45
    redirected = getattr(handler, f"http_error_{code}")(
        request,
        body,
        code,
        "redirect",
        {"location": "/renamed"},
    )
    assert body.closed and redirected.get_method() == "HEAD"
    assert redirected.full_url == "https://example.test/renamed"


class _Response(io.BytesIO):
    status = 200
    headers = {"Content-Length": "1"}


def test_actual_byte_limit_does_not_trust_content_length(monkeypatch):
    """A dishonest length cannot cause an over-limit body to be accepted."""
    monkeypatch.setattr(hrrr, "_open_provider_request", lambda *args, **kwargs: _Response(b"too much"))
    with pytest.raises(PrecipitationFeatureError, match="byte limit"):
        hrrr.http_request("https://example.test/data", method="GET", headers={}, limit=3)


def test_slow_stream_exceeds_cooperative_deadline_without_retry(monkeypatch):
    """Slow progress consumes a shared deadline even when each read succeeds."""
    clock = [0.0]
    calls = []

    class SlowResponse(_Response):
        def read1(self, size):
            clock[0] += 100
            return b"x"

    def open_request(*args, **kwargs):
        calls.append(kwargs["timeout"])
        return SlowResponse()

    monkeypatch.setattr(hrrr.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(hrrr, "_open_provider_request", open_request)
    with pytest.raises(PrecipitationFeatureError, match="deadline"):
        hrrr.http_request("https://example.test/data", method="GET", headers={}, limit=10)
    assert calls == [45.0]


def test_certificate_failure_is_permanent(monkeypatch):
    """TLS authentication failures must not be treated as transient availability failures."""
    calls = []

    def invalid_certificate(*args, **kwargs):
        calls.append(1)
        raise URLError(ssl.SSLCertVerificationError(1, "untrusted certificate"))

    monkeypatch.setattr(hrrr, "_open_provider_request", invalid_certificate)
    with pytest.raises(PrecipitationFeatureError, match="transport rejected"):
        hrrr.http_request("https://example.test/data", method="GET", headers={}, limit=10)
    assert calls == [1]


def test_mrms_expansion_is_bounded_before_any_receipt(tmp_path):
    """A small compressed payload cannot expand beyond the established GRIB limit."""
    from test_mrms_precipitation import REQUEST as mrms_request
    from test_mrms_precipitation import provider

    from stormhub.met import mrms_precipitation as mrms

    compressed = gzip.compress(b"x" * (32 * 1024**2 + 1), mtime=0)
    assert len(compressed) < 64 * 1024
    with pytest.raises(PrecipitationFeatureError, match="bounded GRIB2"):
        mrms.acquire(mrms_request, tmp_path, transport=provider(content=compressed)[0])
    assert not list(tmp_path.glob("mrms-acquisition-*.json"))


@pytest.mark.parametrize("kind", ["channel", "state"])
def test_nwm_receipt_interruption_reuses_payload_budget(tmp_path, monkeypatch, kind):
    """A nearly full cache admits an identical payload without charging it twice."""
    from test_nwm_channel import REQUEST as channel_request
    from test_nwm_state import REQUEST as state_request

    from stormhub.met import nwm_channel, nwm_state

    module = nwm_channel if kind == "channel" else nwm_state
    request = {**channel_request, "hours": 1} if kind == "channel" else state_request
    payload = b"\x89HDF\r\n\x1a\n" + b"fixture bytes" * 1000

    def transport(url, *, method, headers, limit):
        return 200, {"etag": '"one"', "content-length": str(len(payload))}, payload if method == "GET" else b""

    write = module.write_immutable
    receipt_sizes = []

    def interrupted(path, content):
        if path.suffix == ".json":
            receipt_sizes.append(len(content))
            raise OSError("receipt interruption")
        return write(path, content)

    monkeypatch.setattr(module, "write_immutable", interrupted)
    with pytest.raises(OSError, match="receipt interruption"):
        module.acquire(request, tmp_path, transport=transport)
    used = sum(path.stat().st_size for path in tmp_path.rglob("*") if path.is_file())
    budget = used + receipt_sizes[0] + 256
    assert used + len(payload) > budget
    monkeypatch.setattr(module, "MAX_CACHE", budget)
    monkeypatch.setattr(module, "write_immutable", write)
    receipt = module.acquire(request, tmp_path, transport=transport)
    assert module.acquire(request, tmp_path, offline=True) == receipt
    assert sum(path.stat().st_size for path in tmp_path.rglob("*") if path.is_file()) <= budget


@pytest.mark.parametrize("kind", ["channel", "state"])
def test_nwm_receipt_bytes_cannot_exceed_cache_limit(tmp_path, monkeypatch, kind):
    """Payload fit alone is insufficient when the completion receipt exceeds budget."""
    from test_nwm_channel import REQUEST as channel_request
    from test_nwm_state import REQUEST as state_request

    from stormhub.met import nwm_channel, nwm_state

    module = nwm_channel if kind == "channel" else nwm_state
    request = {**channel_request, "hours": 1} if kind == "channel" else state_request
    payload = b"\x89HDF\r\n\x1a\nfixture bytes"
    monkeypatch.setattr(module, "MAX_CACHE", len(payload) + 1)

    def transport(url, *, method, headers, limit):
        return 200, {"etag": '"one"', "content-length": str(len(payload))}, payload if method == "GET" else b""

    with pytest.raises(PrecipitationFeatureError, match="receipt exceeds"):
        module.acquire(request, tmp_path, transport=transport)
    assert not list(tmp_path.glob("*acquisition-*.json"))
