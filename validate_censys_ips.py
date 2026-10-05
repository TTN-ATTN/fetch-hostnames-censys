#!/usr/bin/env python3
"""Find candidate IPs that serve the same HTTPS website as each hostname.

Mapping: python validate_censys_ips.py --hosts hosts.txt --ips IP.txt
         --output valid_hosts.txt
DNS only: python validate_censys_ips.py --hosts hosts.txt --ips IP.txt --dns-only
Legacy TLS/HEAD: python validate_censys_ips.py --domain example.com ips.txt

Mapping connects directly to candidate IPs on port 443, preserving the
hostname in TLS SNI and HTTP Host. It compares bounded GET responses with
one reference website per hostname. Only certificate-verified, matching
websites are written as IP hostname lines. DNS equality is not required.
Errors, default/error pages, and unavailable references are never promoted.
Same-host HTTPS redirects are bounded; cross-host redirects are not followed.

--dns-only retains system-resolver IPv4/IPv6 comparison without IP probes.
Legacy --domain performs TLS verification and HEAD without body comparison.

Inputs contain one hostname or IP per line. Results default to unique files
under /tmp. No Censys requests, retries, authentication, or /etc/hosts edits.
Matching a website does not establish exclusive ownership or origin hosting.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import signal
import socket
import ssl
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit


CSV_FIELDS = (
    "domain",
    "ip",
    "peer_ip",
    "tls_hostname_verified",
    "certificate_common_name",
    "certificate_dns_sans",
    "http_status",
    "location",
    "server",
    "elapsed_ms",
    "classification",
    "error",
)
DNS_CSV_FIELDS = (
    "hostname",
    "resolved_ip",
    "record_type",
    "in_ip_list",
    "status",
    "resolved_at_utc",
    "elapsed_ms",
    "error",
)


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Use a candidate IP for TCP while retaining hostname-based TLS SNI."""

    def __init__(self, hostname: str, address: str, timeout: float) -> None:
        super().__init__(hostname, port=443, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self) -> None:
        raw_socket = socket.create_connection((self.address, self.port), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
        except Exception:
            raw_socket.close()
            raise


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_domain(value: str) -> str:
    value = value.strip().rstrip(".")
    try:
        hostname = value.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("--domain must be a valid DNS hostname") from exc
    labels = hostname.split(".")
    if (
        len(hostname) > 253
        or len(labels) < 2
        or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in labels
        )
    ):
        raise ValueError("--domain must be a valid DNS hostname")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return hostname
    raise ValueError("--domain must be a DNS hostname, not an IP address")


def load_ips(
    input_name: str | Path, *, public_only: bool = True
) -> tuple[list[str], list[dict[str, Any]], int]:
    if input_name == "-":
        lines = sys.stdin.read().splitlines()
    else:
        lines = Path(input_name).read_text(encoding="utf-8").splitlines()

    addresses: list[str] = []
    seen: set[str] = set()
    rejected: list[dict[str, Any]] = []
    duplicates = 0

    for line_number, line in enumerate(lines, start=1):
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            rejected.append({"line": line_number, "reason": "not an IP address"})
            continue
        if public_only and not address.is_global:
            rejected.append({"line": line_number, "reason": "IP is not globally routable"})
            continue
        normalized = str(address)
        if normalized in seen:
            duplicates += 1
            continue
        seen.add(normalized)
        addresses.append(normalized)
    return addresses, rejected, duplicates


