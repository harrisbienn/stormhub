"""Preview a trusted local STAC directory; this is not a deployment server."""

import argparse
import html
import io
import ipaddress
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote


class CORSRequestHandler(SimpleHTTPRequestHandler):
    """Serve files confined to a trusted directory, including resolved links."""

    def end_headers(self):
        """Allow the hosted STAC browser to read this local preview."""
        self.send_header("Access-Control-Allow-Origin", "https://radiantearth.github.io")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def _within_root(self, path: Path) -> bool:
        try:
            return path.resolve().is_relative_to(Path(self.directory).resolve())
        except (OSError, RuntimeError, ValueError):
            return False

    def send_head(self):
        """Reject external symlinks/junctions before the standard handler opens them."""
        path = Path(self.translate_path(self.path))
        if not self._within_root(path):
            self.send_error(403, "Path is outside the preview directory")
            return None
        # The base handler also opens directory indexes; check those separately.
        if path.is_dir():
            for name in ("index.html", "index.htm"):
                candidate = path / name
                if not self._within_root(candidate):
                    self.send_error(403, "Index is outside the preview directory")
                    return None
                if candidate.is_file():
                    break
        return super().send_head()

    def list_directory(self, path):
        """Escape display text and encode URLs; omit links outside the root."""
        try:
            entries = sorted(Path(path).iterdir(), key=lambda entry: entry.name.lower())
        except OSError:
            self.send_error(404, "Cannot list directory")
            return None
        display = html.escape(unquote(self.path), quote=True)
        rows = [
            f"<!doctype html><html><head><meta charset='utf-8'><title>{display}</title></head>",
            f"<body><h1>Local preview: {display}</h1><ul>",
        ]
        for entry in entries:
            if not self._within_root(entry):
                continue
            name = entry.name + ("/" if entry.is_dir() else "")
            href = quote(name, safe="/", errors="surrogatepass")
            rows.append(f'<li><a href="{href}">{html.escape(name, quote=True)}</a></li>')
        rows.append("</ul><p>Stop the server with Ctrl+C in its terminal.</p></body></html>")
        body = "\n".join(rows).encode("utf-8", "surrogateescape")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        return io.BytesIO(body)

    def do_OPTIONS(self):
        """Advertise read-only preview methods."""
        self.send_response(204)
        self.send_header("Allow", "GET, HEAD, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()


class ThreadedHTTPServer(ThreadingHTTPServer):
    """Retain the existing server import with standard daemon request threads."""


def main(mkdir: bool = True):
    """Start a loopback preview; require explicit consent for network exposure."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("host", nargs="?", default="127.0.0.1")
    parser.add_argument("port", nargs="?", type=int, default=5000)
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="Allow a non-loopback bind on a trusted network (no authentication)",
    )
    args = parser.parse_args()
    try:
        loopback = ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        loopback = args.host.lower() == "localhost"
    if not loopback and not args.allow_network:
        parser.error("Non-loopback hosts require --allow-network; this preview has no authentication")
    root = args.directory.resolve()
    if mkdir:
        root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        parser.error("The preview directory must exist and be a directory")
    handler = partial(CORSRequestHandler, directory=str(root))
    with ThreadedHTTPServer((args.host, args.port), handler) as server:
        print(f"Serving '{root}' on {args.host}:{server.server_port}; stop with Ctrl+C")
        webbrowser.open(f"http://127.0.0.1:{server.server_port}/")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
