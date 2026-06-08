#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
historydocuments.org scraper — fast, requests-only
pip install requests beautifulsoup4 reportlab arabic-reshaper python-bidi pillow
"""

import os, re, io, zipfile, urllib.parse, time
from concurrent.futures import ThreadPoolExecutor
from xml.sax.saxutils import escape as xe
import urllib3, requests
from bs4 import BeautifulSoup, NavigableString, Tag, Comment
from PIL import Image
import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "downloads")
FONT_PATH  = os.path.join(os.path.dirname(__file__), "Amiri-Regular.ttf")
FONT_B     = os.path.join(os.path.dirname(__file__), "Amiri-Bold.ttf")
HEADERS    = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
SESSION    = requests.Session()
SESSION.verify = False


# ── فونت ──────────────────────────────────────────────────────────────────────

def ensure_font():
    if os.path.exists(FONT_PATH):
        return
    print("Downloading Amiri font...")
    r = SESSION.get(
        "https://github.com/aliftype/amiri/releases/download/1.000/Amiri-1.000.zip",
        timeout=60
    )
    z = zipfile.ZipFile(io.BytesIO(r.content))
    for name in z.namelist():
        if name.endswith("Amiri-Regular.ttf"):
            open(FONT_PATH, "wb").write(z.read(name))
        if name.endswith("Amiri-Bold.ttf"):
            open(FONT_B, "wb").write(z.read(name))
    print("Font saved.")


def register_fonts():
    pdfmetrics.registerFont(TTFont("Amiri",  FONT_PATH))
    pdfmetrics.registerFont(TTFont("AmiriB", FONT_B if os.path.exists(FONT_B) else FONT_PATH))


# ── متن فارسی ─────────────────────────────────────────────────────────────────

def rtl(text: str) -> str:
    """reshape + bidi — برای drawRightString"""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text).strip()
    if not text:
        return ""
    return get_display(arabic_reshaper.reshape(text))


# ── دریافت با retry ───────────────────────────────────────────────────────────

def fetch_html(url: str) -> BeautifulSoup | None:
    for attempt in range(3):
        try:
            r = SESSION.get(url, headers=HEADERS, timeout=45)
            r.encoding = "utf-8"
            return BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            print(f"  fetch attempt {attempt+1}/3 failed: {type(e).__name__}")
            if attempt < 2:
                time.sleep(3)
    return None


# ── استخراج ──────────────────────────────────────────────────────────────────

def extract(soup: BeautifulSoup, base_url: str):
    # نام فایل
    h4 = soup.find(lambda t: t.name == "h4" and "panel-title" in t.get("class", []))
    pt = h4.get_text(strip=True) if h4 else ""
    h2 = soup.find("h2")
    h2t = h2.get_text(strip=True) if h2 else ""
    pdf_name = f"{pt}.{h2t}" if pt and h2t else (pt or h2t or "document")

    # panel-body محتوا: اول اونی که h2 داره، وگرنه طولانی‌ترین
    panels = soup.find_all(class_="panel-body")
    content = None
    for pb in panels:
        if pb.find("h2"):
            content = pb
            break
    if content is None and panels:
        # fallback: panel-body با بیشترین متن (احتمالاً محتوای اصلی)
        content = max(panels, key=lambda p: len(p.get_text(strip=True)))

    blocks = []   # (kind, text)  kind: h2|h3|p|hr
    images = []   # url strings
    buf    = []   # جمع‌آوری متن پاراگراف جاری

    def flush():
        t = re.sub(r" {2,}", " ", " ".join(buf)).strip()
        if t:
            blocks.append(("p", t))
        buf.clear()

    if content:
        for el in content.children:
            if isinstance(el, NavigableString):
                t = str(el).replace("\n", " ").replace("\r", " ").strip()
                if t:
                    buf.append(t)
            elif isinstance(el, Tag):
                name = el.name
                if name == "br":
                    flush()
                elif name in ("h1","h2","h3","h4","h5"):
                    flush()
                    t = el.get_text(strip=True)
                    if t:
                        blocks.append((name if name in ("h2","h3") else "h2", t))
                elif name == "hr":
                    flush()
                    blocks.append(("hr", ""))
                elif name == "center":
                    flush()
                    for img in el.find_all("img"):
                        src = img.get("src", "").strip()
                        if src:
                            images.append(urllib.parse.urljoin(base_url, src))
                else:
                    t = el.get_text(separator=" ", strip=True)
                    if t:
                        buf.append(t)
        flush()

    # لینک بعدی
    next_url = None
    for a in soup.find_all("a", class_=lambda c: c and "btn-primary" in c):
        href = a.get("href", "").strip()
        if href and href != "#":
            next_url = urllib.parse.urljoin(base_url, href)
            break

    return pdf_name, blocks, images, next_url


# ── دانلود تصویر ─────────────────────────────────────────────────────────────

def download_one(args):
    url, tmp_dir = args
    for attempt in range(3):
        try:
            r = SESSION.get(url, headers=HEADERS, timeout=60)
            r.raise_for_status()
            img = Image.open(io.BytesIO(r.content)).convert("RGB")
            fname = re.sub(r"[^a-zA-Z0-9._-]", "_",
                           os.path.basename(urllib.parse.urlparse(url).path)) or "img"
            if "." not in fname:
                fname += ".jpg"
            # اگه چند تصویر هم‌نام باشن
            fpath = os.path.join(tmp_dir, fname)
            base, ext = os.path.splitext(fpath)
            counter = 0
            while os.path.exists(fpath):
                counter += 1
                fpath = f"{base}_{counter}{ext}"
            img.save(fpath, "JPEG", quality=92)
            return fpath
        except Exception:
            if attempt < 2:
                time.sleep(2)
    return None


def download_images(urls: list, tmp_dir: str) -> list:
    if not urls:
        return []
    os.makedirs(tmp_dir, exist_ok=True)
    args = [(u, tmp_dir) for u in urls]
    # حداکثر 4 دانلود موازی
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(download_one, args))
    # ترتیب اصلی حفظ بشه
    return [r for r in results if r]


# ── ساخت PDF ──────────────────────────────────────────────────────────────────

PAGE_W, PAGE_H = A4
L_MARGIN = 18*mm
R_MARGIN = 18*mm
T_MARGIN = 16*mm
B_MARGIN = 16*mm
USABLE_W = PAGE_W - L_MARGIN - R_MARGIN


def wrap_logical(text: str, font_name: str, font_size: float, max_width: float):
    """
    متن منطقی (قبل از bidi) رو به خطوطی تقسیم می‌کنه که در max_width جا بشن.
    عرض رو روی متن reshape‌شده (نه bidi) اندازه می‌گیریم — چون چینش حروف
    عربی عرض رو تغییر میده.
    """
    words = text.split()
    lines = []
    cur = []
    for w in words:
        trial = " ".join(cur + [w])
        # عرض با خود فونت متصل (reshaped)
        reshaped = arabic_reshaper.reshape(trial)
        width = pdfmetrics.stringWidth(reshaped, font_name, font_size)
        if width <= max_width or not cur:
            cur.append(w)
        else:
            lines.append(" ".join(cur))
            cur = [w]
    if cur:
        lines.append(" ".join(cur))
    return lines


def build_pdf(pdf_name: str, blocks: list, img_paths: list, out_path: str):
    c = rl_canvas.Canvas(out_path, pagesize=A4)
    y = PAGE_H - T_MARGIN
    right_x = PAGE_W - R_MARGIN
    left_x  = L_MARGIN

    def new_page():
        nonlocal y
        c.showPage()
        y = PAGE_H - T_MARGIN

    def need(h):
        nonlocal y
        if y - h < B_MARGIN:
            new_page()

    def draw_block(text, font, size, leading, color=(0,0,0), bg=None):
        nonlocal y
        if not text.strip():
            return
        lines = wrap_logical(text, font, size, USABLE_W)
        for line in lines:
            need(leading)
            if bg:
                c.setFillColorRGB(*bg)
                c.rect(left_x, y - leading + 2, USABLE_W, leading, stroke=0, fill=1)
            c.setFillColorRGB(*color)
            c.setFont(font, size)
            c.drawRightString(right_x, y - size, rtl(line))
            y -= leading

    def draw_hr():
        nonlocal y
        need(4)
        c.setStrokeColorRGB(0.7, 0.7, 0.7)
        c.setLineWidth(0.5)
        c.line(left_x, y, right_x, y)
        y -= 4

    # عنوان
    draw_block(pdf_name, "AmiriB", 14, 20, color=(0.08,0.24,0.47), bg=(0.93,0.95,0.98))
    y -= 4

    for kind, text in blocks:
        if kind == "hr":
            draw_hr()
        elif kind in ("h2","h3"):
            y -= 2
            draw_block(text, "AmiriB", 12, 17, color=(0.08,0.24,0.47))
            y -= 1
        else:
            draw_block(text, "Amiri", 10, 15)
            y -= 2

    # تصاویر
    if img_paths:
        max_img_w = min(USABLE_W, 160*mm)
        max_img_h = PAGE_H - T_MARGIN - B_MARGIN
        for p in img_paths:
            try:
                with Image.open(p) as im:
                    w, h = im.size
                ratio = h / w if w else 1
                dw = max_img_w
                dh = dw * ratio
                if dh > max_img_h:
                    dw = dw * max_img_h / dh
                    dh = max_img_h
                # هر تصویر صفحه‌ی جدید
                new_page()
                x = (PAGE_W - dw) / 2
                img_y = y - dh
                c.drawImage(p, x, img_y, width=dw, height=dh,
                            preserveAspectRatio=True, mask='auto')
                y = img_y - 4
            except Exception as e:
                print(f"  [!] image draw: {e}")

    c.save()


# ── پاک‌سازی نام ─────────────────────────────────────────────────────────────

def safe_name(name: str) -> str:
    # حذف کنترل‌کاراکترها و newline ها، و کاراکترهای غیرمجاز ویندوز
    name = re.sub(r"[\r\n\t\x00-\x1f]", " ", name)
    name = re.sub(r'[\\/*?:"<>|]', "-", name)
    name = re.sub(r"\s+", " ", name).strip(". ").strip()
    return name[:120] or "document"


# ── پردازش یک صفحه ───────────────────────────────────────────────────────────

FAILED = []   # [(url, reason, intended_name, target_dir)]


def fetch_html_retry(url: str, attempts: int = 5) -> BeautifulSoup | None:
    """fetch با تلاش بیشتر و backoff"""
    for i in range(attempts):
        try:
            r = SESSION.get(url, headers=HEADERS, timeout=60)
            r.encoding = "utf-8"
            return BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            print(f"   fetch try {i+1}/{attempts}: {type(e).__name__}")
            if i < attempts - 1:
                time.sleep(3 + i * 2)
    return None


def download_doc_to(target_dir: str, doc_url: str, pdf_title: str, max_attempts: int = 3) -> bool:
    """
    یک سند رو در target_dir ذخیره می‌کنه. در صورت شکست retry می‌کنه.
    True اگر موفق.
    """
    os.makedirs(target_dir, exist_ok=True)
    for attempt in range(1, max_attempts + 1):
        try:
            soup = fetch_html_retry(doc_url)
            if not soup:
                if attempt < max_attempts:
                    time.sleep(4)
                    continue
                return False

            _, blocks, img_urls, _ = extract(soup, doc_url)

            tmp = os.path.join(target_dir, "_tmp")
            img_paths = []
            if img_urls:
                img_paths = download_images(img_urls, tmp)
                if len(img_paths) < len(img_urls):
                    # بعضی تصاویر ناقص - retry کامل
                    if attempt < max_attempts:
                        for p in img_paths:
                            try: os.remove(p)
                            except: pass
                        time.sleep(3)
                        continue

            if not blocks and not img_paths:
                return False

            base = safe_name(pdf_title)
            out = os.path.join(target_dir, base + ".pdf")
            counter = 1
            while os.path.exists(out):
                counter += 1
                out = os.path.join(target_dir, f"{base} ({counter}).pdf")

            build_pdf(pdf_title, blocks, img_paths, out)

            for p in img_paths:
                try: os.remove(p)
                except: pass

            return True
        except Exception as e:
            print(f"   try {attempt}/{max_attempts} error: {type(e).__name__}: {e}")
            if attempt < max_attempts:
                time.sleep(4)
    return False


# ── حالت کتاب: استخراج لیست اسناد از یک صفحه ─────────────────────────────────

def extract_book_page(soup: BeautifulSoup, base_url: str):
    """
    از table.table-striped.table-hover ردیف‌ها رو می‌گیره.
    هر ردیف: کامنت <!--<td><b>N</b></td>--> + <td>نام</td> + ... <center><a href=...>
    خروجی: [(order:int, name:str, url:str), ...], next_page_url
    """
    items = []
    table = soup.find("table", class_=lambda c: c and "table-striped" in c and "table-hover" in c)
    if table:
        for tr in table.find_all("tr"):
            # عدد ردیف از کامنت
            order = None
            for child in tr.children:
                if isinstance(child, Comment):
                    m = re.search(r"<b>\s*(\d+)\s*</b>", str(child))
                    if m:
                        order = int(m.group(1))
                        break
            if order is None:
                continue

            tds = tr.find_all("td", recursive=False)
            if not tds:
                continue

            # اولین td = نام
            name = tds[0].get_text(strip=True)

            # لینک: اولین <a> داخل <center> در همین ردیف
            link = None
            for cen in tr.find_all("center"):
                a = cen.find("a", href=True)
                if a:
                    link = urllib.parse.urljoin(base_url, a["href"].strip())
                    break
            if not link:
                # fallback: هر <a> در ردیف
                a = tr.find("a", href=True)
                if a:
                    link = urllib.parse.urljoin(base_url, a["href"].strip())

            if name and link:
                items.append((order, name, link))

    # صفحه بعد: لینک‌هایی با class شامل "paging_left" (به جز paging_active که صفحه فعلی است)
    next_page = None
    candidates = []
    for a in soup.find_all("a", class_=True, href=True):
        cls = a.get("class", [])
        if "paging_left" in cls and "paging_active" not in cls:
            href = a["href"].strip()
            if href and href != "#":
                candidates.append(urllib.parse.urljoin(base_url, href))
    # اولین لینک paging_left بعد از current = صفحه بعد
    # ولی برای اطمینان، آدرس فعلی رو فیلتر می‌کنیم
    for u in candidates:
        if u != base_url:
            next_page = u
            break

    return items, next_page


def derive_book_folder(soup: BeautifulSoup, book_url: str) -> str:
    """نام پوشه‌ی کتاب رو از عنوان صفحه یا URL بساز"""
    h = soup.find(lambda t: t.name in ("h1","h2","h3","h4") and t.get_text(strip=True))
    name = h.get_text(strip=True) if h else ""
    if not name:
        # از URL استفاده کن
        q = urllib.parse.urlparse(book_url).query
        name = "book_" + re.sub(r"[^a-zA-Z0-9]+", "_", q)[:40]
    return safe_name(name)


def build_start_url(book_url: str, start: int) -> str:
    """به URL کتاب پارامتر start=N اضافه/جایگزین می‌کنه"""
    parsed = urllib.parse.urlparse(book_url)
    qs = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    qs["start"] = str(start)
    new_q = urllib.parse.urlencode(qs)
    return urllib.parse.urlunparse(parsed._replace(query=new_q))


def run_book_mode(book_url: str):
    """با افزایش start=1,2,... همه‌ی صفحات کتاب رو دانلود می‌کنه"""
    print(f"\n[Book mode] {book_url}")
    global_idx = 0
    book_dir = None
    start = 1
    empty_streak = 0  # چند صفحه‌ی خالی پشت‌سرهم → پایان

    while True:
        page_url = build_start_url(book_url, start)
        print(f"\n=== start={start}: {page_url} ===")

        soup = fetch_html_retry(page_url, attempts=5)
        if not soup:
            FAILED.append((page_url, "book page fetch failed", "", OUTPUT_DIR))
            empty_streak += 1
            if empty_streak >= 2:
                print("Too many failures, stopping.")
                break
            start += 1
            continue

        if book_dir is None:
            book_dir = os.path.join(OUTPUT_DIR, derive_book_folder(soup, page_url))
            os.makedirs(book_dir, exist_ok=True)
            print(f"   book folder: {book_dir}")

        items, _ = extract_book_page(soup, page_url)
        print(f"   {len(items)} document(s)")

        if not items:
            empty_streak += 1
            if empty_streak >= 1:
                print("Empty page → end of book.")
                break
            start += 1
            continue
        empty_streak = 0

        items.sort(key=lambda x: x[0])

        for order, name, doc_url in items:
            global_idx += 1
            prefix = f"{global_idx:03d}"
            pdf_title = f"{prefix} {name}"
            print(f"   [{prefix}] {name}")
            ok = download_doc_to(book_dir, doc_url, pdf_title, max_attempts=3)
            if ok:
                print(f"            saved.")
            else:
                print(f"            FAILED after retries.")
                FAILED.append((doc_url, "failed after retries", pdf_title, book_dir))

        start += 1

    return global_idx


def process(url: str) -> str | None:
    print(f"\n-> {url}")
    soup = fetch_html(url)
    if not soup:
        FAILED.append((url, "fetch failed", "", OUTPUT_DIR))
        return None

    pdf_name, blocks, img_urls, next_url = extract(soup, url)
    print(f"   [{pdf_name}]  blocks={len(blocks)}  imgs={len(img_urls)}")

    tmp = os.path.join(OUTPUT_DIR, "_tmp")
    img_paths = []
    failed_imgs = 0
    if img_urls:
        print(f"   downloading {len(img_urls)} image(s)...")
        img_paths = download_images(img_urls, tmp)
        failed_imgs = len(img_urls) - len(img_paths)
        if failed_imgs:
            FAILED.append((url, f"{failed_imgs}/{len(img_urls)} images failed", "", OUTPUT_DIR))

    if not blocks and not img_paths:
        FAILED.append((url, "no content extracted", "", OUTPUT_DIR))
        return next_url

    base = safe_name(pdf_name)
    out = os.path.join(OUTPUT_DIR, base + ".pdf")
    counter = 1
    while os.path.exists(out):
        counter += 1
        out = os.path.join(OUTPUT_DIR, f"{base} ({counter}).pdf")
    try:
        build_pdf(pdf_name, blocks, img_paths, out)
        print(f"   saved: {os.path.basename(out)}")
    except Exception as e:
        print(f"   [!] PDF build failed: {e}")
        FAILED.append((url, f"pdf build: {e}", "", OUTPUT_DIR))
        return next_url

    # پاک‌سازی تصاویر موقت
    for p in img_paths:
        try: os.remove(p)
        except: pass

    return next_url


# ── main ──────────────────────────────────────────────────────────────────────

def save_failed_log(prefix_dir: str = None):
    if not FAILED:
        return None
    target = prefix_dir or OUTPUT_DIR
    os.makedirs(target, exist_ok=True)
    path = os.path.join(target, "_failed.tsv")
    with open(path, "w", encoding="utf-8") as f:
        # ستون‌ها: url \t reason \t intended_name \t target_dir
        f.write("# url\treason\tname\ttarget_dir\n")
        for item in FAILED:
            if len(item) == 2:
                u, reason = item
                name, td = "", target
            else:
                u, reason, name, td = item
            f.write(f"{u}\t{reason}\t{name}\t{td}\n")
    return path


def run_retry_file(path: str):
    """فایل _failed.tsv رو می‌خونه و دوباره تلاش می‌کنه"""
    if not os.path.exists(path):
        print(f"File not found: {path}")
        return
    print(f"Retrying from: {path}")
    success = 0
    still_failed = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            u, reason, name, target_dir = parts[0], parts[1], parts[2], parts[3]
            if not name:
                # سند نیست (مثلاً book page) — نمی‌تونیم دانلودش کنیم به تنهایی
                print(f"  skip (no name): {u}")
                still_failed.append((u, reason, name, target_dir))
                continue
            print(f"  retry: {name}")
            ok = download_doc_to(target_dir, u, name, max_attempts=5)
            if ok:
                print(f"     OK")
                success += 1
            else:
                print(f"     still failing")
                still_failed.append((u, "still failing after retry", name, target_dir))

    print(f"\nRetry done. Success: {success}  Still failed: {len(still_failed)}")
    # بازنویسی فایل با ناتمام‌ها
    if still_failed:
        with open(path, "w", encoding="utf-8") as f:
            f.write("# url\treason\tname\ttarget_dir\n")
            for u, reason, name, td in still_failed:
                f.write(f"{u}\t{reason}\t{name}\t{td}\n")
        print(f"Remaining list: {path}")
    else:
        os.remove(path)
        print("All retried successfully. Log removed.")


def main():
    ensure_font()
    register_fonts()
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("Modes:")
    print("  1) single document")
    print("  2) document + follow all next-page links")
    print("  3) book — download all documents from a book listing")
    print("  4) retry — re-run failed downloads from _failed.tsv")
    mode = input("Mode [1/2/3/4]: ").strip()

    if mode == "4":
        path = input("Path to _failed.tsv [downloads/_failed.tsv]: ").strip()
        if not path:
            path = os.path.join(OUTPUT_DIR, "_failed.tsv")
        run_retry_file(path)
        return

    url = input("URL: ").strip()
    if not url:
        return

    if mode == "3":
        count = run_book_mode(url)
        print(f"\nDone. {count} PDF(s) attempted in book.")
        if FAILED:
            print(f"\n[!] {len(FAILED)} failures:")
            for item in FAILED:
                if len(item) >= 4:
                    u, reason, name, _ = item
                    print(f"  - {name or '?'}\t{u}\t({reason})")
                else:
                    print(f"  - {item}")
            p = save_failed_log()
            print(f"\nFailed list saved: {p}")
            print("Run again with mode 4 to retry these.")
        else:
            print("\nAll documents processed successfully.")
        return

    follow = mode == "2"

    visited = set()
    cur = url
    count = 0
    while cur:
        if cur in visited:
            print("Duplicate, stopping.")
            break
        visited.add(cur)
        nxt = process(cur)
        count += 1
        if not follow:
            break
        if not nxt or nxt == cur:
            print("No next page.")
            break
        cur = nxt

    print(f"\nDone. {count} PDF(s) in: {OUTPUT_DIR}")

    if FAILED:
        print(f"\n[!] {len(FAILED)} failures:")
        for item in FAILED:
            u, reason = item[0], item[1]
            print(f"  - {u}   ({reason})")
        p = save_failed_log()
        print(f"\nList saved: {p}")
    else:
        print("\nAll pages processed successfully.")


if __name__ == "__main__":
    main()
