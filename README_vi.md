# fetch-hostnames-censys

[English](README.md) | Tiếng Việt

Script Python dùng **BeautifulSoup** để lấy IP hoặc hostname hiển thị ở tiêu đề kết quả tìm kiếm trên Censys Platform. Có thể đọc HTML đã lưu hoàn toàn offline hoặc dùng **Playwright + Chromium** để tải và render một trang kết quả.

Script hỗ trợ kết quả **Web Property** và **Host**, giữ IPv4/IPv6 khi tiêu đề hiển thị IP, chuẩn hóa hostname và bỏ trùng. Không dùng Censys API và không yêu cầu API key; khả năng tìm kiếm vẫn phụ thuộc quyền truy cập và quota mà Censys áp dụng cho phiên trình duyệt.

`validate_censys_ips.py` giúp kiểm tra các IP ứng viên với một hostname qua HTTPS sau khi có kết quả.

## Requirements và cài đặt

- Python **3.10 trở lên**; đã chạy kiểm tra với Python 3.12.
- `beautifulsoup4`: bắt buộc cho cả hai chế độ.
- `playwright` và browser Chromium tương ứng: cần khi `--fetch` không có cache để dùng lại.
- Chế độ `--headed` cần môi trường hiển thị đồ họa, chẳng hạn desktop Linux hoặc WSL có WSLg.
- `validate_censys_ips.py` chỉ dùng thư viện chuẩn Python, không cần cài thêm package.

Clone repo và tạo môi trường riêng:

```bash
git clone https://github.com/TTN-ATTN/fetch-hostnames-censys.git
cd fetch-hostnames-censys
python3 -m venv .venv
source .venv/bin/activate
python -m pip install beautifulsoup4 playwright
python -m playwright install chromium
```

Cài package `playwright` **không đồng nghĩa** với đã cài Chromium. Sau khi nâng phiên bản Playwright, có thể cần chạy lại lệnh cài browser.

Nếu chỉ parse HTML đã lưu:

```bash
python -m pip install beautifulsoup4
```

## Cách sử dụng

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

Script validate IP có manual riêng:

```bash
python validate_censys_ips.py --help
```

### Quick start

Đọc HTML đã render offline và lưu danh sách IP, hostname:

```bash
python fetch_censys.py --html /tmp/censys-results.html > hosts.txt
```

Tải một trang kết quả với cửa sổ browser, hoặc dùng lại cache đã có:

```bash
python fetch_censys.py --fetch --headed --query 'example.com'
```

Nếu xuất hiện CAPTCHA của Cloudflare, hãy giải trong Chromium rồi nhấn Enter ở terminal. Script chờ
kết quả mà không tải lại trang. Chế độ headless không dừng để chờ xác nhận thủ công.

Để chọn executable Chromium cài riêng, truyền đường dẫn bằng `--chromium-path`, ví dụ
`--chromium-path /usr/bin/chromium`. Nếu không truyền tùy chọn này, script dùng browser Playwright
đã tải về.

Truyền trực tiếp chuỗi truy vấn Censys khi cần tìm cụ thể hơn:

```bash
python fetch_censys.py --fetch --headed \
  --query '(example.com) and host.ip: * and host.services.cert.names="example.com"'
```

Chỉ giữ tiêu đề hostname thuộc miền và các subdomain, loại kết quả IP, không gửi request:

```bash
python fetch_censys.py --html /tmp/censys-results.html --domain example.com
```

Cache mặc định ở `/tmp/censys-search`; dùng `--cache-dir` để đổi vị trí. Chỉ thêm `--refresh` khi muốn tải lại và chấp nhận có thể tốn thêm quota. Bỏ `--headed` để chạy headless.

Lưu mỗi IP ứng viên trên một dòng trong `ips.txt`, rồi kiểm tra chúng với hostname muốn xác minh:

```bash
python validate_censys_ips.py --domain example.com \
  --output results/ip-validation.csv ips.txt
```

Validator kết nối tới mỗi IP trên cổng 443, đặt TLS SNI và HTTP Host theo `--domain`, kiểm tra
certificate rồi gửi một request `HEAD /` nếu TLS hợp lệ. Kết quả gồm HTTP status, tên trên
certificate, header server và địa chỉ redirect; không tải body hay đi theo redirect. Các request
chạy tuần tự, mặc định cách nhau tối thiểu một giây và không retry. IP không hợp lệ hoặc không
định tuyến toàn cầu bị loại; IP trùng bị bỏ qua. Có thể đọc từ stdin bằng cách bỏ tên file input.

Output CSV đi kèm `.manifest.json` và `.progress.log`, với heartbeat mỗi mười giây khi chạy.
Dùng `--format json` để lưu kết quả và metadata trong một file JSON. Nếu không truyền `--output`,
artifact được tạo với tên riêng dưới `/tmp`; script không ghi đè artifact có sẵn. `--timeout` và
`--delay` thay đổi giá trị mặc định năm giây và một giây. Không cần truyền target ID.

## Cách hoạt động

