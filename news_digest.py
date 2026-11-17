"""
Daily News Digest - Tin tức tài chính (CafeF) + Bất động sản gửi qua Gmail.

Biến môi trường (đặt trong GitHub Secrets):
    GMAIL_USER          : địa chỉ Gmail dùng để gửi
    GMAIL_APP_PASSWORD  : App Password 16 ký tự của Gmail
    MAIL_TO             : email nhận (nhiều email cách nhau bằng dấu phẩy)
Tùy chọn:
    DRY_RUN=1           : không gửi mail, chỉ lưu preview.html
    LOOKBACK_HOURS=26   : số giờ lấy tin gần nhất
"""

import calendar
import html
import logging
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("news_digest")

VN_TZ = timezone(timedelta(hours=7))
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "26"))
MAX_PER_FIN_SOURCE = 8
MAX_PER_BDS_SECTION = 15
REQUEST_TIMEOUT = 25

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.5",
}

# group = "fin": nhóm theo từng nguồn | group = "bds": phân loại theo từ khóa
SOURCES = [
    # ---- CafeF ----
    {"name": "CafeF - Chứng khoán", "url": "https://cafef.vn/thi-truong-chung-khoan.rss", "kind": "rss", "group": "fin"},
    {"name": "CafeF - Vĩ mô", "url": "https://cafef.vn/vi-mo-dau-tu.rss", "kind": "rss", "group": "fin"},
    {"name": "CafeF - Ngân hàng", "url": "https://cafef.vn/tai-chinh-ngan-hang.rss", "kind": "rss", "group": "fin"},
    {"name": "CafeF - Doanh nghiệp", "url": "https://cafef.vn/doanh-nghiep.rss", "kind": "rss", "group": "fin"},
    {"name": "CafeF - Bất động sản", "url": "https://cafef.vn/bat-dong-san.rss", "kind": "rss", "group": "bds"},
    # ---- Các trang BĐS / báo lớn mục BĐS ----
    {"name": "VnExpress - BĐS", "url": "https://vnexpress.net/rss/bat-dong-san.rss", "kind": "rss", "group": "bds"},
    {"name": "Dân Trí - BĐS", "url": "https://dantri.com.vn/rss/bat-dong-san.rss", "kind": "rss", "group": "bds"},
    {"name": "Tuổi Trẻ - Nhà đất", "url": "https://tuoitre.vn/rss/nha-dat.rss", "kind": "rss", "group": "bds"},
    {"name": "VietnamNet - BĐS", "url": "https://vietnamnet.vn/rss/bat-dong-san.rss", "kind": "rss", "group": "bds"},
    # Best-effort: trang này thường chặn bot, nếu lỗi script sẽ bỏ qua và báo trong email
    {"name": "Batdongsan.com.vn", "url": "https://batdongsan.com.vn/tin-tuc", "kind": "html", "group": "bds"},
]

POLICY_KW = [
    "luật", "nghị định", "thông tư", "nghị quyết", "quy hoạch", "pháp lý",
    "sổ đỏ", "sổ hồng", "chính sách", "dự thảo", "bảng giá đất",
    "tiền sử dụng đất", "thuế", "quyết định", "giải phóng mặt bằng",
    "thanh tra", "vi phạm", "quốc hội", "chính phủ", "bộ xây dựng",
    "lãi suất", "tín dụng", "gỡ vướng", "cấp phép",
]
PRICE_KW = [
    "giá", "nguồn cung", "giao dịch", "mở bán", "thanh khoản", "đất nền",
    "căn hộ", "chung cư", "đấu giá", "m2", "triệu đồng", "tỷ đồng",
    "lượng bán", "tồn kho", "rao bán", "sơ cấp", "thứ cấp", "cho thuê",
]

