#!/usr/bin/env python3
"""Validate candidate Censys IPs with one pinned HTTPS HEAD request each.

The connection goes directly to each supplied IP while TLS SNI and the HTTP
Host header use --domain. Certificate verification stays enabled. Redirects
are recorded but never followed; response bodies are never read.

Example:
    python validate_censys_ips.py --domain example.com \
        ips.txt

Input is one IP per line, or stdin when no input file is provided. Results and
append-only progress telemetry default to unique files under /tmp.
"""

from __future__ import annotations

import argparse
import csv
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


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


def load_ips(input_name: str) -> tuple[list[str], list[dict[str, Any]], int]:
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
        if not address.is_global:
            rejected.append({"line": line_number, "reason": "IP is not globally routable"})
            continue
        normalized = str(address)
        if normalized in seen:
            duplicates += 1
            continue
        seen.add(normalized)
        addresses.append(normalized)
    return addresses, rejected, duplicates


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


class ProgressReporter:
    def __init__(
        self,
        path: Path,
        domain: str,
        ips: list[str],
        delay: float,
        output_path: Path,
    ) -> None:
        self.path = path
        self.domain = domain
        self.ips = ips
        self.delay = delay
        self.output_path = output_path
        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self.state: dict[str, Any] = {
            "completed": 0,
            "last_completed": "none",
            "phase": "starting",
            "counts": {"success": 0, "http": 0, "error": 0, "timeout": 0},
        }

    def update(self, results: list[dict[str, Any]], phase: str) -> None:
        with self._state_lock:
            self.state = {
                "completed": len(results),
                "last_completed": results[-1]["ip"] if results else "none",
                "phase": phase,
                "counts": summarize(results),
            }

    def emit(self, phase: str, next_action: str) -> None:
        with self._state_lock:
            state = dict(self.state)
            counts = dict(state["counts"])
        payload = {
            "timestamp_utc": utc_now(),
            "tool": "validate_censys_ips.py",
            "invocation": "pinned-https-head",
            "phase": phase,
            "target_host": self.domain,
            "candidate_ips": self.ips,
            "planned": len(self.ips),
            "completed": state["completed"],
            **counts,
            "rate_limit_rps": round(1.0 / self.delay, 2),
            "concurrency": 1,
            "last_completed": state["last_completed"],
            "next_action": next_action,
            "result_file": str(self.output_path),
        }
        line = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        with self._write_lock:
            with self.path.open("a", encoding="utf-8") as progress_file:
                progress_file.write(line + "\n")
                progress_file.flush()
            print(line, file=sys.stderr, flush=True)


def write_report(
    output_path: Path,
    output_format: str,
    metadata: dict[str, Any],
    results: list[dict[str, Any]],
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "json":
        with output_path.open("x", encoding="utf-8") as output_file:
            json.dump({"metadata": metadata, "results": results}, output_file, indent=2)
            output_file.write("\n")
    else:
        with output_path.open("x", encoding="utf-8", newline="") as output_file:
            writer = csv.DictWriter(output_file, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for row in results:
                csv_row = dict(row)
                csv_row["certificate_dns_sans"] = ";".join(row["certificate_dns_sans"])
                writer.writerow(csv_row)
    output_path.chmod(0o600)

    if output_format == "csv":
        manifest_path = output_path.with_suffix(".manifest.json")
        with manifest_path.open("x", encoding="utf-8") as manifest_file:
            json.dump(metadata, manifest_file, indent=2)
            manifest_file.write("\n")
        manifest_path.chmod(0o600)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input",
        nargs="?",
        default="-",
        help="Text file containing one IP per line; defaults to stdin",
    )
    parser.add_argument("--domain", required=True, help="Hostname to test with TLS SNI and HTTP Host")
    parser.add_argument("--timeout", type=float, default=5.0, help="Connect/TLS/HTTP timeout in seconds (default: 5)")
    parser.add_argument("--delay", type=float, default=1.0, help="Minimum gap between requests in seconds (default: 1)")
    parser.add_argument("--format", choices=("csv", "json"), default="csv", help="Output format (default: csv)")
    parser.add_argument("--output", type=Path, help="Result path; defaults to a unique file under /tmp")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.delay < 0.02:
        parser.error("--delay cannot exceed 50 requests per second")
    try:
        args.domain = normalize_domain(args.domain)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def main() -> int:
    args = parse_args()
    try:
        ips, rejected, duplicates = load_ips(args.input)
    except OSError as exc:
        print(f"InputError: {exc}", file=sys.stderr)
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = args.output or Path("/tmp") / f"censys-ip-validation-{stamp}-{os.getpid()}.{args.format}"
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
    reporter = ProgressReporter(progress_path, args.domain, ips, args.delay, output_path)

    started_at = utc_now()
    results: list[dict[str, Any]] = []
    status = "completed"
    reason = "all candidates processed"
    stop_heartbeat = threading.Event()

    def heartbeat() -> None:
        while not stop_heartbeat.wait(10):
            reporter.update(results, "running")
            reporter.emit("running", "continue with next candidate IP")

    heartbeat_thread = threading.Thread(target=heartbeat, name="validation-heartbeat", daemon=True)
    reporter.update(results, "starting")
    reporter.emit("started", "begin first candidate IP" if ips else "write empty report; no valid public IPs")
    heartbeat_thread.start()

    try:
        last_request_started = 0.0
        for address in ips:
            wait = args.delay - (time.monotonic() - last_request_started)
            if last_request_started and wait > 0:
                time.sleep(wait)
            last_request_started = time.monotonic()
            results.append(probe_ip(address, args.domain, args.timeout))
            reporter.update(results, "running")
    except KeyboardInterrupt:
        status = "interrupted"
        reason = "keyboard interrupt; preserving completed results"
    finally:
        stop_heartbeat.set()
        heartbeat_thread.join()

    counts = summarize(results)
    metadata = {
        "domain": args.domain,
        "method": "HEAD",
        "port": 443,
        "status": status,
        "reason": reason,
        "started_at_utc": started_at,
        "completed_at_utc": utc_now(),
        "planned": len(ips),
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
    return 130 if status == "interrupted" else 0


if __name__ == "__main__":
    sys.exit(main())
