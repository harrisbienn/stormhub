"""Deterministic compatibility checks for the pinned HTTP client dependency."""

import io

import pytest
import requests
from requests.adapters import BaseAdapter


@pytest.fixture
def isolated_http_environment(monkeypatch, tmp_path):
    """Use dummy netrc credentials and remove inherited proxy configuration."""
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
    ):
        monkeypatch.delenv(name, raising=False)
    netrc = tmp_path / "netrc"
    netrc.write_text("machine library.example login fixture-user password fixture-password\n")
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))


def test_netrc_credentials_follow_hostname_not_url_userinfo(isolated_http_environment):
    # GHSA-9hjg-9r4m-mvj7: the old netloc split matched the userinfo as a host.
    """Prevent crafted URL userinfo from selecting another host's credentials."""
    assert requests.utils.get_netrc_auth("https://library.example/catalog.json") == ("fixture-user", "fixture-password")
    assert requests.utils.get_netrc_auth("https://library.example:80@other.example/catalog.json") is None


def test_credentials_are_removed_on_cross_host_redirect(isolated_http_environment):
    """Strip origin credentials before following a redirect to another host."""
    session = requests.Session()
    original = session.prepare_request(requests.Request("GET", "https://library.example/catalog.json"))
    assert original.headers["Authorization"].startswith("Basic ")
    redirected = original.copy()
    redirected.url = "https://other.example/catalog.json"
    response = requests.Response()
    response.request = original
    session.rebuild_auth(redirected, response)
    assert "Authorization" not in redirected.headers


def test_proxy_routing_does_not_disable_tls_verification(isolated_http_environment, monkeypatch):
    """Honor explicit proxy bypass while retaining certificate verification."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("NO_PROXY", "library.example")
    session = requests.Session()
    direct = session.merge_environment_settings("https://library.example/catalog.json", {}, False, None, None)
    proxied = session.merge_environment_settings("https://other.example/catalog.json", {}, False, None, None)
    assert direct["proxies"] == {}
    assert proxied["proxies"]["https"] == "http://proxy.example:8080"
    assert direct["verify"] is True
    assert proxied["verify"] is True


def test_streamed_download_preserves_bytes_and_verification(isolated_http_environment):
    """Stream exact fixture bytes through the public Requests client interface."""
    payload = b'{"id":"test-catalog","type":"Catalog"}\n'

    class FixtureAdapter(BaseAdapter):
        def send(self, request, **kwargs):
            assert kwargs["verify"] is True
            assert kwargs["timeout"] == 5
            response = requests.Response()
            response.status_code = 200
            response.request = request
            response.raw = io.BytesIO(payload)
            return response

        def close(self):
            pass

    with requests.Session() as session:
        session.mount("https://", FixtureAdapter())
        with session.get("https://library.example/catalog.json", stream=True, timeout=5) as response:
            response.raise_for_status()
            assert b"".join(response.iter_content(chunk_size=7)) == payload
