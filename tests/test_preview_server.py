"""Exercise the preview over loopback without external services."""

import os
import subprocess
import threading
from functools import partial
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import quote

import pytest

from stormhub.server import serve


@pytest.fixture
def preview(tmp_path):
    """Serve a disposable catalog on an ephemeral loopback port."""
    root = tmp_path / "public"
    root.mkdir()
    (root / "catalog.json").write_text('{"id":"preview"}')
    handler = partial(serve.CORSRequestHandler, directory=str(root))
    with serve.ThreadedHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(path="/", method="GET"):
            connection = HTTPConnection(*server.server_address, timeout=5)
            try:
                connection.request(method, path)
                response = connection.getresponse()
                return response.status, dict(response.getheaders()), response.read()
            finally:
                connection.close()

        try:
            yield root, request
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_preview_read_only_and_no_shutdown(preview):
    """Reject writes while continuing to serve read-only catalog requests."""
    _, request = preview
    assert request("/shutdown", "POST")[0] == 501
    assert request("/catalog.json", "PUT")[0] == 501
    status, headers, body = request("/catalog.json")
    assert status == 200
    assert body == b'{"id":"preview"}'
    assert headers["Access-Control-Allow-Origin"] == "https://radiantearth.github.io"
    assert request("/", "HEAD")[2] == b""
    assert request("/", "OPTIONS")[0] == 204


def test_listing_encodes_names_and_request_path(preview):
    """Keep filenames and request paths inert in listing markup."""
    root, request = preview
    name = "storm & 'rain' #1%.json"
    (root / name).write_text("safe")
    status, _, body = request("/?label=%3Cscript%3Ealert(1)%3C/script%3E")
    text = body.decode()
    assert status == 200
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "storm &amp; &#x27;rain&#x27;" in text
    assert f'href="{quote(name)}"' in text
    assert request("/" + quote(name))[2] == b"safe"


def _link(link, target, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        if os.name != "nt" or not directory:
            pytest.skip("File symlink creation requires OS privileges")
        # Windows junction creation does not require symlink privileges.
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)


def test_external_directory_link_is_hidden_and_denied(preview, tmp_path):
    """Deny direct and encoded access through an external symlink or junction."""
    root, request = preview
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "secret.txt").write_text("private-content")
    _link(root / "outside", outside, directory=True)
    assert b"outside" not in request()[2]
    for path in ("/outside/secret.txt", "/%6futside/secret.txt", "/outside/"):
        assert request(path)[0] == 403
    assert b"private-content" not in request("/../private/secret.txt")[2]
    assert b"private-content" not in request("/%2e%2e/private/secret.txt")[2]


@pytest.mark.parametrize("name", ["secret.txt", "index.html", "index.htm"])
def test_external_file_and_index_links_are_denied(preview, tmp_path, name):
    """Confine both direct files and implicitly opened directory indexes."""
    root, request = preview
    secret = tmp_path / "secret.txt"
    secret.write_text("private-content")
    _link(root / name, secret)
    assert request("/" + name)[0] == 403
    status, _, body = request("/")
    assert status == (403 if name.startswith("index.") else 200)
    assert b"private-content" not in body


def test_internal_directory_link_remains_readable(preview):
    """Permit links whose final target stays inside the trusted preview root."""
    root, request = preview
    inside = root / "inside"
    inside.mkdir()
    (inside / "item.json").write_text("internal")
    _link(root / "alias", inside, directory=True)
    assert request("/alias/item.json")[2] == b"internal"


def test_main_defaults_to_loopback_without_changing_directory(tmp_path, monkeypatch):
    """Require explicit network opt-in and close the server on interruption."""
    seen = {}

    class FakeServer:
        server_port = 5000

        def __init__(self, address, handler):
            seen.update(address=address, directory=handler.keywords["directory"])

        def __enter__(self):
            return self

        def __exit__(self, *args):
            seen["closed"] = True

        def serve_forever(self):
            raise KeyboardInterrupt

    original = Path.cwd()
    monkeypatch.setattr(serve, "ThreadedHTTPServer", FakeServer)
    monkeypatch.setattr(serve.webbrowser, "open", lambda url: None)
    monkeypatch.setattr("sys.argv", ["stormhub-server", str(tmp_path)])
    serve.main()
    assert seen == {"address": ("127.0.0.1", 5000), "directory": str(tmp_path.resolve()), "closed": True}
    assert Path.cwd() == original
    monkeypatch.setattr("sys.argv", ["stormhub-server", str(tmp_path), "0.0.0.0"])
    with pytest.raises(SystemExit, match="2"):
        serve.main()
    monkeypatch.setattr("sys.argv", ["stormhub-server", str(tmp_path), "0.0.0.0", "--allow-network"])
    serve.main()
    assert seen["address"] == ("0.0.0.0", 5000)
