#!/usr/bin/env python3
"""Extract IPs or hostnames shown in Censys result titles (Web Properties and Hosts).

Offline: python fetch_censys.py --html /tmp/saved.html
Online:  python fetch_censys.py --fetch --headed

Requires beautifulsoup4; --fetch also requires playwright and its Chromium:
    python -m pip install beautifulsoup4 playwright
    python -m playwright install chromium
No pagination or retries. Existing cached HTML is reused unless --refresh is set.
Keeps IPv4/IPv6 titles instead of DNS aliases; hostname titles remain hostnames.
Excludes certificate/Matched Fields snippets, which can contain truncated text.
"""

import argparse
import hashlib
import ipaddress
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlencode, urlsplit

from bs4 import BeautifulSoup


DEFAULT_QUERY = "example.com"
IDENTIFIER_SELECTOR = 'h2 [data-testid="host-identifier-name"]'


class CensysError(RuntimeError):
    """Censys content could not be retrieved or recognized."""


class CensysRegistrationRequired(CensysError):
    """The search redirected to accounts.censys.io/register."""


def check_redirect(url: str) -> None:
    parsed = urlsplit(url)
    if (parsed.hostname or "").lower() == "accounts.censys.io" and (
        unquote(parsed.path).rstrip("/") == "/register"
    ):
        raise CensysRegistrationRequired(
            "Censys redirected to https://accounts.censys.io/register"
        )


def normalize_hostname(value: str) -> str | None:
    value = value.strip().lower().rstrip(".")
    try:
        ipaddress.ip_address(value.strip("[]"))
        return None
    except ValueError:
        pass
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    labels = value.split(".")
    if len(value) > 253 or len(labels) < 2 or labels[-1].isdigit():
        return None
    if not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", x) for x in labels):
        return None
    return value


def parse_hostnames(html: str, *, final_url: str = "", domain: str | None = None) -> list[str]:
    """Return deduplicated title IPs/hostnames (legacy function name retained).

    Supply final_url when parsing externally saved HTML: HTML alone cannot
    establish whether an HTTP or JavaScript navigation occurred.
    """
    check_redirect(final_url)
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True).lower() if soup.title else ""
    if "just a moment" in title or soup.select_one("#challenge-error-text"):
        raise CensysError("Cloudflare challenge HTML; search results are not available.")
    if domain is not None:
        domain = normalize_hostname(domain)
        if domain is None:
            raise ValueError("--domain must be a DNS hostname")

    nodes = soup.select(IDENTIFIER_SELECTOR)
    host_links = [a for a in soup.select('a[href^="/hosts/"]') if a.find('h2')]
    if not nodes and not host_links:
        # Do not confuse a login page, app shell or changed markup with zero hits.
        for element in soup(["script", "style", "noscript"]):
            element.decompose()
        text = soup.get_text(" ", strip=True)
        if re.search(r"\bResults:\s*0\b|\bNo results found\b", text, re.I):
            return []
        raise CensysError(
            "No result identifiers found. HTML may be incomplete, access may be "
            "restricted, or Censys markup may have changed."
        )

    candidates = []
    for node in nodes:
        label = node.get("aria-label", "")
        match = re.match(r"^Host identifier:\s*(.*?)(?:,\s*Port Number:.*)?$", label)
        if match:
            candidate = match.group(1)
        else:
            # The observed title link is /web/<hostname>:<port>?at_time=...
            anchor = node.find_parent("a", href=True)
            if anchor is None:
                raise CensysError("Result identifier has neither a label nor a link.")
            path = unquote(urlsplit(anchor["href"]).path)
            if not path.startswith(("/web/", "/hosts/")):
                raise CensysError("Unrecognized result-title URL.")
            candidate = path.split("/", 2)[2].split("/", 1)[0]
            bracketed = re.fullmatch(r"\[([^\]]+)\](?::\d+)?", candidate)
            if bracketed:
                candidate = bracketed.group(1)
            elif candidate.count(":") == 1:
                candidate = candidate.rsplit(":", 1)[0]
        candidates.append(candidate)
    for link in host_links:
        # Keep the displayed IP, even when a DNS alias appears below the title.
        candidates.append(link.find('h2').get_text("", strip=True))

    identifiers: dict[str, None] = {}
    for candidate in candidates:
        try:
            address = str(ipaddress.ip_address(candidate.strip().strip("[]")))
        except ValueError:
            pass
        else:
            # A domain filter applies only to hostname titles, never DNS aliases.
            if domain is None:
                identifiers[address] = None
            continue
        hostname = normalize_hostname(candidate)
        if hostname and (domain is None or hostname == domain or hostname.endswith("." + domain)):
            identifiers[hostname] = None
    return list(identifiers)


