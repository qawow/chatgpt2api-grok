"""Grok egress: transient TLS blips must be retried on the same egress.

Measured 2026-10-19 on the remote: Grok refresh failed with

    refresh network error: HTTPSConnectionPool(host='auth.x.ai', port=443):
    Read timed out. (read timeout=29.6...)

while the proxy itself answered ``auth.x.ai/oauth2/token`` in 0.4s. The cause
was the fallback chain, not the proxy:

1. the SOCKS pool drops ~25-50% of *new* TLS handshakes with
   ``SSLEOFError: UNEXPECTED_EOF_WHILE_READING`` (reusing a keep-alive
   connection succeeded 8/8);
2. urllib3 wraps that blip as "Max retries exceeded", which
   ``_is_egress_connect_error`` matches, so the blip was treated as a dead
   egress — blacklisted and abandoned;
3. the next candidate is ``direct``, which has no route here, so it hung for
   the whole 30s budget and its ``Read timed out`` masked the real error.

These tests pin the retry and the error attribution.
"""
from __future__ import annotations

import unittest
from unittest import mock

import requests

from services import grok_backend_api as grok


class _EgressPatchMixin:
    """Patch helper that never calls ``mock.patch.stopall``.

    ``stopall`` unwinds *every* active patch, including ones other test modules
    started, so using it in a setUp leaks state across the suite.
    """

    def _patch(self, target, *args, **kwargs):
        patcher = mock.patch(target, *args, **kwargs)
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher

    def _patch_object(self, target, attribute, *args, **kwargs):
        patcher = mock.patch.object(target, attribute, *args, **kwargs)
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher


def _tls_eof() -> requests.exceptions.SSLError:
    """The exact wrapper urllib3 produces for a dropped TLS handshake."""
    return requests.exceptions.SSLError(
        "SOCKSHTTPSConnectionPool(host='auth.x.ai', port=443): Max retries exceeded "
        "with url: /oauth2/token (Caused by SSLError(SSLEOFError(8, "
        "'[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of "
        "protocol (_ssl.c:1032)')))"
    )


def _direct_timeout() -> requests.exceptions.ReadTimeout:
    return requests.exceptions.ReadTimeout(
        "HTTPSConnectionPool(host='auth.x.ai', port=443): Read timed out. "
        "(read timeout=29.6)"
    )


class _FakeResponse:
    def __init__(self, status_code: int = 400, text: str = '{"error":"invalid_grant"}'):
        self.status_code = status_code
        self.text = text

    def json(self):
        import json as _json

        return _json.loads(self.text)