SEC_POLICY = "Chính sách - Pháp lý BĐS"
SEC_PRICE = "Giá - Nguồn cung - Giao dịch BĐS"
SEC_MARKET = "Thị trường - Doanh nghiệp BĐS"
BDS_SECTION_ORDER = [SEC_POLICY, SEC_PRICE, SEC_MARKET]


def fetch(url):
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp


def clean_text(raw, limit=220):
    if not raw:
        return ""
    text = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "..."
    return text


def normalize_url(url):
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/").lower()


def parse_rss(src):
    resp = fetch(src["url"])
    feed = feedparser.parse(resp.content)
    items = []
    for e in feed.entries:
        parsed = e.get("published_parsed") or e.get("updated_parsed")
        dt = None
        if parsed:
            dt = datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
        title = clean_text(e.get("title", ""), limit=250)
        link = e.get("link", "")
        if not title or not link:
            continue
        items.append(
            {
                "title": title,
                "link": link,
                "summary": clean_text(e.get("summary", "")),
                "dt": dt,
                "source": src["name"],
                "group": src["group"],
            }
        )
    return items


def parse_html_news(src):
    resp = fetch(src["url"])
    soup = BeautifulSoup(resp.text, "html.parser")
    base_domain = urlparse(src["url"]).netloc
    seen = set()
    items = []
    for a in soup.find_all("a", href=True):
        link = urljoin(src["url"], a["href"])
        if urlparse(link).netloc != base_domain:
            continue
        if "/tin-tuc" not in link and "/phan-tich" not in link:
            continue
        title = a.get("title") or a.get_text(" ", strip=True)
        title = re.sub(r"\s+", " ", title or "").strip()
        if len(title) < 35:
            continue
        key = normalize_url(link)
        if key in seen or key == normalize_url(src["url"]):
            continue
        seen.add(key)
        items.append(
            {
                "title": title,
                "link": link,
                "summary": "",
                "dt": None,
                "source": src["name"],
                "group": src["group"],
            }
        )
    return items[:20]


def collect():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    all_items = []
    failed = []
    for src in SOURCES:
        try:
            items = parse_rss(src) if src["kind"] == "rss" else parse_html_news(src)
            fresh = [i for i in items if i["dt"] is None or i["dt"] >= cutoff]
            log.info("%s: %d tin (%d mới)", src["name"], len(items), len(fresh))
            if not items:
                failed.append(f"{src['name']} (không có dữ liệu)")
            all_items.extend(fresh)
        except Exception as exc:  # noqa: BLE001
            log.warning("Lỗi nguồn %s: %s", src["name"], exc)
            failed.append(f"{src['name']} ({type(exc).__name__})")
    return all_items, failed


def dedupe(items):
    seen_urls, seen_titles, result = set(), set(), []
    for it in items:
        u = normalize_url(it["link"])
        t = re.sub(r"\W+", "", it["title"].lower())
        if u in seen_urls or t in seen_titles:
            continue
        seen_urls.add(u)
        seen_titles.add(t)
        result.append(it)
    return result


def classify_bds(item):
    text = item["title"].lower()
    if any(k in text for k in POLICY_KW):
        return SEC_POLICY
    if any(k in text for k in PRICE_KW):
        return SEC_PRICE
    return SEC_MARKET


def sort_key(item):
    return item["dt"] or datetime(1970, 1, 1, tzinfo=timezone.utc)


def build_sections(items):
    """Trả về list[(tiêu đề, [items])] theo thứ tự hiển thị."""
    items = sorted(dedupe(items), key=sort_key, reverse=True)
    sections = []

    # Nhóm tài chính: theo từng nguồn CafeF
    fin_sources = [s["name"] for s in SOURCES if s["group"] == "fin"]
    for name in fin_sources:
        rows = [i for i in items if i["group"] == "fin" and i["source"] == name]
        if rows:
            sections.append((name, rows[:MAX_PER_FIN_SOURCE]))

    # Nhóm BĐS: phân loại theo từ khóa
    bds_buckets = {name: [] for name in BDS_SECTION_ORDER}
    for it in items:
        if it["group"] == "bds":
            bds_buckets[classify_bds(it)].append(it)
    for name in BDS_SECTION_ORDER:
        if bds_buckets[name]:
            sections.append((name, bds_buckets[name][:MAX_PER_BDS_SECTION]))
    return sections


