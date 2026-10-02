# fetch-hostnames-censys

English | [Tiếng Việt](README_vi.md)

A Python script that uses **BeautifulSoup** to extract the IP addresses or hostnames displayed in Censys Platform result titles. Parse saved HTML entirely offline, or use **Playwright + Chromium** to load and render one search results page.

The script supports **Web Property** and **Host** results, keeps IPv4/IPv6 addresses when shown in the title, normalizes hostnames, and removes duplicates. It does not use the Censys API or require an API key. Search access remains subject to the permissions and quota Censys applies to the browser session.

`validate_censys_ips.py` can then check candidate IP addresses against a hostname using HTTPS.

## Requirements and installation

- Python **3.10 or newer**; checked with Python 3.12.
- `beautifulsoup4`: required for both modes.
- `playwright` and its matching Chromium browser: required when `--fetch` has no reusable cache.
- `--headed` requires a graphical display, such as a Linux desktop or WSL with WSLg.
- `validate_censys_ips.py` uses only the Python standard library; no additional packages are required.

Clone the repository and create a virtual environment:

```bash
git clone https://github.com/TTN-ATTN/fetch-hostnames-censys.git
cd fetch-hostnames-censys
python3 -m venv .venv
source .venv/bin/activate
python -m pip install beautifulsoup4 playwright
python -m playwright install chromium
```

Installing the `playwright` Python package **does not install Chromium**. After upgrading Playwright, you may need to run the browser installation command again.

For offline HTML parsing only:

```bash
python -m pip install beautifulsoup4
```

## Usage

### Manual

```text
$ python fetch_censys.py --help
usage: fetch_censys.py [-h] (--html HTML | --fetch) [--query QUERY]
                       [--domain DOMAIN] [--final-url FINAL_URL]
                       [--cache-dir CACHE_DIR] [--refresh]
                       [--chromium-path CHROMIUM_PATH] [--headed]

Extract IPs or hostnames shown in Censys result titles (Web Properties and
Hosts). Offline: python fetch_censys.py --html /tmp/saved.html Online: python
fetch_censys.py --fetch --headed Requires beautifulsoup4; --fetch also
requires playwright and its Chromium: python -m pip install beautifulsoup4
playwright python -m playwright install chromium No pagination or retries.
Existing cached HTML is reused unless --refresh is set. Keeps IPv4/IPv6 titles
instead of DNS aliases; hostname titles remain hostnames. With --headed,
pauses for manual confirmation when a Cloudflare challenge appears. Use
--chromium-path to select an installed Chromium executable. Excludes
certificate/Matched Fields snippets, which can contain truncated text.

options:
  -h, --help            show this help message and exit
  --html HTML           Parse saved HTML; no network access
  --fetch               Use cache or fetch one search page
  --query QUERY
  --domain DOMAIN       Only this domain and its subdomains; excludes IPs
  --final-url FINAL_URL
                        Final URL associated with --html (for redirect checks)
  --cache-dir CACHE_DIR
  --refresh             Explicitly spend quota to replace a cached page
  --chromium-path CHROMIUM_PATH
                        Use this installed Chromium executable instead of
                        Playwright's bundled browser
  --headed              Show Chromium; wait for manual Cloudflare CAPTCHA
                        confirmation if needed
```

The IP validator has its own manual:

```bash
python validate_censys_ips.py --help
```

### Quick start

Parse saved, rendered HTML offline and save the IP addresses and hostnames:

```bash
python fetch_censys.py --html /tmp/censys-results.html > hosts.txt
```

Fetch one results page with a visible browser, or reuse its cache:

```bash
python fetch_censys.py --fetch --headed --query 'example.com'
```

If a Cloudflare challenge appears, solve it in Chromium, then press Enter in the terminal. The
script waits for the results without reloading the page. Headless mode does not pause for manual
verification.

To select a separately installed Chromium executable, pass its path with `--chromium-path`, for
example `--chromium-path /usr/bin/chromium`. Without this option, Playwright's bundled browser is
used.

Use a more specific Censys query by passing its text directly:

```bash
python fetch_censys.py --fetch --headed \
  --query '(example.com) and host.ip: * and host.services.cert.names="example.com"'
```

Keep only hostname titles in a domain and its subdomains, excluding IP results, without making requests:

```bash
python fetch_censys.py --html /tmp/censys-results.html --domain example.com
```

Cache defaults to `/tmp/censys-search`; use `--cache-dir` to change it. Add `--refresh` only when you intend to fetch again and potentially spend more quota. Omit `--headed` to run headless.

Save candidate IPs one per line in `ips.txt`, then check whether they serve the chosen hostname:

```bash
python validate_censys_ips.py --domain example.com \
  --output results/ip-validation.csv ips.txt
```