class TransientTlsRetryTest(_EgressPatchMixin, unittest.TestCase):
    """Same-egress retry for a dropped handshake."""

    def setUp(self) -> None:
        patcher = mock.patch.object(grok, "_http")
        self.http = patcher.start()
        self.addCleanup(patcher.stop)
        # One proxied candidate then direct, like the real deployment.
        self.candidates = [("global", "socks5h://u:p@proxy.local:1080"), ("direct", "")]
        self._patch(
            "services.proxy_service.proxy_settings.list_egress_candidates",
            return_value=self.candidates,
        )
        self._patch(
            "services.proxy_service.proxy_settings.get_profile",
            return_value=mock.Mock(runtime_enabled=False, skip_ssl_verify=False,
                                   proxy_url=self.candidates[0][1], proxy_source="global"),
        )
        # Never let a test touch the real blacklist.
        self._patch("services.proxy_service.mark_egress_unusable")
        self._patch_object(grok.time, "sleep")

    def _side_effect(self, *results):
        """Return a callable yielding ``results`` in order, then the last one."""
        calls = {"n": 0}

        def run(*args, **kwargs):
            idx = min(calls["n"], len(results) - 1)
            calls["n"] += 1
            item = results[idx]
            if isinstance(item, Exception):
                raise item
            return item

        return run, calls

    def test_a_dropped_handshake_is_retried_and_succeeds(self):
        run, calls = self._side_effect(_tls_eof(), _tls_eof(), _FakeResponse())
        self.http.return_value.post.side_effect = run

        resp = grok._request("post", "https://auth.x.ai/oauth2/token", timeout=30.0)

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(calls["n"], 3, "should retry the same egress twice")

    def test_a_single_attempt_would_have_failed(self):
        """Guard: without retries the first blip must not be the final answer."""
        run, calls = self._side_effect(_tls_eof(), _FakeResponse())
        self.http.return_value.post.side_effect = run

        resp = grok._request("post", "https://auth.x.ai/oauth2/token", timeout=30.0)

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(calls["n"], 2)

    def test_direct_is_not_retried(self):
        """A blocked direct route gets one shot, not three."""
        run, calls = self._side_effect(_tls_eof(), _tls_eof(), _tls_eof(), _direct_timeout())
        self.http.return_value.post.side_effect = run

        with self.assertRaises(requests.exceptions.RequestException):
            grok._request("post", "https://auth.x.ai/oauth2/token", timeout=30.0)

        self.assertEqual(calls["n"], 4, "3 proxy attempts + 1 direct attempt")

    def test_the_proxy_error_is_reported_not_the_direct_timeout(self):
        """The direct fallback's timeout must not mask the real cause."""
        run, _ = self._side_effect(_tls_eof(), _tls_eof(), _tls_eof(), _direct_timeout())
        self.http.return_value.post.side_effect = run

        with self.assertRaises(requests.exceptions.RequestException) as ctx:
            grok._request("post", "https://auth.x.ai/oauth2/token", timeout=30.0)

        message = str(ctx.exception)
        self.assertIn("UNEXPECTED_EOF_WHILE_READING", message)
        self.assertNotIn("Read timed out", message)

    def test_a_non_connect_error_is_raised_immediately(self):
        """A 4xx-style local failure is not an egress problem — no retry."""
        run, calls = self._side_effect(requests.exceptions.InvalidURL("bad url"))
        self.http.return_value.post.side_effect = run

        with self.assertRaises(requests.exceptions.InvalidURL):
            grok._request("post", "https://auth.x.ai/oauth2/token", timeout=30.0)
        self.assertEqual(calls["n"], 1)

    def test_a_genuine_connect_error_still_falls_back(self):
        """A refused proxy is really dead — fall back and let direct answer."""
        refused = requests.exceptions.ProxyError(
            "Cannot connect to proxy.", ConnectionRefusedError(111, "Connection refused")
        )
        run, calls = self._side_effect(refused, _FakeResponse(200, "{}"))
        self.http.return_value.post.side_effect = run

        resp = grok._request("post", "https://auth.x.ai/oauth2/token", timeout=30.0)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(calls["n"], 2, "no same-egress retry for a refused proxy")