def render_item_html(it):
    t = html.escape(it["title"])
    link = html.escape(it["link"], quote=True)
    when = it["dt"].astimezone(VN_TZ).strftime("%H:%M %d/%m") if it["dt"] else "n/a"
    meta = f"{html.escape(it['source'])} · {when}"
    summary = (
        f"<div style='color:#555;font-size:13px;margin-top:2px'>{html.escape(it['summary'])}</div>"
        if it["summary"]
        else ""
    )
    return (
        "<li style='margin-bottom:10px'>"
        f"<a href='{link}' style='color:#0b57d0;text-decoration:none;font-weight:600'>{t}</a>"
        f"<div style='color:#888;font-size:12px'>{meta}</div>{summary}</li>"
    )


def build_html(sections, failed, today_str):
    total = sum(len(rows) for _, rows in sections)
    parts = [
        "<html><body style='font-family:Arial,Helvetica,sans-serif;max-width:720px;"
        "margin:auto;color:#222'>",
        f"<h2 style='margin-bottom:4px'>Bản tin tài chính &amp; BĐS - {today_str}</h2>",
        f"<div style='color:#666;font-size:13px'>Tổng {total} tin trong {LOOKBACK_HOURS} giờ qua</div>",
    ]
    if not sections:
        parts.append("<p>Không có tin mới từ các nguồn.</p>")
    for title, rows in sections:
        parts.append(
            f"<h3 style='border-bottom:2px solid #0b57d0;padding-bottom:4px;margin-top:24px'>"
            f"{html.escape(title)} ({len(rows)})</h3><ul style='padding-left:18px'>"
        )
        parts.extend(render_item_html(it) for it in rows)
        parts.append("</ul>")
    if failed:
        parts.append(
            "<p style='color:#b00020;font-size:12px;margin-top:30px'>Nguồn lỗi/không truy cập được: "
            + html.escape("; ".join(failed))
            + "</p>"
        )
    parts.append("</body></html>")
    return "".join(parts)


def build_text(sections, today_str):
    lines = [f"BẢN TIN TÀI CHÍNH & BĐS - {today_str}", ""]
    for title, rows in sections:
        lines.append(f"== {title} ({len(rows)}) ==")
        for it in rows:
            lines.append(f"- {it['title']}")
            lines.append(f"  {it['link']}")
        lines.append("")
    return "\n".join(lines)


def send_email(subject, html_body, text_body):
    user = os.getenv("GMAIL_USER")
    password = os.getenv("GMAIL_APP_PASSWORD")
    mail_to = os.getenv("MAIL_TO") or user
    if not user or not password:
        raise SystemExit("Thiếu GMAIL_USER hoặc GMAIL_APP_PASSWORD trong biến môi trường.")
    recipients = [m.strip() for m in mail_to.split(",") if m.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(user, password.replace(" ", ""))
        server.sendmail(user, recipients, msg.as_string())
    log.info("Đã gửi mail tới: %s", ", ".join(recipients))


def main():
    today_str = datetime.now(VN_TZ).strftime("%d/%m/%Y")
    items, failed = collect()
    sections = build_sections(items)
    html_body = build_html(sections, failed, today_str)
    text_body = build_text(sections, today_str)
    subject = f"[Bản tin] Tài chính & BĐS - {today_str}"

    if os.getenv("DRY_RUN") == "1":
        with open("preview.html", "w", encoding="utf-8") as f:
            f.write(html_body)
        log.info("DRY_RUN: đã lưu preview.html, không gửi mail.")
        return
    send_email(subject, html_body, text_body)


if __name__ == "__main__":
    main()