The validator connects to each IP on port 443, sets TLS SNI and HTTP Host to `--domain`, verifies
the certificate, and sends one `HEAD /` request if TLS verification succeeds. It records the HTTP
status, certificate names, server header, and redirect location without downloading the body or
following redirects. Requests run serially with a one-second minimum gap by default; no retries
are made. Invalid/non-global IPs are rejected and duplicates are ignored. Input can also come from
stdin when the filename is omitted.

CSV output includes a `.manifest.json` sidecar and a `.progress.log` with a heartbeat every ten
seconds during the run. Use `--format json` for results and metadata in one JSON file. Without
`--output`, unique artifacts are saved under `/tmp`. Existing artifacts are never overwritten.
`--timeout` and `--delay` adjust the defaults of five seconds and one second. No target ID is required.

## How it works

1. Read the `--html` file, or check the cache for `--fetch`.
2. If a fetch is needed, Playwright launches Chromium and navigates to the search URL. It waits for results, a no-results indicator, a registration redirect, or a Cloudflare challenge. In headed mode, it pauses for manual CAPTCHA confirmation and then waits for results without reloading. Navigation and the initial DOM wait each have a 20-second timeout.
3. Track redirects and save the DOM and metadata. If the page is navigating during capture, wait briefly for the current document; if capture still fails, record that separately in metadata and close the browser without masking the original fetch error.
4. BeautifulSoup parses the HTML with Python's built-in `html.parser`, using the structures below.
5. Keep and normalize IPv4/IPv6 addresses from result titles. For hostname titles, lowercase names, remove trailing dots, and convert Unicode names to IDNA. Reject invalid identifiers, apply `--domain` if supplied, remove duplicates, and print one IP or hostname per line.

| Result type | Output source |
| --- | --- |
| Web Property | `h2 [data-testid="host-identifier-name"]`, preferring `aria-label="Host identifier: ..."` and falling back to the title link's URL. |
| Host | The `h2` title inside a `/hosts/...` link. Outputs the displayed IP, even if a DNS hostname is shown below it. |

The parser reads result titles, not DNS aliases below them, **Matched Fields**, certificate content, or the entire page text. Snippets may be truncated or contain unrelated identifiers. Ports are excluded; IPv6 addresses are emitted without brackets.

A query containing `host.services.cert.names="example.com"` can return Host results with IP titles. The script outputs those IPs, not their DNS aliases or certificate names. `--domain example.com` keeps only matching hostname titles and excludes all IPs; omit it to retain IP results.

## Redirects, errors, and output

A redirect to `https://accounts.censys.io/register`, including a query string or trailing slash, raises **`CensysRegistrationRequired`**, a subclass of `CensysError`.

The CLI catches this exception, prints a message to **stderr**, and exits with code **2**. When calling `fetch_html()` or `parse_hostnames()` from Python, you can catch the exception directly.

Saved HTML alone cannot establish redirect history. If you know the final URL associated with an offline file, supply it explicitly:

```bash
python fetch_censys.py --html /tmp/censys-results.html \
  --final-url 'https://accounts.censys.io/register'
```

A “Register” link or button on a results page is not considered a redirect.

| Case | Behavior |
| --- | --- |
| Successful parsing | Exit code `0`; one IP address or hostname per line on stdout. |
| Explicitly empty results, no valid identifiers, or all results filtered out | Exit code `0`; empty stdout. |
| Registration redirect | `CensysRegistrationRequired`, exit code `2`. |
| Recognized Cloudflare challenge, incomplete HTML, fetch failure, or unrecognized markup | `CensysError`, exit code `2` for errors handled by the CLI. |

Cache location messages and errors go to stderr, so they do not appear in a file captured with `> hosts.txt`.

### Troubleshooting

**`Executable doesn't exist` or `Playwright Chromium is missing`:**

```bash
python -m playwright install chromium
```

**Missing Chromium system libraries on Linux:**

```bash
python -m playwright install --with-deps chromium
```

`--with-deps` may require administrator privileges to install system packages.

**Cannot open a window with `--headed`:** check your graphical environment and `DISPLAY`, or omit `--headed` to run headless.

**Cloudflare, timeout, or cached error:** inspect `page.html` and `metadata.json`. The script does not bypass CAPTCHAs or quota limits. Use `--refresh` only when you intend to attempt another fetch.

**Unrecognized HTML:** ensure the saved DOM includes rendered results. If Censys changes its interface, the selectors may need updating; the script does not use a stable API schema.

List all CLI options:

```bash
python fetch_censys.py --help
```

## Limitations

- Extracts only data present in the current HTML page; it does not retrieve every page of a search.
- Does not extract certificate-only results, wildcard certificate names, or SAN lists.
- `fetch_censys.py` does not perform DNS lookups or verify that IP addresses or hostnames are still active.
- The IP validator checks only HTTPS on port 443. A matching certificate and HTTP response do not prove an origin IP; the address may be a CDN or shared edge.
- Cannot guarantee access when Censys requires login or Cloudflare blocks the browser.
- Does not refresh the cache automatically; results may remain stale until explicitly refreshed.