class RefreshUsesTheRetryTest(_EgressPatchMixin, unittest.TestCase):
    """``refresh_access_token`` must survive a dropped handshake.

    Exercises the real ``_request`` (only the HTTP session is faked), so this
    fails if the retry is ever moved out of the egress walk.
    """

    def setUp(self) -> None:
        self._patch(
            "services.proxy_service.proxy_settings.list_egress_candidates",
            return_value=[("global", "socks5h://u:p@proxy.local:1080"), ("direct", "")],
        )
        self._patch(
            "services.proxy_service.proxy_settings.get_profile",
            return_value=mock.Mock(runtime_enabled=False, skip_ssl_verify=False,
                                   proxy_url="socks5h://u:p@proxy.local:1080",
                                   proxy_source="global"),
        )
        self._patch("services.proxy_service.mark_egress_unusable")
        self._patch_object(grok.time, "sleep")

    def test_refresh_recovers_from_a_transient_blip(self):
        calls = {"n": 0}

        def post(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _tls_eof()
            return _FakeResponse(200, '{"access_token":"at","refresh_token":"rt","expires_in":3600}')

        with mock.patch.object(grok, "_http") as http:
            http.return_value.post.side_effect = post
            data = grok.refresh_access_token("rt-fixture")

        self.assertEqual(data["access_token"], "at")
        self.assertEqual(calls["n"], 2, "the blip must be retried, not surfaced")

    def test_refresh_reports_the_proxy_cause_when_everything_fails(self):
        def post(*args, **kwargs):
            raise _tls_eof()

        with mock.patch.object(grok, "_http") as http:
            http.return_value.post.side_effect = post
            with self.assertRaises(grok.GrokBackendError) as ctx:
                grok.refresh_access_token("rt-fixture")

        message = str(ctx.exception)
        self.assertIn("refresh network error", message)
        self.assertIn("UNEXPECTED_EOF_WHILE_READING", message)


class TransientClassifierTest(unittest.TestCase):
    """Two tiers: handshake EOF is always safe to replay; ambiguous socket
    deaths only for idempotent methods, so a POST that rotated a single-use
    credential can never be replayed with the now-stale value."""

    RESET = "('Connection aborted.', ConnectionResetError(104, 'Connection reset by peer'))"

    def test_handshake_death_is_transient_for_any_method(self):
        from services.proxy_service import is_transient_tls_error

        self.assertTrue(is_transient_tls_error(str(_tls_eof()), method="post"))
        self.assertTrue(is_transient_tls_error(str(_tls_eof()), method="get"))

    def test_ambiguous_socket_death_is_only_retried_when_idempotent(self):
        from services.proxy_service import is_transient_tls_error

        self.assertFalse(is_transient_tls_error(self.RESET, method="post"))
        self.assertTrue(is_transient_tls_error(self.RESET, method="get"))
        self.assertTrue(is_transient_tls_error(self.RESET, method="head"))
        self.assertTrue(is_transient_tls_error(self.RESET, idempotent=True))

    def test_real_egress_failures_are_not_transient(self):
        from services.proxy_service import is_transient_tls_error

        self.assertFalse(is_transient_tls_error(str(_direct_timeout()), method="post"))
        self.assertFalse(is_transient_tls_error(
            "Cannot connect to proxy. Connection refused", method="get"
        ))
        self.assertFalse(is_transient_tls_error(
            "curl: (97) User was rejected by the SOCKS5 server", method="get"
        ))
        self.assertFalse(is_transient_tls_error("Name or service not known", method="get"))


if __name__ == "__main__":
    unittest.main()


class PostIsNotReplayedOnAmbiguousFailuresTest(_EgressPatchMixin, unittest.TestCase):
    """A POST whose socket died at an unknown stage must not be replayed.

    The refresh endpoint mints a new access token; if the first attempt was
    actually processed and only its response was lost, replaying the request
    with the same (possibly single-use) refresh_token would surface
    ``invalid_grant`` — which now marks the account 异常. Only handshake-level
    failures, where the request provably never left the client, are retried.
    """

    def setUp(self) -> None:
        self._patch(
            "services.proxy_service.proxy_settings.list_egress_candidates",
            return_value=[("global", "socks5h://u:p@proxy.local:1080")],
        )
        self._patch(
            "services.proxy_service.proxy_settings.get_profile",
            return_value=mock.Mock(runtime_enabled=False, skip_ssl_verify=False,
                                   proxy_url="socks5h://u:p@proxy.local:1080",
                                   proxy_source="global"),
        )
        self._patch("services.proxy_service.mark_egress_unusable")
        self._patch_object(grok.time, "sleep")

    def test_a_reset_post_is_attempted_once(self):
        reset = requests.exceptions.ConnectionError(
            "('Connection aborted.', ConnectionResetError(104, 'Connection reset by peer'))"
        )
        calls = {"n": 0}

        def post(*args, **kwargs):
            calls["n"] += 1
            raise reset

        with mock.patch.object(grok, "_http") as http:
            http.return_value.post.side_effect = post
            with self.assertRaises(requests.exceptions.RequestException):
                grok._request("post", "https://auth.x.ai/oauth2/token", timeout=30.0)

        self.assertEqual(calls["n"], 1, "an ambiguous POST failure must not be replayed")

    def test_a_reset_get_is_retried(self):
        reset = requests.exceptions.ConnectionError(
            "('Connection aborted.', ConnectionResetError(104, 'Connection reset by peer'))"
        )
        calls = {"n": 0}

        def get(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] < 2:
                raise reset
            return _FakeResponse(200, "{}")

        with mock.patch.object(grok, "_http") as http:
            http.return_value.get.side_effect = get
            resp = grok._request("get", "https://cli-chat-proxy.grok.com/v1/models",
                                 timeout=30.0)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(calls["n"], 2)
