"""Offline HTTP transport: only explicitly registered, unmodified recordings."""

import io
from pathlib import Path
from urllib.parse import urlsplit

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "github"


class RecordedGitHub:
    """Replace a real GitHub client's opener, leaving request/parsing code intact.

    Routes use exact HTTP methods and API paths, including query strings.
    Authentication is synthetic and preverified; no identity response is invented.
    Every open is handled here, so even an unregistered URL cannot reach a network.
    """

    def __init__(self, client, routes):
        self.routes = dict(routes)
        self.calls = []
        client.opener = self
        client.identity_verified = True

    def open(self, request, timeout=30):
        url = urlsplit(request.full_url)
        path = url.path + ("?" + url.query if url.query else "")
        key = (request.get_method(), path)
        self.calls.append(key)
        if url.scheme != "https" or url.netloc != "api.github.com" or key not in self.routes:
            pytest.fail(f"No GitHub recording for {key[0]} {path}")
        return io.BytesIO((FIXTURES / self.routes[key]).read_bytes())