def fetch_html(query: str, cache_dir: Path, *, refresh: bool = False, headed: bool = False) -> tuple[str, str]:
    """Navigate once with Playwright, cache the rendered DOM, never paginate."""
    source_url = "https://platform.censys.io/search?" + urlencode({"q": query})
    cache = cache_dir / hashlib.sha256(source_url.encode()).hexdigest()[:16]
    html_path, metadata_path = cache / "page.html", cache / "metadata.json"
    if html_path.exists() and metadata_path.exists() and not refresh:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        check_redirect(metadata["final_url"])
        if metadata.get("registration_redirect"):
            raise CensysRegistrationRequired("Censys redirected to https://accounts.censys.io/register")
        if metadata.get("error"):
            raise CensysError(f"Cached fetch failed; inspect {cache} before explicitly using --refresh.")
        return html_path.read_text(encoding="utf-8"), metadata["final_url"]

    from playwright.sync_api import Error as PlaywrightError, sync_playwright

    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            raise CensysError(
                "Playwright Chromium is missing. Run: python -m playwright install chromium"
            )
        try:
            browser = playwright.chromium.launch(headless=not headed)
        except PlaywrightError as exc:
            raise CensysError(
                "Could not start Chromium. Run: python -m playwright install chromium. "
                "If using --headed, also check that a graphical display is available. "
                f"Playwright details: {exc}"
            ) from exc
        page = browser.new_page()
        registration_seen = []

        def record_navigation(frame):
            if frame == page.main_frame:
                try:
                    check_redirect(frame.url)
                except CensysRegistrationRequired:
                    registration_seen.append(True)

        page.on("framenavigated", record_navigation)
        error = None
        response = None
        try:
            response = page.goto(source_url, wait_until="domcontentloaded", timeout=20000)
            # Inspect the redirect chain too, including transient register redirects.
            request = response.request if response else None
            while request:
                check_redirect(request.url)
                request = request.redirected_from
            check_redirect(page.url)
            # DOM-only waiting: no reload, request replay, click or extra search.
            page.wait_for_function(
                """() => (location.hostname === 'accounts.censys.io' &&
                    location.pathname.replace(/\\/+$/, '') === '/register') ||
                    document.querySelector('h2 [data-testid="host-identifier-name"]') ||
                    document.querySelector('a[href^="/hosts/"] h2') ||
                    /Results:\\s*0\\b|No results found/i.test(document.body?.innerText || '')""",
                timeout=20000,
            )
            check_redirect(page.url)
            if registration_seen:
                raise CensysRegistrationRequired("Censys redirected to https://accounts.censys.io/register")
        except Exception as exc:
            error = exc
        finally:
            final_url = page.url
            html = page.content()
            html_path.write_text(html, encoding="utf-8")
            html_path.chmod(0o600)
            metadata_path.write_text(json.dumps({
                "source_url": source_url,
                "final_url": final_url,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "initial_http_status": response.status if response else None,
                "error": type(error).__name__ if error else None,
                "registration_redirect": bool(registration_seen) or isinstance(error, CensysRegistrationRequired),
                "sha256": hashlib.sha256(html.encode()).hexdigest(),
            }, indent=2), encoding="utf-8")
            metadata_path.chmod(0o600)
            browser.close()
            print(f"Saved observation: {cache}", file=sys.stderr)
        check_redirect(final_url)
        if registration_seen or isinstance(error, CensysRegistrationRequired):
            raise CensysRegistrationRequired("Censys redirected to https://accounts.censys.io/register") from error
        if error:
            raise CensysError(f"Search did not finish; inspect {cache}. No retry was made.") from error
        return html, final_url


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--html", type=Path, help="Parse saved HTML; no network access")
    source.add_argument("--fetch", action="store_true", help="Use cache or fetch one search page")
    parser.add_argument("--query", default=DEFAULT_QUERY)
    parser.add_argument("--domain", help="Only this domain and its subdomains; excludes IPs")
    parser.add_argument("--final-url", default="", help="Final URL associated with --html (for redirect checks)")
    parser.add_argument("--cache-dir", type=Path, default=Path("/tmp/censys-search"))
    parser.add_argument("--refresh", action="store_true", help="Explicitly spend quota to replace a cached page")
    parser.add_argument("--headed", action="store_true", help="Show Chromium when fetching")
    args = parser.parse_args()
    if args.html and (args.refresh or args.headed):
        parser.error("--refresh and --headed require --fetch")
    try:
        if args.html:
            html, final_url = args.html.read_text(encoding="utf-8"), args.final_url
        else:
            html, final_url = fetch_html(args.query, args.cache_dir, refresh=args.refresh, headed=args.headed)
        for identifier in parse_hostnames(html, final_url=final_url, domain=args.domain):
            print(identifier)
        return 0
    except (CensysError, OSError, ValueError, ImportError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
