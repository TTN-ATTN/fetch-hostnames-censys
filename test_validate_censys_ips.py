"""Offline tests for validated IP/hostname output; no external connections."""

import contextlib
import io
import http.client
import json
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import validate_censys_ips as validator


SITE = b'''<!doctype html><html><head><title>Example Support Portal</title></head>
<body><h1>Example Support Portal</h1><p>Welcome to our customer support center.
Open a support request, check an existing ticket, or read the documentation
for your account and services. Our support team is available to help you.</p></body></html>'''
OTHER_SITE = b'''<!doctype html><html><head><title>Example Support Portal</title></head>
<body><h1>Example Support Portal</h1><p>This is a completely unrelated parking
website. Buy this domain now. Browse advertising, promotions, auction listings,
and sponsored links offered by a different organization.</p></body></html>'''
DEFAULT_SITE = b'''<html><head><title>Welcome to nginx!</title></head><body>
<h1>Welcome to nginx!</h1><p>If you see this page, the nginx web server is
successfully installed and working. Further configuration is required.
For online documentation and support please refer to nginx.org.</p></body></html>'''


class Response:
    def __init__(self, body=SITE, status=200, headers=None):
        self.status = status
        self.body = io.BytesIO(body)
        self.headers = {"content-type": "text/html; charset=utf-8", **(headers or {})}

    def getheader(self, key, default=None):
        return self.headers.get(key.lower(), default)

    def read(self, amount=None):
        return self.body.read(amount)

    def close(self):
        self.body.close()


class FakeSocket:
    def __init__(self, host, address):
        self.host, self.address = host, address

    def getpeername(self):
        return (self.address or "9.9.9.9", 443)

    def getpeercert(self):
        return {"subject": ((('commonName', self.host),),),
                "subjectAltName": (("DNS", self.host),)}