1. Đọc file `--html`, hoặc kiểm tra cache khi dùng `--fetch`.
2. Nếu cần tải, Playwright mở Chromium và điều hướng tới URL tìm kiếm. Script chờ kết quả, trạng thái không có kết quả, redirect đăng ký hoặc thử thách Cloudflare. Với chế độ headed, script tạm dừng để bạn xác nhận đã giải CAPTCHA, sau đó chờ kết quả mà không tải lại trang. Thời gian điều hướng và chờ DOM ban đầu tối đa 20 giây cho mỗi bước.
3. Theo dõi redirect và lưu DOM cùng metadata. Nếu trang vẫn đang điều hướng lúc chụp HTML, script chờ ngắn để document hiện tại ổn định; nếu vẫn không chụp được, lỗi này được ghi riêng vào metadata và browser được đóng mà không che lỗi fetch ban đầu.
4. BeautifulSoup parse HTML bằng `html.parser` có sẵn trong Python, theo cấu trúc dưới đây.
5. Giữ và chuẩn hóa IPv4/IPv6 ở tiêu đề kết quả. Với tiêu đề hostname, chuyển thành chữ thường, bỏ dấu chấm cuối và chuyển tên Unicode sang IDNA. Loại giá trị không hợp lệ, áp dụng `--domain` nếu có, bỏ trùng và in mỗi IP hoặc hostname trên một dòng.

| Loại kết quả | Nguồn dữ liệu xuất |
| --- | --- |
| Web Property | Phần tử `h2 [data-testid="host-identifier-name"]`, ưu tiên `aria-label="Host identifier: ..."`; dự phòng bằng URL của link tiêu đề. |
| Host | Tiêu đề `h2` trong link `/hosts/...`. Xuất IP hiển thị ở tiêu đề, kể cả khi bên dưới có tên DNS. |

Parser đọc tiêu đề kết quả, không lấy tên DNS bên dưới, **Matched Fields**, nội dung certificate hay toàn bộ văn bản trang. Các đoạn trích có thể bị cắt ngắn hoặc chứa giá trị không liên quan. Output không chứa port; IPv6 được xuất không kèm dấu ngoặc vuông.

Truy vấn có điều kiện `host.services.cert.names="example.com"` có thể trả về kết quả Host với tiêu đề IP. Script xuất IP đó, không lấy tên DNS hay tên trên certificate. `--domain example.com` chỉ giữ tiêu đề hostname thuộc miền và loại toàn bộ IP; hãy bỏ tùy chọn này nếu muốn giữ kết quả IP.

## Redirect, lỗi và output

Nếu chuyển tới `https://accounts.censys.io/register` (kể cả có query string hoặc dấu `/` cuối), script tạo exception **`CensysRegistrationRequired`**, kế thừa `CensysError`.

Khi chạy CLI, exception này được bắt để in thông báo lên **stderr** và thoát với mã **2**. Khi gọi `fetch_html()` hoặc `parse_hostnames()` từ Python, bạn có thể bắt exception trực tiếp.

Với HTML offline, nội dung HTML không đủ để biết lịch sử redirect. Nếu biết URL cuối của lần lưu trang, truyền thêm:

```bash
python fetch_censys.py --html /tmp/censys-results.html \
  --final-url 'https://accounts.censys.io/register'
```

Link hoặc nút “Register” xuất hiện trên trang kết quả không được coi là redirect.

| Trường hợp | Kết quả |
| --- | --- |
| Parse thành công | Mã thoát `0`; mỗi dòng stdout là một IP hoặc hostname. |
| Trang báo không có kết quả, không có giá trị hợp lệ hoặc bộ lọc loại hết | Mã thoát `0`; stdout rỗng. |
| Redirect đăng ký | `CensysRegistrationRequired`, mã thoát `2`. |
| Nhận diện trang Cloudflare, HTML chưa đầy đủ, lỗi tải hoặc cấu trúc không nhận dạng được | `CensysError`, mã thoát `2` cho các lỗi được CLI xử lý. |

Thông báo vị trí cache và lỗi đi vào stderr nên không trộn vào file khi dùng `> hosts.txt`.

### Khắc phục lỗi thường gặp

**`Executable doesn't exist` hoặc `Playwright Chromium is missing`:**

```bash
python -m playwright install chromium
```

**Linux thiếu thư viện hệ thống cho Chromium:**

```bash
python -m playwright install --with-deps chromium
```

Lệnh `--with-deps` có thể yêu cầu quyền quản trị để cài package hệ thống.

**Không mở được cửa sổ khi dùng `--headed`:** kiểm tra môi trường đồ họa/`DISPLAY`, hoặc bỏ `--headed` để chạy headless.

**Cloudflare, timeout hoặc lỗi cache:** kiểm tra `page.html` và `metadata.json`. Script không vượt CAPTCHA hay giới hạn quota. Chỉ dùng `--refresh` khi muốn chủ động thử một lần tải mới.

**HTML không nhận dạng được:** kiểm tra đã lưu DOM sau khi kết quả render. Nếu Censys thay đổi giao diện, các selector có thể cần cập nhật; script không dùng API schema ổn định.

Xem toàn bộ tham số:

```bash
python fetch_censys.py --help
```

## Giới hạn

- Chỉ đọc dữ liệu có trong trang HTML hiện tại; không bảo đảm lấy đủ toàn bộ kết quả tìm kiếm nhiều trang.
- Không trích xuất certificate-only results, wildcard certificate names hay danh sách SAN.
- `fetch_censys.py` không thực hiện DNS lookup hoặc xác minh IP, hostname còn hoạt động.
- Validator chỉ kiểm tra HTTPS trên cổng 443. Certificate phù hợp và HTTP response không chứng minh đây là IP origin; địa chỉ có thể thuộc CDN hoặc hạ tầng dùng chung.
- Không bảo đảm truy cập được khi Censys yêu cầu đăng nhập hoặc Cloudflare chặn browser.
- Không tự cập nhật cache; dữ liệu có thể cũ cho tới khi chủ động refresh.