def load_hosts(input_path: Path) -> tuple[list[str], list[dict[str, Any]], int]:
    hosts: list[str] = []
    seen: set[str] = set()
    rejected: list[dict[str, Any]] = []
    duplicates = 0
    for line_number, line in enumerate(
        input_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        try:
            hostname = normalize_domain(value)
        except ValueError:
            rejected.append({"line": line_number, "reason": "not a DNS hostname"})
            continue
        if hostname in seen:
            duplicates += 1
            continue
        seen.add(hostname)
        hosts.append(hostname)
    return hosts, rejected, duplicates


def resolve_host(hostname: str, candidate_ips: set[str]) -> dict[str, Any]:
    """Resolve a hostname through the OS resolver without probing any IP."""
    started = time.monotonic()
    row: dict[str, Any] = {
        "hostname": hostname,
        "resolved_ips": [],
        "matched_ips": [],
        "status": "dns-error",
        "resolved_at_utc": utc_now(),
        "elapsed_ms": 0,
        "error": "",
    }
    try:
        answers = socket.getaddrinfo(
            hostname, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
        )
        addresses: dict[str, None] = {}
        for family, _type, _protocol, _canonical_name, sockaddr in answers:
            if family in (socket.AF_INET, socket.AF_INET6):
                addresses[str(ipaddress.ip_address(sockaddr[0]))] = None
        row["resolved_ips"] = list(addresses)
        row["matched_ips"] = [address for address in addresses if address in candidate_ips]
        row["status"] = (
            "dns-match" if row["matched_ips"]
            else "dns-not-in-list" if addresses
            else "dns-not-found"
        )
    except socket.gaierror as exc:
        not_found_codes = {socket.EAI_NONAME}
        if hasattr(socket, "EAI_NODATA"):
            not_found_codes.add(socket.EAI_NODATA)
        row["status"] = "dns-not-found" if exc.errno in not_found_codes else "dns-error"
        row["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
    except (socket.timeout, TimeoutError) as exc:
        row["status"] = "timeout"
        row["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
    except (OSError, ValueError) as exc:
        row["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
    finally:
        row["resolved_at_utc"] = utc_now()
        row["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return row


def common_name(cert: dict[str, Any]) -> str:
    for rdn in cert.get("subject", ()):
        for key, value in rdn:
            if key == "commonName":
                return value
    return ""


def redact_location(value: str | None) -> str:
    if not value:
        return ""
    parsed = urlsplit(value)
    return urlunsplit(parsed._replace(query="", fragment=""))


def probe_ip(address: str, domain: str, timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    row: dict[str, Any] = {
        "domain": domain,
        "ip": address,
        "peer_ip": "",
        "tls_hostname_verified": False,
        "certificate_common_name": "",
        "certificate_dns_sans": [],
        "http_status": None,
        "location": "",
        "server": "",
        "elapsed_ms": 0,
        "classification": "connection-error",
        "error": "",
    }
    connection: PinnedHTTPSConnection | None = None

    try:
        connection = PinnedHTTPSConnection(domain, address, timeout)
        connection.connect()
        row["tls_hostname_verified"] = True
        cert = connection.sock.getpeercert() if connection.sock else {}
        row["certificate_common_name"] = common_name(cert)
        row["certificate_dns_sans"] = [
            name for kind, name in cert.get("subjectAltName", ()) if kind == "DNS"
        ]
        if connection.sock:
            row["peer_ip"] = connection.sock.getpeername()[0]

        # HEAD is read-only and does not download the page body.
        connection.request(
            "HEAD",
            "/",
            headers={
                "User-Agent": "censys-ip-validator/1.0",
                "Accept": "*/*",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        row["http_status"] = response.status
        row["location"] = redact_location(response.getheader("Location"))
        row["server"] = (response.getheader("Server") or "")[:200]
        row["classification"] = (
            "tls-match-redirect"
            if 300 <= response.status < 400
            else "tls-match-http-response"
        )
    except (socket.timeout, TimeoutError) as exc:
        row["classification"] = "timeout"
        row["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
    except ssl.SSLCertVerificationError as exc:
        row["classification"] = "tls-verification-failed"
        row["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
    except ssl.SSLError as exc:
        row["classification"] = "tls-error"
        row["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
    except (OSError, http.client.HTTPException) as exc:
        row["classification"] = "connection-error"
        row["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
    finally:
        if connection is not None:
            connection.close()
        row["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return row


def summarize(results: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "success": sum(row["tls_hostname_verified"] and row["http_status"] is not None for row in results),
        "http": sum(row["http_status"] is not None for row in results),
        "error": sum(bool(row["error"]) and row["classification"] != "timeout" for row in results),
        "timeout": sum(row["classification"] == "timeout" for row in results),
    }


def summarize_dns(results: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "success": sum(bool(row["resolved_ips"]) for row in results),
        "http": 0,
        "error": sum(bool(row["error"]) and row["status"] != "timeout" for row in results),
        "timeout": sum(row["status"] == "timeout" for row in results),
        "matched_hosts": sum(bool(row["matched_ips"]) for row in results),
        "matched_pairs": sum(len(row["matched_ips"]) for row in results),
    }


class WebsiteParser(HTMLParser):
    """Ignore executable/style/hidden-field data when comparing visible pages."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.content: list[str] = []
        self.title: list[str] = []
        self.stack: list[tuple[str, bool, bool]] = []
        self.inline_scripts: list[str] = []
        self.assets: set[str] = set()
        self.forms = 0
        self.password_inputs = 0
        self.user_inputs = 0
        self.login_links = 0
        self.controls: list[tuple[str, ...]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        hidden = "hidden" in attributes or "display:none" in (attributes.get("style") or "").replace(" ", "").casefold()
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append((tag, tag in {"script", "style"} or hidden,
                               tag in {"head", "nav", "header", "footer", "aside"}
                               or attributes.get("role") in {"navigation", "banner", "contentinfo"}))
        asset = attributes.get("src") if tag == "script" else attributes.get("href") if tag == "link" else None
        if asset:
            path = urlsplit(asset).path
            if path.endswith((".js", ".css")):
                self.assets.add(path)
        if not any(ignored or chrome for _, ignored, chrome in self.stack):
            if tag == "form":
                self.forms += 1
                # Query parameters can identify a tenant/application. Compare the
                # complete destination by hash without retaining raw query values.
                destination_hash = hashlib.sha256((attributes.get("action") or "").encode()).hexdigest()
                self.controls.append(("form", destination_hash,
                                      (attributes.get("method") or "get").casefold()))
            elif tag == "input":
                kind = (attributes.get("type") or "text").casefold()
                if kind != "hidden":
                    self.controls.append(("input", kind))
                    self.password_inputs += kind == "password"
                    self.user_inputs += kind in {"text", "email"}
            elif tag == "a":
                destination = attributes.get("href") or ""
                if re.search(r"(?:login|signin|sign-in|oauth|sso|auth)", urlsplit(destination).path, re.I):
                    self.login_links += 1
                    self.controls.append(("auth-link", hashlib.sha256(destination.encode()).hexdigest()))

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, value: str) -> None:
        if any(tag == "script" for tag, _, _ in self.stack):
            self.inline_scripts.append(value)
        if not any(ignored for _, ignored, _ in self.stack):
            self.text.append(value)
            if any(tag == "title" for tag, _, _ in self.stack):
                self.title.append(value)
            if not any(chrome for _, _, chrome in self.stack):
                self.content.append(value)


class RequestLimiter:
    """One shared request-start budget across all workers and redirects."""

    def __init__(self, delay: float, stop: threading.Event | None = None) -> None:
        self.delay = delay
        self.stop = stop
        self.lock = threading.Lock()
        self.last_start = 0.0

    def wait(self) -> None:
        with self.lock:
            if self.stop is not None and self.stop.is_set():
                raise InterruptedError("validation stopped")
            gap = self.delay - (time.monotonic() - self.last_start)
            if self.last_start and gap > 0:
                if self.stop is None:
                    time.sleep(gap)
                elif self.stop.wait(gap):
                    raise InterruptedError("validation stopped")
            self.last_start = time.monotonic()


def fetch_website(
    hostname: str, address: str | None, timeout: float, limiter: RequestLimiter,
) -> dict[str, Any]:
    """Fetch only a bounded public page, with at most two same-host redirects."""
    started = time.monotonic()
    row: dict[str, Any] = {
        "hostname": hostname, "ip": address, "peer_ip": "",
        "tls_hostname_verified": False, "http_status": None,
        "path": "/", "title": "", "body_sha256": "", "text_sha256": "",
        "body_bytes": 0, "truncated": False, "generic_page": False,
        "error": "", "classification": "connection-error", "elapsed_ms": 0,
        "checked_at_utc": utc_now(), "_text": "", "_content": "", "_assets": [],
        "inline_scripts_sha256": "",
        "controls_sha256": "", "login_evidence": False, "content_chars": 0,
    }
    path = "/"
    connection = None
    timeout_timer = None
    expired = threading.Event()
    deadline = 0.0
    try:
        for redirect in range(3):
            limiter.wait()
            connection = (
                PinnedHTTPSConnection(hostname, address, timeout) if address is not None
                else http.client.HTTPSConnection(hostname, timeout=timeout, context=ssl.create_default_context())
            )
            expired = threading.Event()
            deadline = time.monotonic() + timeout
            watched_socket = [None]

            def expire(conn=connection, event=expired, sockets=watched_socket) -> None:
                event.set()
                active_socket = conn.sock or sockets[0]
                if active_socket:
                    try:
                        active_socket.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

            timeout_timer = threading.Timer(timeout, expire)
            timeout_timer.daemon = True
            timeout_timer.start()
            connection.connect()
            watched_socket[0] = connection.sock
            if expired.is_set():
                raise socket.timeout("HTTPS request deadline exceeded")
            row["tls_hostname_verified"] = True
            if connection.sock:
                row["peer_ip"] = connection.sock.getpeername()[0]
            connection.request("GET", path, headers={
                "Host": hostname, "User-Agent": "censys-ip-validator/2.0",
                "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
                "Accept-Encoding": "identity", "Connection": "close",
            })
            response = connection.getresponse()
            row["http_status"] = response.status
            row["path"] = urlsplit(path).path or "/"
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                destination = urlsplit(urljoin(f"https://{hostname}{path}", location or ""))
                if not location or redirect == 2:
                    row["error"] = "missing redirect location or same-host redirect limit reached"
                    break
                if (destination.scheme != "https" or destination.hostname != hostname
                        or destination.port not in (None, 443)
                        or destination.username is not None or destination.password is not None):
                    row["error"] = "cross-host, credential-bearing, or non-HTTPS redirect not followed"
                    break
                path = urlunsplit(("", "", destination.path or "/", destination.query, ""))
                timeout_timer.cancel()
                connection.close()
                connection = None
                continue
            maximum = 256 * 1024
            body = response.read(maximum + 1)
            if expired.is_set() or time.monotonic() >= deadline:
                raise socket.timeout("HTTPS request deadline exceeded")
            declared_length = response.getheader("Content-Length")
            incomplete = bool(declared_length and declared_length.isdecimal()
                              and len(body) < int(declared_length))
            row["truncated"] = len(body) > maximum or incomplete
            if incomplete:
                row["error"] = "response ended before its declared Content-Length"
            body = body[:maximum]
            row["body_bytes"] = len(body)
            row["body_sha256"] = hashlib.sha256(body).hexdigest()
            content_type = response.getheader("Content-Type") or ""
            encoding = re.search(r"charset\s*=\s*([\w-]+)", content_type, re.I)
            try:
                decoded = body.decode(encoding.group(1) if encoding else "utf-8", errors="replace")
            except LookupError:
                decoded = body.decode("utf-8", errors="replace")
            parser = WebsiteParser()
            parser.feed(decoded)
            text = re.sub(r"\s+", " ", " ".join(parser.text)).strip().casefold()
            title = re.sub(r"\s+", " ", " ".join(parser.title)).strip().casefold()
            content = re.sub(r"\s+", " ", " ".join(parser.content)).strip().casefold()
            row.update({
                "_text": text, "_content": content, "_assets": sorted(parser.assets), "title": title[:200],
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "inline_scripts_sha256": hashlib.sha256("\n".join(parser.inline_scripts).encode()).hexdigest(),
                "controls_sha256": hashlib.sha256(json.dumps(parser.controls).encode()).hexdigest(),
                "content_chars": len(content),
                "login_evidence": len(content) >= 24 and parser.forms >= 1
                    and bool(re.search(r"(?:authentication|sign.?in|log.?in|đăng nhập)", title))
                    and bool(parser.password_inputs and parser.user_inputs or parser.login_links)
                    and bool(parser.assets),
                "classification": "http-response",
                "generic_page": any(marker in title for marker in (
                    "welcome to nginx", "apache2 ubuntu default", "apache http server test page",
                    "test page for the apache", "iis windows server",
                    "just a moment", "attention required", "access denied", "403 forbidden",
                    "404 not found", "bad gateway", "service unavailable", "index of /",
                )) or any(marker in content for marker in (
                    "an error occurred", "site is temporarily unavailable", "application error",
                    "the requested url was not found", "error establishing a database connection",
                    "verify you are human", "checking your browser", "complete the security check",
                    "enable javascript and cookies to continue", "complete the captcha",
                    "nginx web server is successfully installed", "default web site page",
                    "test the proper operation of the apache http server",
                )) or "cf-chl-" in decoded.casefold(),
            })
            break
    except (socket.timeout, TimeoutError) as exc:
        row["classification"] = "timeout"
        row["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
    except ssl.SSLCertVerificationError as exc:
        row["classification"] = "tls-verification-failed"
        row["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
    except (OSError, ValueError, http.client.HTTPException) as exc:
        row["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
    finally:
        if timeout_timer is not None:
            timeout_timer.cancel()
        if connection is not None:
            connection.close()
        if expired.is_set():
            row["classification"] = "timeout"
            row["error"] = "HTTPS request deadline exceeded"
        row["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        row["checked_at_utc"] = utc_now()
    return row


def usable_website(row: dict[str, Any]) -> bool:
    return (
        not row["error"] and row["tls_hostname_verified"]
        and row["http_status"] is not None and 200 <= row["http_status"] < 300
        and not row["generic_page"] and not row["truncated"]
        and row["body_bytes"] >= 200
        and (len(row["_content"]) >= 80 or row["login_evidence"])
    )


def check_website_pair(reference: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    row = {**candidate, "valid": False, "reason": "unverified-response"}
    if not usable_website(reference):
        row["reason"] = "reference-unavailable"
    elif not usable_website(candidate):
        row["reason"] = candidate["classification"] if candidate["error"] else "unusable-response"
    elif candidate["path"] != reference["path"] or candidate["http_status"] != reference["http_status"]:
        row["reason"] = "different-response-path-or-status"
    elif candidate["body_sha256"] == reference["body_sha256"]:
        row.update(valid=True, reason="matching-page-body")
    elif (candidate["title"] == reference["title"]
          and (len(reference["_text"]) >= 80 or reference["login_evidence"] and candidate["login_evidence"])
          and candidate["text_sha256"] == reference["text_sha256"]
          and candidate["inline_scripts_sha256"] == reference["inline_scripts_sha256"]
          and candidate["controls_sha256"] == reference["controls_sha256"]
          and candidate["_assets"] == reference["_assets"]):
        row.update(valid=True, reason="matching-visible-page")
    else:
        row["reason"] = "different-page-content"
    return row


def summarize_mapping(results: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "valid": sum(bool(row["valid"]) for row in results),
        "http": sum(row.get("http_status") is not None for row in results),
        "error": sum(bool(row.get("error")) and row.get("classification") != "timeout" for row in results),
        "timeout": sum(row.get("classification") == "timeout" for row in results),
        "skipped": sum(bool(row.get("skipped")) for row in results),
    }


class ProgressReporter:
    def __init__(
        self,
        path: Path,
        domain: str | None,
        ips: list[str],
        delay: float,
        output_path: Path,
        *,
        hosts: list[str] | None = None,
        mapping: bool = False,
        workers: int = 1,
    ) -> None:
        self.path = path
        self.domain = domain
        self.hosts = hosts
        self.is_mapping = mapping
        self.is_dns = hosts is not None and not mapping
        self.workers = workers
        self.reference_completed = 0
        self.reference_usable = 0
        self.active_items: set[str] = set()
        self.ips = ips
        self.delay = delay
        self.output_path = output_path
        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self.state: dict[str, Any] = {
            "completed": 0,
            "last_completed": "none",
            "phase": "starting",
            "current_item": "none",
            "counts": summarize_mapping([]) if mapping else summarize_dns([]) if self.is_dns else summarize([]),
        }

    def start_item(self, value: str) -> None:
        with self._state_lock:
            self.state["current_item"] = value
            if self.is_mapping:
                self.active_items.add(value)

    def finish_item(self, value: str) -> None:
        with self._state_lock:
            self.active_items.discard(value)

    def update_references(self, references: dict[str, dict[str, Any]]) -> None:
        with self._state_lock:
            self.reference_completed = len(references)
            self.reference_usable = sum(usable_website(row) for row in references.values())

    def update(self, results: list[dict[str, Any]], phase: str) -> None:
        with self._state_lock:
            self.state = {
                "completed": len(results),
                "last_completed": (f'{results[-1]["ip"]} {results[-1]["hostname"]}' if self.is_mapping
                                   else results[-1]["hostname" if self.is_dns else "ip"]) if results else "none",
                "phase": phase,
                "current_item": "none",
                "counts": summarize_mapping(results) if self.is_mapping else summarize_dns(results) if self.is_dns else summarize(results),
            }

    def emit(self, phase: str, next_action: str) -> None:
        with self._state_lock:
            state = dict(self.state)
            counts = dict(state["counts"])
            active = sorted(self.active_items)
            reference_completed, reference_usable = self.reference_completed, self.reference_usable
        payload = {
            "timestamp_utc": utc_now(),
            "tool": "validate_censys_ips.py",
            "invocation": "website-pair-validation" if self.is_mapping else "dns-resolution" if self.is_dns else "pinned-https-head",
            "phase": phase,
            "target_host": self.domain,
            "candidate_ips": self.ips,
            "planned": len(self.hosts) * len(self.ips) if self.is_mapping else len(self.hosts) if self.hosts is not None else len(self.ips),
            "completed": state["completed"],
            **counts,
            "rate_limit_rps": round(1.0 / self.delay, 2),
            "rate_unit": "host-lookups" if self.is_dns else "requests",
            "concurrency": self.workers,
            "last_completed": state["last_completed"],
            "current_item": active if self.is_mapping else state["current_item"],
            "next_action": next_action,
            "result_file": str(self.output_path),
        }
        if self.hosts is not None:
            payload["target_hosts"] = self.hosts
        if self.is_mapping:
            payload.update(reference_completed=reference_completed, reference_usable=reference_usable,
                           reference_planned=len(self.hosts))
        line = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        with self._write_lock:
            with self.path.open("a", encoding="utf-8") as progress_file:
                progress_file.write(line + "\n")
                progress_file.flush()
            if self.is_mapping:
                print(f'{payload["timestamp_utc"]} phase={phase} scope={len(self.hosts)}hosts/{len(self.ips)}IPs '
                      f'considered={payload["completed"]}/{payload["planned"]} valid={counts["valid"]} '
                      f'http={counts["http"]} errors={counts["error"]} timeouts={counts["timeout"]} '
                      f'skipped={counts["skipped"]} references={reference_completed}/{len(self.hosts)} '
                      f'usable={reference_usable} rate={payload["rate_limit_rps"]}/s concurrency={self.workers} '
                      f'last={state["last_completed"]} active={active} next={next_action}', file=sys.stderr, flush=True)
            else:
                print(line, file=sys.stderr, flush=True)


def write_report(
    output_path: Path,
    output_format: str,
    metadata: dict[str, Any],
    results: list[dict[str, Any]],
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if metadata.get("mode") == "mapping":
        pairs = list(dict.fromkeys((row["ip"], row["hostname"]) for row in results if row["valid"]))
        with output_path.open("x", encoding="utf-8", newline="") as output_file:
            if output_format == "hosts":
                for address, hostname in pairs:
                    output_file.write(f"{address} {hostname}\n")
            elif output_format == "csv":
                writer = csv.writer(output_file)
                writer.writerow(("ip", "hostname"))
                writer.writerows(pairs)
            else:
                json.dump({"metadata": metadata, "valid_pairs": [
                    {"ip": address, "hostname": hostname} for address, hostname in pairs
                ]}, output_file, indent=2)
                output_file.write("\n")
    elif output_format == "json":
        with output_path.open("x", encoding="utf-8") as output_file:
            json.dump({"metadata": metadata, "results": results}, output_file, indent=2)
            output_file.write("\n")
    else:
        with output_path.open("x", encoding="utf-8", newline="") as output_file:
            is_dns = metadata.get("mode") == "dns"
            fields = DNS_CSV_FIELDS if is_dns else CSV_FIELDS
            writer = csv.DictWriter(output_file, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in results:
                if is_dns:
                    for address in row["resolved_ips"] or [""]:
                        matched = address in row["matched_ips"]
                        writer.writerow({
                            "hostname": row["hostname"],
                            "resolved_ip": address,
                            "record_type": ("A" if ipaddress.ip_address(address).version == 4 else "AAAA") if address else "",
                            "in_ip_list": matched,
                            "status": ("dns-match" if matched else "dns-not-in-list") if address else row["status"],
                            "resolved_at_utc": row["resolved_at_utc"],
                            "elapsed_ms": row["elapsed_ms"],
                            "error": row["error"],
                        })
                else:
                    csv_row = dict(row)
                    csv_row["certificate_dns_sans"] = ";".join(row["certificate_dns_sans"])
                    writer.writerow(csv_row)
    output_path.chmod(0o600)

    if output_format == "csv" or metadata.get("mode") == "mapping":
        manifest_path = output_path.with_suffix(".manifest.json")
        with manifest_path.open("x", encoding="utf-8") as manifest_file:
            json.dump(metadata, manifest_file, indent=2)
            manifest_file.write("\n")
        manifest_path.chmod(0o600)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input",
        nargs="?",
        help="HTTPS mode: IP file; defaults to stdin",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--domain", help="HTTPS mode: hostname for TLS SNI and HTTP Host")
    mode.add_argument("--hosts", type=Path, help="Validate websites: file containing one hostname per line")
    parser.add_argument("--ips", type=Path, help="Candidate IP file (required with --hosts)")
    parser.add_argument("--dns-only", action="store_true", help="With --hosts, only compare DNS; do not validate websites")
    parser.add_argument("--workers", type=int, default=4, help="Website-check concurrency, globally rate-limited (default: 4)")
    parser.add_argument("--timeout", type=float, help="HTTPS socket timeout in seconds (default: 5); DNS uses OS resolver timeouts")
    parser.add_argument("--delay", type=float, default=1.0, help="Minimum gap between host lookups or HTTPS requests (default: 1 second)")
    parser.add_argument("--format", choices=("hosts", "csv", "json"), help="Default: hosts for website mapping; csv for DNS/legacy HEAD")
    parser.add_argument("--output", type=Path, help="Result path; defaults to a unique file under /tmp")
    args = parser.parse_args(argv)
    args.mode = ("dns" if args.dns_only else "mapping") if args.hosts is not None else "https"
    args.format = args.format or ("hosts" if args.mode == "mapping" else "csv")
    if args.hosts is not None:
        if args.ips is None:
            parser.error("--hosts requires --ips IP.txt")
        if args.input is not None:
            parser.error("Use --ips with --hosts; positional IP input is for --domain")
        if args.timeout is not None and args.mode == "dns":
            parser.error("--timeout is HTTPS-only; DNS uses OS resolver timeouts")
    else:
        if args.dns_only:
            parser.error("--dns-only requires --hosts")
        if args.ips is not None:
            parser.error("--ips requires --hosts")
        args.input = args.input or "-"
        try:
            args.domain = normalize_domain(args.domain)
        except ValueError as exc:
            parser.error(str(exc))
    if args.format == "hosts" and args.mode != "mapping":
        parser.error("--format hosts is for website mapping with --hosts, without --dns-only")
    if not 1 <= args.workers <= 16:
        parser.error("--workers must be between 1 and 16")
    if args.timeout is not None and (not math.isfinite(args.timeout) or args.timeout <= 0):
        parser.error("--timeout must be finite and greater than zero")
    args.timeout = args.timeout if args.timeout is not None else 5.0
    minimum_delay = 0.1 if args.mode == "dns" else 0.02
    if not math.isfinite(args.delay) or args.delay < minimum_delay:
        parser.error(f"--delay must be finite and at least {minimum_delay} seconds in {args.mode} mode")
    return args


def run_mapping(args: argparse.Namespace) -> int:
    try:
        ips, rejected_ips, duplicate_ips = load_ips(args.ips)
        hosts, rejected_hosts, duplicate_hosts = load_hosts(args.hosts)
        if not hosts or not ips:
            print("InputError: website mapping needs valid hostnames and globally routable candidate IPs", file=sys.stderr)
            return 2
    except (OSError, UnicodeError) as exc:
        print(f"InputError: {exc}", file=sys.stderr)
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    extension = "txt" if args.format == "hosts" else args.format
    output = (args.output or Path("/tmp") / f"censys-valid-hosts-{stamp}-{os.getpid()}.{extension}").expanduser().resolve()
    progress = output.with_suffix(".progress.log")
    manifest = output.with_suffix(".manifest.json")
    details = output.with_suffix(".details.json")
    paths = (output, progress, manifest, details)
    if len(set(paths)) != len(paths) or any(os.path.lexists(path) for path in paths):
        print("OutputError: refusing to overwrite an existing artifact or use colliding artifact paths", file=sys.stderr)
        return 2
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        progress.touch(mode=0o600, exist_ok=False)
    except OSError as exc:
        print(f"OutputError: {exc}", file=sys.stderr)
        return 2
    reporter = ProgressReporter(progress, None, ips, args.delay, output, hosts=hosts,
                                mapping=True, workers=args.workers)
    references: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    started = utc_now()
    status, reason = "completed", "all candidate pairs considered"
    stop = threading.Event()
    limiter = RequestLimiter(args.delay, stop)
    previous_sigint = None

    def heartbeat() -> None:
        while not stop.wait(10):
            reporter.emit("running", "finish active website comparisons")

    def fetch(host: str, address: str | None) -> dict[str, Any]:
        item = f"{address or 'reference'} {host}"
        reporter.start_item(item)
        try:
            return fetch_website(host, address, args.timeout, limiter)
        finally:
            reporter.finish_item(item)

    reporter.emit("started", "collect reference websites, then validate candidate pairs")
    thread = threading.Thread(target=heartbeat, name="mapping-heartbeat", daemon=True)
    thread.start()
    executor = ThreadPoolExecutor(max_workers=args.workers)
    futures = []
    try:
        reference_jobs = {executor.submit(fetch, host, None): host for host in hosts}
        futures = list(reference_jobs)
        for future in as_completed(reference_jobs):
            references[reference_jobs[future]] = future.result()
            reporter.update_references(references)
        jobs = {}
        for host in hosts:
            reference = references[host]
            for address in ips:
                if not usable_website(reference):
                    results.append({"hostname": host, "ip": address, "valid": False,
                                    "reason": "reference-unavailable", "skipped": True})
                else:
                    jobs[executor.submit(fetch, host, address)] = host
        reporter.update(results, "running")
        futures = list(jobs)
        for future in as_completed(jobs):
            candidate = future.result()
            results.append(check_website_pair(references[jobs[future]], candidate))
            reporter.update(results, "running")
    except KeyboardInterrupt:
        status, reason = "interrupted", "keyboard interrupt; preserving completed comparisons"
        if threading.current_thread() is threading.main_thread():
            previous_sigint = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
    except Exception as exc:
        status, reason = "failed", f"unexpected {type(exc).__name__}; preserving completed comparisons"
        print(f"ValidationError: {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
    finally:
        for future in futures:
            future.cancel()
        stop.set()
        thread.join()

    host_order, ip_order = {host: i for i, host in enumerate(hosts)}, {address: i for i, address in enumerate(ips)}
    results.sort(key=lambda row: (host_order[row["hostname"]], ip_order[row["ip"]]))
    counts = summarize_mapping(results)
    metadata = {
        "mode": "mapping", "status": status, "reason": reason,
        "started_at_utc": started, "completed_at_utc": utc_now(),
        "planned": len(hosts) * len(ips), "completed": len(results), "counts": counts,
        "reference_planned": len(hosts), "reference_completed": len(references),
        "reference_usable": sum(usable_website(row) for row in references.values()),
        "hosts_file": str(args.hosts.resolve()), "ips_file": str(args.ips.resolve()),
        "hostnames": hosts, "candidate_ips": ips, "concurrency": args.workers,
        "request_rate_limit_rps": round(1 / args.delay, 2), "timeout_seconds": args.timeout,
        "method": "GET", "port": 443, "max_response_bytes": 256 * 1024,
        "max_same_host_redirects": 2, "result_file": str(output),
        "progress_file": str(progress), "details_file": str(details),
        "rejected_input_lines": len(rejected_ips), "rejected_reasons": rejected_ips,
        "duplicate_ips_ignored": duplicate_ips, "rejected_host_lines": len(rejected_hosts),
        "rejected_host_reasons": rejected_hosts, "duplicate_hosts_ignored": duplicate_hosts,
        "note": "Valid means verified TLS and matching website content, not DNS equality, exclusive IP ownership, or proven origin hosting. Unavailable references are unverified, not evidence that an IP cannot serve the hostname. Reference resolution honors system DNS/hosts/NSS. No cookies, authentication, retries, or cross-host redirects.",
    }
    def public(row: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in row.items() if not key.startswith("_")}
    try:
        if previous_sigint is None and threading.current_thread() is threading.main_thread():
            previous_sigint = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        write_report(output, args.format, metadata, results)
        with details.open("x", encoding="utf-8") as stream:
            json.dump({"metadata": metadata, "references": [public(references[host]) for host in hosts if host in references],
                       "results": [public(row) for row in results]}, stream, indent=2)
            stream.write("\n")
        details.chmod(0o600)
    except OSError as exc:
        reporter.update(results, "failed")
        reporter.emit("failed", "could not write all artifacts")
        print(f"OutputError: {exc}", file=sys.stderr)
        return 2
    finally:
        try:
            # Checkpoint first: workers stuck in OS DNS must not delay artifact preservation.
            executor.shutdown(wait=False, cancel_futures=True)
        finally:
            if previous_sigint is not None:
                signal.signal(signal.SIGINT, previous_sigint)
    reporter.update(results, status)
    reporter.emit(status, reason)
    print(f'Valid pairs: {counts["valid"]}; file: {output}', file=sys.stderr)
    if not counts["valid"]:
        print(f"No confirmed pairs. This does not rule out unverified pairs; see {details}", file=sys.stderr)
    return 130 if status == "interrupted" else 2 if status == "failed" else 0


def main() -> int:
    args = parse_args()
    if args.mode == "mapping":
        return run_mapping(args)
    is_dns = args.mode == "dns"
    try:
        ips, rejected, duplicates = load_ips(
            args.ips if is_dns else args.input, public_only=not is_dns
        )
        hosts, rejected_hosts, duplicate_hosts = load_hosts(args.hosts) if is_dns else ([], [], 0)
        if is_dns and (not hosts or not ips):
            print("InputError: DNS mode requires at least one valid hostname and candidate IP", file=sys.stderr)
            return 2
    except (OSError, UnicodeError) as exc:
        print(f"InputError: {exc}", file=sys.stderr)
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prefix = "censys-dns-resolution" if is_dns else "censys-ip-validation"
    output_path = args.output or Path("/tmp") / f"{prefix}-{stamp}-{os.getpid()}.{args.format}"
    output_path = output_path.expanduser().resolve()
    progress_path = output_path.with_name(f"{output_path.stem}.progress.log")
    manifest_path = output_path.with_suffix(".manifest.json") if args.format == "csv" else None
    artifacts = [output_path, progress_path]
    if manifest_path is not None:
        artifacts.append(manifest_path)
    existing = [path for path in artifacts if path.exists()]
    if existing:
        print(
            "OutputError: refusing to overwrite existing artifact(s): "
            + ", ".join(str(path) for path in existing),
            file=sys.stderr,
        )
        return 2

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        progress_path.touch(mode=0o600, exist_ok=False)
        progress_path.chmod(0o600)
    except OSError as exc:
        print(f"OutputError: could not initialize artifacts: {exc}", file=sys.stderr)
        return 2
    reporter = ProgressReporter(
        progress_path, args.domain, ips, args.delay, output_path,
        hosts=hosts if is_dns else None,
    )

    started_at = utc_now()
    results: list[dict[str, Any]] = []
    status = "completed"
    reason = "all hostnames processed" if is_dns else "all candidates processed"
    stop_heartbeat = threading.Event()

    def heartbeat() -> None:
        while not stop_heartbeat.wait(10):
            reporter.emit("running", "finish current DNS lookup" if is_dns else "continue with next candidate IP")

    heartbeat_thread = threading.Thread(target=heartbeat, name="validation-heartbeat", daemon=True)
    reporter.update(results, "starting")
    reporter.emit(
        "started", "resolve first hostname" if is_dns
        else "begin first candidate IP" if ips else "write empty report; no valid public IPs",
    )
    heartbeat_thread.start()

    try:
        last_request_started = 0.0
        candidate_ips = set(ips)
        for item in hosts if is_dns else ips:
            wait = args.delay - (time.monotonic() - last_request_started)
            if last_request_started and wait > 0:
                time.sleep(wait)
            last_request_started = time.monotonic()
            reporter.start_item(item)
            results.append(
                resolve_host(item, candidate_ips) if is_dns
                else probe_ip(item, args.domain, args.timeout)
            )
            reporter.update(results, "running")
    except KeyboardInterrupt:
        status = "interrupted"
        reason = "keyboard interrupt; preserving completed results"
    finally:
        stop_heartbeat.set()
        heartbeat_thread.join()

    counts = summarize_dns(results) if is_dns else summarize(results)
    metadata = {
        "mode": args.mode,
        "domain": args.domain,
        "method": "HEAD",
        "port": 443,
        "status": status,
        "reason": reason,
        "started_at_utc": started_at,
        "completed_at_utc": utc_now(),
        "planned": len(hosts) if is_dns else len(ips),
        "completed": len(results),
        "rejected_input_lines": len(rejected),
        "rejected_reasons": rejected,
        "duplicate_ips_ignored": duplicates,
        "counts": counts,
        "request_rate_limit_rps": round(1.0 / args.delay, 2),
        "concurrency": 1,
        "timeout_seconds": args.timeout,
        "result_file": str(output_path),
        "progress_file": str(progress_path),
        "note": "A matching TLS certificate and HTTP response do not prove this IP is the origin; it may be a CDN or shared edge.",
    }
    if is_dns:
        matched_addresses = {address for row in results for address in row["matched_ips"]}
        for key in ("domain", "method", "port", "timeout_seconds", "request_rate_limit_rps"):
            metadata.pop(key)
        metadata.update({
            "resolver": "system socket.getaddrinfo (IPv4/IPv6)",
            "resolver_timeout_policy": "operating system configuration",
            "hosts_file": str(args.hosts.resolve()),
            "ips_file": str(args.ips.resolve()),
            "candidate_ips": ips,
            "matched_candidate_ips": [address for address in ips if address in matched_addresses],
            "unmatched_candidate_ips": [address for address in ips if address not in matched_addresses],
            "rejected_host_lines": len(rejected_hosts),
            "rejected_host_reasons": rejected_hosts,
            "duplicate_hosts_ignored": duplicate_hosts,
            "host_lookup_rate_limit_per_second": round(1.0 / args.delay, 2),
            "note": "Current system-resolver mappings, possibly cached or supplied by hosts/NSS configuration; they do not prove IP ownership or origin hosting. No HTTPS probes are made in DNS mode.",
        })

    try:
        write_report(output_path, args.format, metadata, results)
    except OSError as exc:
        reporter.update(results, status)
        reporter.emit("failed", f"report write failed: {type(exc).__name__}")
        print(f"OutputError: could not save result: {exc}", file=sys.stderr)
        return 2

    reporter.update(results, status)
    reporter.emit(status, reason)
    print(f"Results: {output_path}", file=sys.stderr)
    print(f"Progress: {progress_path}", file=sys.stderr)
    if is_dns:
        print(
            f"DNS matches: {counts['matched_hosts']}/{len(hosts)} hosts, "
            f"{len(matched_addresses)}/{len(ips)} candidate IPs", file=sys.stderr,
        )
    return 130 if status == "interrupted" else 0


if __name__ == "__main__":
    sys.exit(main())
