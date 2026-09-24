# fetch-hostnames-censys

[English](README.md) | Tiếng Việt

Script Python dùng **BeautifulSoup** để lấy hostname từ HTML kết quả tìm kiếm trên Censys Platform. Có thể đọc HTML đã lưu hoàn toàn offline hoặc dùng **Playwright + Chromium** để tải và render một trang kết quả.

Script hỗ trợ kết quả **Web Property** và **Host**, loại địa chỉ IP, chuẩn hóa hostname và bỏ trùng. Không dùng Censys API và không yêu cầu API key; khả năng tìm kiếm vẫn phụ thuộc quyền truy cập và quota mà Censys áp dụng cho phiên trình duyệt.

## Requirements và cài đặt

- Python **3.10 trở lên**; đã chạy kiểm tra với Python 3.12.
- `beautifulsoup4`: bắt buộc cho cả hai chế độ.
- `playwright` và browser Chromium tương ứng: cần khi `--fetch` không có cache để dùng lại.
- Chế độ `--headed` cần môi trường hiển thị đồ họa, chẳng hạn desktop Linux hoặc WSL có WSLg.

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
usage: fetch_censys.py [-h] (--html HTML | --fetch) [--query QUERY] [--domain DOMAIN]
                       [--final-url FINAL_URL] [--cache-dir CACHE_DIR] [--refresh] [--headed]

Extract hostnames from one Censys search page (Web Properties and Hosts). Offline: python
fetch_censys.py --html /tmp/saved.html Online: python fetch_censys.py --fetch --headed Requires
beautifulsoup4; --fetch also requires playwright and its Chromium: python -m pip install
beautifulsoup4 playwright python -m playwright install chromium No pagination or retries. Existing
cached HTML is reused unless --refresh is set. Extracts Web Property titles and Host DNS rows.
Excludes IP addresses and certificate/Matched Fields snippets, which can contain truncated text.

options:
  -h, --help            show this help message and exit
  --html HTML           Parse saved HTML; no network access
  --fetch               Use cache or fetch one search page
  --query QUERY
  --domain DOMAIN       Only this domain and its subdomains
  --final-url FINAL_URL
                        Final URL associated with --html (for redirect checks)
  --cache-dir CACHE_DIR
  --refresh             Explicitly spend quota to replace a cached page
  --headed              Show Chromium when fetching
```

### Quick start

Đọc HTML đã render offline và lưu danh sách hostname:

```bash
python fetch_censys.py --html /tmp/censys-results.html > hostnames.txt
```

Tải một trang kết quả với cửa sổ browser, hoặc dùng lại cache đã có:

```bash
python fetch_censys.py --fetch --headed --query 'example.com'
```

Truyền trực tiếp chuỗi truy vấn Censys khi cần tìm cụ thể hơn:

```bash
python fetch_censys.py --fetch --headed \
  --query '(example.com) and host.ip: * and host.services.cert.names="example.com"'
```

Lọc kết quả đã lưu theo miền và các subdomain, không gửi request:

```bash
python fetch_censys.py --html /tmp/censys-results.html --domain example.com
```

Cache mặc định ở `/tmp/censys-search`; dùng `--cache-dir` để đổi vị trí. Chỉ thêm `--refresh` khi muốn tải lại và chấp nhận có thể tốn thêm quota. Bỏ `--headed` để chạy headless.

## Cách hoạt động

1. Đọc file `--html`, hoặc kiểm tra cache khi dùng `--fetch`.
2. Nếu cần tải, Playwright mở Chromium, điều hướng tới URL tìm kiếm và chờ DOM có dấu hiệu kết quả, không có kết quả hoặc redirect đăng ký. Thời gian chờ điều hướng và chờ DOM tối đa 20 giây cho mỗi bước.
3. Theo dõi redirect và lưu DOM cùng metadata. Các lỗi xảy ra trước khi tạo được trang browser, như thiếu Chromium, chưa có DOM để lưu.
4. BeautifulSoup parse HTML bằng `html.parser` có sẵn trong Python, theo cấu trúc dưới đây.
5. Chuẩn hóa tên thành chữ thường, bỏ dấu chấm cuối, chuyển tên Unicode sang IDNA; loại IPv4/IPv6 và tên không hợp lệ. Sau đó áp dụng `--domain`, bỏ trùng và in hostname.

| Loại kết quả | Nguồn hostname |
| --- | --- |
| Web Property | Phần tử `h2 [data-testid="host-identifier-name"]`, ưu tiên `aria-label="Host identifier: ..."`; dự phòng bằng URL của link tiêu đề. |
| Host | Dòng DNS có class chứa `_countRow_` trong phần header `_content_` gắn với link tiêu đề `/hosts/...`. Tiêu đề IP không được đưa vào danh sách hostname. |

Parser không lấy tên từ **Matched Fields**, nội dung certificate hay toàn bộ văn bản trang vì các đoạn hiển thị này có thể bị cắt ngắn hoặc chứa tên không phải hostname của kết quả.

Một truy vấn có điều kiện `host.services.cert.names="example.com"` có thể trả về các hostname DNS thuộc `amazonaws.com` hoặc `googleusercontent.com`. Điều kiện tìm kiếm khớp tên trên certificate; trường script xuất là hostname DNS hiển thị của host. Vì vậy `--domain example.com` có thể cho danh sách rỗng dù Censys có kết quả.

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
| Parse thành công | Mã thoát `0`; hostname trên stdout, mỗi dòng một tên. |
| Trang báo không có kết quả, không có hostname DNS hoặc bộ lọc loại hết | Mã thoát `0`; stdout rỗng. |
| Redirect đăng ký | `CensysRegistrationRequired`, mã thoát `2`. |
| Nhận diện trang Cloudflare, HTML chưa đầy đủ, lỗi tải hoặc cấu trúc không nhận dạng được | `CensysError`, mã thoát `2` cho các lỗi được CLI xử lý. |

Thông báo vị trí cache và lỗi đi vào stderr nên không trộn vào file khi dùng `> hostnames.txt`.

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
- Không thực hiện DNS lookup hoặc xác minh hostname còn hoạt động.
- Không bảo đảm truy cập được khi Censys yêu cầu đăng nhập hoặc Cloudflare chặn browser.
- Không tự cập nhật cache; dữ liệu có thể cũ cho tới khi chủ động refresh.