class MappingTests(unittest.TestCase):
    def run_mapping(self, replies, *, hosts=None, ips=None, extra=(), existing=None,
                    second_shutdown_interrupt=False):
        """Replace only the external HTTPS transport and DNS dependency."""
        hosts = hosts or ["support.example.com"]
        ips = ips or ["8.8.8.8", "1.1.1.1"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hosts_path, ips_path = root / "hosts.txt", root / "IP.txt"
            hosts_path.write_text("\n".join(hosts) + "\n")
            ips_path.write_text("\n".join(ips) + "\n")
            output = root / "valid_hosts.txt"
            if existing is not None:
                output.write_text(existing)

            class Connection:
                def __init__(self, hostname, address=None, timeout=5, **kwargs):
                    self.host, self.address = hostname, address
                    self.sock = FakeSocket(hostname, address)
                    self.path = "/"
                    self.sent_host = hostname

                def connect(self):
                    result = replies[(self.host, self.address, self.path)]
                    if isinstance(result, BaseException):
                        raise result

                def request(self, method, path, body=None, headers=None, **kwargs):
                    self.path = path
                    self.sent_host = (headers or {}).get("Host", self.host)
                    if method != "GET":
                        raise AssertionError("mapping must compare website content using GET")

                def getresponse(self):
                    result = replies[(self.sent_host, self.address, self.path)]
                    if isinstance(result, BaseException):
                        raise result
                    if "wire" in result:
                        class WireSocket:
                            def makefile(self, *args):
                                return io.BytesIO(result["wire"])
                        response = http.client.HTTPResponse(WireSocket())
                        response.begin()
                        return response
                    return Response(**result)

                def close(self):
                    pass

            def reference_connection(host, port=443, **kwargs):
                return Connection(host, None, **kwargs)

            argv = ["validate_censys_ips.py", "--hosts", str(hosts_path),
                    "--ips", str(ips_path), "--output", str(output),
                    "--delay", "0.1", *extra]
            stream = io.StringIO()
            shutdown = validator.ThreadPoolExecutor.shutdown

            def interrupt_before_uncheckpointed_shutdown(executor, *args, **kwargs):
                if second_shutdown_interrupt and not output.exists():
                    shutdown(executor, wait=False, cancel_futures=True)
                    raise KeyboardInterrupt()
                return shutdown(executor, *args, **kwargs)

            with patch.object(validator.sys, "argv", argv), \
                    patch.object(validator, "PinnedHTTPSConnection", Connection), \
                    patch.object(validator.http.client, "HTTPSConnection", reference_connection), \
                    patch.object(validator.socket, "getaddrinfo", return_value=[
                        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]), \
                    patch.object(validator.ThreadPoolExecutor, "shutdown", interrupt_before_uncheckpointed_shutdown), \
                    contextlib.redirect_stderr(stream), contextlib.redirect_stdout(io.StringIO()):
                try:
                    code = validator.main()
                except KeyboardInterrupt:
                    code = -99
            manifest = output.with_suffix(".manifest.json")
            details = output.with_suffix(".details.json")
            progress = output.with_suffix(".progress.log")
            return {
                "code": code,
                "output": output.read_text() if output.exists() else None,
                "manifest": json.loads(manifest.read_text()) if manifest.exists() else None,
                "details": json.loads(details.read_text()) if details.exists() else None,
                "progress": [json.loads(line) for line in progress.read_text().splitlines()]
                if progress.exists() else [],
            }

    def replies(self, candidate=None, reference=None):
        return {
            ("support.example.com", None, "/"): reference or {"body": SITE},
            ("support.example.com", "8.8.8.8", "/"): candidate or {"body": SITE},
            ("support.example.com", "1.1.1.1", "/"): {"body": OTHER_SITE},
        }

    def test_hosts_mode_defaults_to_direct_validation_and_hosts_format(self):
        args = validator.parse_args(["--hosts", "hosts.txt", "--ips", "IP.txt"])
        self.assertEqual(args.mode, "mapping")
        self.assertEqual(args.format, "hosts")

    def test_single_hostname_legacy_head_mode_remains_available(self):
        args = validator.parse_args(["--domain", "Example.COM.", "ips.txt"])
        self.assertEqual((args.mode, args.domain, args.format), ("https", "example.com", "csv"))

    def test_only_matching_website_is_written_not_dns_match_or_same_title(self):
        result = self.run_mapping(self.replies())
        self.assertEqual(result["code"], 0)
        self.assertEqual(result["output"], "8.8.8.8 support.example.com\n")

    def test_matching_ip_need_not_be_present_in_dns(self):
        replies = self.replies(candidate={"body": OTHER_SITE})
        replies[("support.example.com", "1.1.1.1", "/")] = {"body": SITE}
        result = self.run_mapping(replies)
        self.assertEqual(result["output"], "1.1.1.1 support.example.com\n")

    def test_tls_failure_is_not_a_valid_pair(self):
        result = self.run_mapping(self.replies(candidate=ssl.SSLCertVerificationError("wrong hostname")))
        self.assertEqual(result["output"], "")

    def test_identical_generic_nginx_pages_are_not_valid(self):
        result = self.run_mapping(self.replies(candidate={"body": DEFAULT_SITE},
                                               reference={"body": DEFAULT_SITE}))
        self.assertEqual(result["output"], "")

    def test_identical_apache_centos_installation_test_pages_are_not_valid(self):
        body = b'<html><head><title>Apache HTTP Server Test Page powered by CentOS</title></head><body><h1>Testing 123..</h1><p>This page is used to test the proper operation of the Apache HTTP server after it has been installed. If you can read this page it means that this site is working properly.</p></body></html>'
        result = self.run_mapping(self.replies(candidate={"body": body}, reference={"body": body}))
        self.assertEqual(result["output"], "")

    def test_identical_error_pages_are_not_valid(self):
        result = self.run_mapping(self.replies(candidate={"body": SITE, "status": 403},
                                               reference={"body": SITE, "status": 403}))
        self.assertEqual(result["output"], "")

    def test_http_200_error_body_with_normal_title_is_not_valid(self):
        body = b'<html><head><title>Example Portal</title></head><body><h1>An error occurred</h1><p>The requested site is temporarily unavailable. Please contact the administrator or come back later. The service cannot process your request at this time.</p></body></html>'
        result = self.run_mapping(self.replies(candidate={"body": body}, reference={"body": body}))
        self.assertEqual(result["output"], "")

    def test_http_200_human_challenge_with_normal_title_is_not_valid(self):
        body = b'<html><head><title>Example Portal</title></head><body><h1>Verify you are human</h1><p>Please complete the security check before you can continue to access this service. Checking your browser helps us protect our website from automated requests.</p></body></html>'
        result = self.run_mapping(self.replies(candidate={"body": body}, reference={"body": body}))
        self.assertEqual(result["output"], "")

    def test_no_reference_is_unverified_not_a_certificate_only_match(self):
        result = self.run_mapping(self.replies(reference=socket.gaierror("no DNS answer")))
        self.assertEqual(result["output"], "")

    def test_premature_content_length_eof_is_not_a_valid_page_prefix(self):
        wire = b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 10000\r\nConnection: close\r\n\r\n' + SITE
        result = self.run_mapping(self.replies(candidate={"wire": wire}, reference={"wire": wire}))
        self.assertEqual(result["output"], "")

    def test_response_larger_than_cap_is_not_valid(self):
        body = SITE * 900
        result = self.run_mapping(self.replies(candidate={"body": body}, reference={"body": body}))
        self.assertEqual(result["output"], "")

    def test_dynamic_hidden_fields_do_not_hide_a_matching_website(self):
        baseline = SITE.replace(b"</body>", b'<input type="hidden" value="token-a"></body>')
        candidate = SITE.replace(b"</body>", b'<input type="hidden" value="token-b"></body>')
        result = self.run_mapping(self.replies(candidate={"body": candidate}, reference={"body": baseline}))
        self.assertEqual(result["output"], "8.8.8.8 support.example.com\n")

    def test_short_sso_login_page_with_real_application_structure_is_valid(self):
        body = b'''<html><head><title>Example - Authentication</title>
<script src="/app/login.js"></script><link href="/app/login.css"></head>
<body><form method="post" action="/front/login.php">
<input type="hidden" name="csrf" value="token-a"><h2>Login to your account</h2>
<a href="/front/login_google.php">Login with Google</a>
<a href="/front/login_microsoft.php">Login with Microsoft</a></form></body></html>'''
        candidate = body.replace(b'token-a', b'token-b')
        result = self.run_mapping(self.replies(candidate={"body": candidate}, reference={"body": body}))
        self.assertEqual(result["output"], "8.8.8.8 support.example.com\n")

    def test_short_generic_heading_with_asset_links_is_still_unverified(self):
        body = b'<html><head><title>Example Authentication</title><script src="/main.js"></script><link href="/style.css"></head><body><h1>Welcome to this example website</h1><div id="root"></div>' + b' ' * 200 + b'</body></html>'
        result = self.run_mapping(self.replies(candidate={"body": body}, reference={"body": body}))
        self.assertEqual(result["output"], "")

    def test_login_destination_query_distinguishes_applications(self):
        body = b'''<html><head><title>Example - Authentication</title>
<script src="/app/login.js"></script><link href="/app/login.css"></head>
<body><form method="post" action="/login?tenant=support">
<h2>Login to your account</h2>
<a href="/oauth?client_id=support">Login with account provider</a>
</form></body></html>'''
        for field in (b"tenant", b"client_id"):
            with self.subTest(field=field):
                candidate = body.replace(field + b"=support", field + b"=unrelated")
                result = self.run_mapping(self.replies(candidate={"body": candidate},
                                                       reference={"body": body}))
                self.assertEqual(result["output"], "")

    def test_identical_long_navigation_does_not_hide_different_page_content(self):
        common = b"shared navigation and site links " * 400
        reference = SITE.replace(b"</body>", common + b"account documentation " * 120 + b"</body>")
        candidate = SITE.replace(b"</body>", common + b"unrelated domain auction " * 120 + b"</body>")
        result = self.run_mapping(self.replies(candidate={"body": candidate}, reference={"body": reference}))
        self.assertEqual(result["output"], "")

    def test_same_visible_login_with_different_application_assets_is_not_valid(self):
        reference = SITE.replace(b"</head>", b'<script src="/assets/support.js"></script></head>')
        candidate = SITE.replace(b"</head>", b'<script src="/assets/unrelated.js"></script></head>')
        result = self.run_mapping(self.replies(candidate={"body": candidate}, reference={"body": reference}))
        self.assertEqual(result["output"], "")

    def test_navigation_only_with_different_inline_apps_is_not_valid(self):
        common = b'<html><head><title>Example Portal</title></head><body><nav>' + b'shared menu links ' * 20 + b'</nav>'
        reference = common + b'<script>app="support"</script></body></html>'
        candidate = common + b'<script>app="auction"</script></body></html>'
        result = self.run_mapping(self.replies(candidate={"body": candidate}, reference={"body": reference}))
        self.assertEqual(result["output"], "")

    def test_empty_application_shell_with_assets_is_not_valid(self):
        shell = b'<html><head><title>Example</title><script src="/main.js"></script><link href="/style.css"></head><body><div id="root"></div>' + b' ' * 300 + b'</body></html>'
        result = self.run_mapping(self.replies(candidate={"body": shell}, reference={"body": shell}))
        self.assertEqual(result["output"], "")

    def test_same_visible_content_but_different_inline_application_is_not_valid(self):
        reference = SITE.replace(b'</body>', b'<script>app="support"</script></body>')
        candidate = SITE.replace(b'</body>', b'<script>app="auction"</script></body>')
        result = self.run_mapping(self.replies(candidate={"body": candidate}, reference={"body": reference}))
        self.assertEqual(result["output"], "")

    def test_interruption_preserves_completed_valid_pairs(self):
        replies = self.replies()
        replies[("support.example.com", "1.1.1.1", "/")] = KeyboardInterrupt()
        result = self.run_mapping(replies, extra=("--workers", "1"))
        self.assertEqual(result["code"], 130)
        self.assertEqual(result["output"], "8.8.8.8 support.example.com\n")
        self.assertEqual(result["manifest"]["status"], "interrupted")
        self.assertEqual(result["manifest"]["completed"], 1)

    def test_repeated_interrupt_during_finalization_keeps_checkpointed_pairs(self):
        replies = self.replies()
        replies[("support.example.com", "1.1.1.1", "/")] = KeyboardInterrupt()
        result = self.run_mapping(replies, extra=("--workers", "1"), second_shutdown_interrupt=True)
        self.assertEqual(result["output"], "8.8.8.8 support.example.com\n")
        self.assertEqual(result["manifest"]["status"], "interrupted")

    def test_dns_only_is_an_explicit_separate_mode(self):
        args = validator.parse_args(["--hosts", "hosts.txt", "--ips", "IP.txt", "--dns-only"])
        self.assertEqual((args.mode, args.format), ("dns", "csv"))

    def test_same_hostname_https_redirect_is_compared_at_final_path(self):
        replies = self.replies()
        for address in (None, "8.8.8.8"):
            replies[("support.example.com", address, "/")] = {
                "body": b"", "status": 302, "headers": {"location": "/login"}}
            replies[("support.example.com", address, "/login")] = {"body": SITE}
        result = self.run_mapping(replies)
        self.assertEqual(result["output"], "8.8.8.8 support.example.com\n")

    def test_cross_hostname_redirect_is_not_followed_or_promoted(self):
        replies = self.replies(candidate={"body": b"", "status": 302,
                                         "headers": {"location": "https://unrelated.example.net/login"}},
                               reference={"body": b"", "status": 302,
                                          "headers": {"location": "https://unrelated.example.net/login"}})
        result = self.run_mapping(replies)
        self.assertEqual(result["output"], "")

    def test_existing_output_is_preserved(self):
        result = self.run_mapping(self.replies(), existing="keep this file\n")
        self.assertEqual(result["code"], 2)
        self.assertEqual(result["output"], "keep this file\n")

    def test_results_manifest_and_final_progress_agree(self):
        result = self.run_mapping(self.replies())
        self.assertIsNotNone(result["manifest"])
        self.assertEqual(result["manifest"]["status"], "completed")
        self.assertEqual(result["manifest"]["planned"], 2)
        self.assertEqual(result["manifest"]["completed"], 2)
        self.assertEqual(result["manifest"]["counts"]["valid"], 1)
        self.assertEqual(result["progress"][-1]["phase"], "completed")


class LocalTLSTests(unittest.TestCase):
    """Real TLS/HTTP over loopback, with a locally trusted test certificate."""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("openssl"):
            raise unittest.SkipTest("openssl is required for the local TLS fixture")
        cls.directory = tempfile.TemporaryDirectory()
        root = Path(cls.directory.name)
        cert, key = root / "cert.pem", root / "key.pem"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "1",
            "-subj", "/CN=support.example.com", "-addext", "subjectAltName=DNS:support.example.com",
        ], check=True, capture_output=True, timeout=15)
        cls.client_context = ssl.create_default_context(cafile=str(cert))

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if getattr(self.server, "slow", False):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", "10000")
                    self.end_headers()
                    try:
                        for _ in range(60):
                            self.wfile.write(b"x")
                            self.wfile.flush()
                            threading.Event().wait(0.03)
                    except OSError:
                        pass
                    return
                body = SITE if self.headers.get("Host") == "support.example.com" else OTHER_SITE
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.directory.cleanup()

    def fetch(self, hostname, timeout=2):
        original = socket.create_connection

        def loopback_only(destination, timeout=None, source_address=None, **kwargs):
            if destination != ("8.8.8.8", 443):
                raise AssertionError("candidate IP was not pinned")
            return original(("127.0.0.1", self.server.server_port), timeout, source_address)

        with patch.object(validator.socket, "create_connection", loopback_only), \
                patch.object(validator.ssl, "create_default_context", return_value=self.client_context):
            return validator.fetch_website(hostname, "8.8.8.8", timeout, validator.RequestLimiter(0.02))

    def test_real_pinned_https_uses_hostname_for_sni_and_http_host(self):
        row = self.fetch("support.example.com")
        self.assertTrue(row["tls_hostname_verified"])
        self.assertEqual(row["title"], "example support portal")
        self.assertTrue(validator.usable_website(row))

    def test_real_wrong_hostname_certificate_is_rejected(self):
        row = self.fetch("wrong.example.com")
        self.assertFalse(row["tls_hostname_verified"])
        self.assertEqual(row["classification"], "tls-verification-failed")
        self.assertFalse(validator.usable_website(row))

    def test_slow_dripping_body_cannot_extend_the_request_deadline(self):
        self.server.slow = True
        try:
            row = self.fetch("support.example.com", timeout=0.2)
        finally:
            self.server.slow = False
        self.assertEqual(row["classification"], "timeout")
        self.assertLess(row["elapsed_ms"], 1000)


if __name__ == "__main__":
    unittest.main()
