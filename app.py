"""
Video Downloader - Backend
Hỗ trợ: YouTube, TikTok, Facebook, Instagram, Reddit, Douyin, v.v.
Dùng yt-dlp để tải video chất lượng cao nhất.

Cách chạy:
    python app.py
Sau đó mở trình duyệt: http://127.0.0.1:5000
"""

import os
import io
import re
import sys
import uuid
import time
import threading
import traceback
import webbrowser
import subprocess
import sqlite3
import hashlib
import shutil
import unicodedata
from contextlib import contextmanager, nullcontext
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory, send_file, render_template_string

import yt_dlp

# ---------- Cấu hình ----------
def get_base_dir() -> Path:
    """
    Khi chạy bằng python app.py: lấy thư mục chứa file này.
    Khi đã đóng gói thành .exe (PyInstaller): lấy thư mục chứa file .exe,
    để downloads/ và cookies/ nằm cạnh file exe, không bị xóa khi tắt chương trình.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_ffmpeg_dir() -> Path:
    """
    Khi đóng gói exe, ffmpeg.exe được nhúng kèm vào thư mục tạm của PyInstaller
    (sys._MEIPASS). Khi chạy thường, dùng ffmpeg trong PATH hệ thống.
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        bundled = Path(sys._MEIPASS) / "ffmpeg_bin"
        if bundled.exists():
            return bundled
    return None


BASE_DIR = get_base_dir()
DOWNLOAD_DIR = BASE_DIR / "downloads"
COOKIES_DIR = BASE_DIR / "cookies"
DOWNLOAD_DIR.mkdir(exist_ok=True)
COOKIES_DIR.mkdir(exist_ok=True)

FFMPEG_DIR = get_ffmpeg_dir()

# Thư mục lưu video hiện tại - mặc định là downloads/, người dùng có thể đổi qua giao diện
CURRENT_OUTPUT_DIR = DOWNLOAD_DIR
OUTPUT_DIR_LOCK = threading.Lock()

app = Flask(__name__)

# Giới hạn số lượng tải đồng thời (tránh nghẽn mạng khi dán quá nhiều link)
MAX_CONCURRENT_DOWNLOADS = 3
DOWNLOAD_SEMAPHORE = threading.Semaphore(MAX_CONCURRENT_DOWNLOADS)
MAX_HTTP_403_RETRIES = 3

# Lưu trạng thái tiến trình tải theo job_id (in-memory, đủ dùng cho 1 người dùng local)
JOBS = {}
JOBS_LOCK = threading.Lock()
HISTORY_DB = BASE_DIR / "download_history.sqlite3"
VIDEO_LOCKS = {}
VIDEO_LOCKS_LOCK = threading.Lock()
FINALIZE_LOCK = threading.Lock()


@contextmanager
def history_connection():
    conn = sqlite3.connect(HISTORY_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS niches (niche_key TEXT PRIMARY KEY, name TEXT NOT NULL)")
    conn.execute("""CREATE TABLE IF NOT EXISTS downloads (
        niche_key TEXT NOT NULL, niche TEXT NOT NULL, video_key TEXT NOT NULL,
        title TEXT NOT NULL, filepath TEXT NOT NULL, digest TEXT NOT NULL,
        source_url TEXT NOT NULL DEFAULT '', group_name TEXT NOT NULL DEFAULT '',
        submitted_at REAL NOT NULL DEFAULT 0, source_order INTEGER NOT NULL DEFAULT 0,
        downloaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (niche_key, video_key))""")
    # Bổ sung dữ liệu phục vụ export mà vẫn giữ nguyên lịch sử từ các bản cũ.
    download_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(downloads)").fetchall()
    }
    for column, declaration in (
        ("source_url", "TEXT NOT NULL DEFAULT ''"),
        ("group_name", "TEXT NOT NULL DEFAULT ''"),
        ("submitted_at", "REAL NOT NULL DEFAULT 0"),
        ("source_order", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if column not in download_columns:
            conn.execute(f"ALTER TABLE downloads ADD COLUMN {column} {declaration}")
    # Lịch sử cũ dùng khóa theo từng ngách. Các chỉ mục này giúp việc dò trùng
    # toàn bộ thư viện nhanh mà không phải xóa hay di chuyển dữ liệu đã có.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_downloads_video_key ON downloads(video_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_downloads_digest ON downloads(digest)")
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def get_setting(key: str, default=None):
    with history_connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    with history_connection() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def restore_saved_output_dir():
    """Khôi phục thư mục đã chọn ở lần chạy trước nếu đường dẫn vẫn tồn tại."""
    global CURRENT_OUTPUT_DIR
    saved = get_setting("output_dir")
    if not saved:
        return
    saved_path = Path(saved)
    if saved_path.exists() and saved_path.is_dir():
        with OUTPUT_DIR_LOCK:
            CURRENT_OUTPUT_DIR = saved_path


restore_saved_output_dir()


def normalize_niche(value):
    if not isinstance(value, str):
        raise ValueError("Tên ngách phải là văn bản")
    name = unicodedata.normalize("NFC", " ".join(value.split()))
    if not name or len(name) > 80:
        raise ValueError("Nhập tên ngách từ 1 đến 80 ký tự")
    return name, name.casefold()


def niche_directory(root, name, key):
    suffix = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return Path(root) / (sanitize_filename(key).rstrip(". ")[:80] + "_" + suffix)


URL_PATTERN = re.compile(r"https?://[^\s<>\[\]()\\\"'`]+", re.IGNORECASE)
GROUP_HEADING_PATTERN = re.compile(r"^\s*(\d{1,3})([.)])\s+(.+?)\s*$")
WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def normalize_group_folder(value):
    """Chuẩn hóa tên thư mục con và không cho phép thoát khỏi thư mục cha."""
    if not isinstance(value, str):
        raise ValueError("Tên thư mục con phải là văn bản")
    display_name = unicodedata.normalize("NFC", " ".join(value.split()))
    display_name = display_name.strip("*_`# ")
    if not display_name or len(display_name) > 100:
        raise ValueError("Tên thư mục con phải có từ 1 đến 100 ký tự")
    folder_name = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "_", display_name).strip(". ")
    if not folder_name or folder_name.split(".", 1)[0].casefold() in WINDOWS_RESERVED_NAMES:
        folder_name = "_" + (folder_name or "nhom")
    return display_name, folder_name[:100].rstrip(". ")


def parse_download_text(raw_text):
    """Đọc URL thường hoặc danh sách nhóm dạng `1. Tên nhóm` + URL bên dưới."""
    if not isinstance(raw_text, str):
        raise ValueError("Danh sách link phải là văn bản")
    entries = []
    current_group = None
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        urls = []
        for match in URL_PATTERN.findall(line):
            url = match.rstrip(".,;")
            if url and url not in urls:
                urls.append(url)
        if urls:
            entries.extend({"url": url, "group": current_group} for url in urls)
            continue
        heading = GROUP_HEADING_PATTERN.match(line.rstrip("\\"))
        if heading and not URL_PATTERN.search(heading.group(3)):
            title, _ = normalize_group_folder(heading.group(3))
            current_group = f"{heading.group(1)}{heading.group(2)} {title}"
    return entries


def grouped_output_directory(base_dir, group_name):
    if not group_name:
        return Path(base_dir)
    _, folder_name = normalize_group_folder(group_name)
    return Path(base_dir) / folder_name


DEFAULT_NICHE_KEY = "chung"


def duplicate_scope(niche_key):
    """Chung là phạm vi riêng; các ngách còn lại dùng chung một phạm vi."""
    return "default" if niche_key == DEFAULT_NICHE_KEY else "named"


def find_download(video_key, niche_key=None):
    """Tìm video trong phạm vi chống trùng của ngách hiện tại.

    niche_key=None được giữ để các thao tác quản trị có thể tìm trên toàn bộ
    lịch sử. Khi tải video, luôn truyền niche_key để tách ngách Chung khỏi các
    ngách có tên khác.
    """
    with history_connection() as conn:
        if niche_key is None:
            row = conn.execute(
                "SELECT * FROM downloads WHERE video_key=? ORDER BY downloaded_at, rowid LIMIT 1",
                (video_key,),
            ).fetchone()
        elif niche_key == DEFAULT_NICHE_KEY:
            row = conn.execute(
                "SELECT * FROM downloads WHERE video_key=? AND niche_key=? "
                "ORDER BY downloaded_at, rowid LIMIT 1",
                (video_key, DEFAULT_NICHE_KEY),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM downloads WHERE video_key=? AND niche_key<>? "
                "ORDER BY downloaded_at, rowid LIMIT 1",
                (video_key, DEFAULT_NICHE_KEY),
            ).fetchone()
    return dict(row) if row else None


def mark_duplicate(job_id, record):
    duplicate_niche = dict(record).get("niche") or "không xác định"
    with JOBS_LOCK:
        JOBS[job_id].update(status="duplicate", percent=100,
            title=record["title"], filepath=record["filepath"],
            duplicate_niche=duplicate_niche,
            message=f'Đã có footage trong ngách "{duplicate_niche}" — bỏ qua để tránh tải trùng.')


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "_", name)
    return name[:150]


PLATFORM_NAME_MAP = [
    ("youtube.com", "youtube"),
    ("youtu.be", "youtube"),
    ("tiktok.com", "tiktok"),
    ("facebook.com", "facebook"),
    ("fb.watch", "facebook"),
    ("instagram.com", "instagram"),
    ("reddit.com", "reddit"),
    ("douyin.com", "douyin"),
]


def platform_name_for(url: str) -> str:
    """Trả về tên nền tảng (dùng để đặt tên file) dựa trên domain trong URL."""
    for domain, name in PLATFORM_NAME_MAP:
        if domain in url:
            return name
    return "video"


def unique_path_with_name(directory: Path, base_name: str, ext: str) -> Path:
    """
    Trả về đường dẫn file dạng <base_name><ext> trong directory.
    Nếu đã tồn tại, tự thêm số phía sau: base_name (2).ext, base_name (3).ext, ...
    """
    base_name = base_name.strip() or "video"
    candidate = directory / f"{base_name}{ext}"
    if not candidate.exists():
        return candidate
    counter = 2
    while True:
        candidate = directory / f"{base_name} ({counter}){ext}"
        if not candidate.exists():
            return candidate
        counter += 1


def cookie_file_for(url: str):
    """
    Nếu bạn có file cookie xuất từ trình duyệt (dùng để tải video riêng tư
    trên Facebook/Instagram), đặt tên file theo nền tảng trong thư mục cookies/:
        cookies/facebook.txt
        cookies/instagram.txt
        cookies/tiktok.txt
    yt-dlp sẽ tự dùng nếu phát hiện đúng nền tảng trong URL.
    """
    mapping = {
        "facebook.com": "facebook.txt",
        "fb.watch": "facebook.txt",
        "instagram.com": "instagram.txt",
        "tiktok.com": "tiktok.txt",
        "douyin.com": "douyin.txt",
        "reddit.com": "reddit.txt",
        "youtube.com": "youtube.txt",
        "youtu.be": "youtube.txt",
    }
    for domain, fname in mapping.items():
        if domain in url:
            fpath = COOKIES_DIR / fname
            if fpath.exists():
                return str(fpath)
    return None


ANSI_ESCAPE_RE = re.compile(r'\x1b\[[0-9;]*m')


def clean_error_message(msg: str) -> str:
    message = ANSI_ESCAPE_RE.sub('', msg).strip()
    if "Could not copy Chrome cookie database" in message:
        return (
            "Không thể đọc trực tiếp cookie của Chrome vì cơ sở dữ liệu đang bị khóa. "
            "Bản mới không còn dùng cách này; với video cần đăng nhập, hãy xuất cookie "
            "ra file trong thư mục cookies rồi bấm Retry."
        )
    if "Fresh cookies (not necessarily logged in) are needed" in message:
        return (
            "Douyin cần cookie phiên mới. Hãy mở link này trong Chrome, hoàn thành CAPTCHA "
            "nếu có; nếu vẫn lỗi, hãy xuất cookie vào cookies/douyin.txt rồi bấm Retry."
        )
    if "HTTP Error 403: Forbidden" in message:
        return (
            "Máy chủ đã từ chối URL media (HTTP 403). Ứng dụng đã tự tái trích xuất "
            "URL và thử lại; hãy bấm Retry nếu lỗi chỉ xảy ra tạm thời."
        )
    if "Unable to extract universal data for rehydration" in message:
        return (
            "TikTok đã thay đổi cấu trúc trang nên phiên bản yt-dlp cũ không đọc được video. "
            "Ứng dụng đã được cập nhật; hãy bấm Retry để thử lại."
        )
    if "Unexpected response from webpage request" in message:
        return (
            "TikTok đã trả về trang kiểm tra chống bot thay vì dữ liệu video. "
            "Ứng dụng đã thử giả lập Chrome và các chế độ cookie; hãy bấm Retry nếu lỗi chỉ xảy ra tạm thời."
        )
    if "[Instagram]" in message and (
        "Failed to parse JSON" in message
        or "exceeded the rate-limit" in message
        or "rate-limit reached" in message
    ):
        return (
            "Instagram đang giới hạn truy cập hoặc trả về dữ liệu rỗng. Ứng dụng đã được nâng cấp "
            "và tự chạy lần lượt từng reel; nếu vẫn lỗi, hãy xuất lại cookie Instagram mới vào "
            "cookies/instagram.txt rồi bấm Retry."
        )
    return message


def is_http_403_error(error: Exception) -> bool:
    return "HTTP Error 403" in str(error) or "403: Forbidden" in str(error)


def clean_job_partial_files(output_dir: Path, job_id: str) -> None:
    """Chỉ xóa file tải dở thuộc đúng job để lần cuối tải lại từ đầu."""
    safe_prefix = f"_tmp_{job_id}."
    for path in output_dir.glob(f"{safe_prefix}*"):
        if not path.is_file() or not path.name.startswith(safe_prefix):
            continue
        if path.name.endswith((".part", ".ytdl")):
            try:
                path.unlink()
            except OSError:
                pass


def get_owner_name(info: dict, fallback_platform: str) -> str:
    """
    Lấy tên chủ tài khoản (uploader/channel) từ metadata yt-dlp trả về.
    Nếu không có, dùng tên nền tảng làm tên dự phòng.
    """
    candidates = [
        info.get("uploader"),
        info.get("channel"),
        info.get("uploader_id"),
        info.get("creator"),
        info.get("channel_id"),
    ]
    for c in candidates:
        if c and str(c).strip() and str(c).strip().lower() not in ("na", "none", "unknown"):
            return sanitize_filename(str(c).strip())
    return fallback_platform


def authentication_attempts_for(url: str) -> list[dict]:
    """
    Trả về các cách xác thực theo thứ tự sẽ thử.

    Các nền tảng mạng xã hội thử video công khai trước, sau đó mới dùng file cookie
    đã xuất nếu có. Tuyệt đối không đọc trực tiếp cơ sở dữ liệu cookie của Chrome:
    trên Windows file này bị Chrome khóa khi đang chạy và gây PermissionError #7271.
    """
    cookie_path = cookie_file_for(url)
    public_first_domains = (
        "instagram.com", "facebook.com", "fb.watch", "tiktok.com", "douyin.com"
    )
    if any(domain in url for domain in public_first_domains):
        # Với Instagram hàng loạt, dùng cookie trước để không tiêu tốn hạn mức ẩn danh.
        # Nếu cookie đã cũ, vẫn fallback sang chế độ công khai.
        if "instagram.com" in url and cookie_path:
            return [{"cookiefile": cookie_path}, {}]
        attempts = [{}]
        if cookie_path:
            attempts.append({"cookiefile": cookie_path})
        return attempts

    if cookie_path:
        return [{"cookiefile": cookie_path}]
    return [{}]


def progress_hook_factory(job_id):
    def hook(d):
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if not job:
                return
            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes", 0)
                percent = (downloaded / total * 100) if total else 0
                job["percent"] = round(percent, 1)
                job["status"] = "downloading"
                job["speed"] = d.get("speed")
                job["eta"] = d.get("eta")
            elif status == "finished":
                job["percent"] = 100
                job["status"] = "processing"  # đang ghép/convert
    return hook


def run_download(job_id: str, url: str):
    with JOBS_LOCK:
        JOBS[job_id]["status"] = "waiting"

    with DOWNLOAD_SEMAPHORE:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "queued"

        with JOBS_LOCK:
            job = dict(JOBS[job_id])
        output_dir = Path(job["output_dir"])
        niche_key = job["niche_key"]
        prevent_duplicates = job.get("prevent_duplicates", True)
        video_lock = None
        locked_key = None

        # Dùng tên tạm dựa trên job_id để tránh trùng lặp trong lúc đang tải,
        # sau khi tải xong sẽ đổi tên thành <platform>.mp4 (hoặc có số phía sau nếu trùng)
        temp_outtmpl = str(output_dir / f"_tmp_{job_id}.%(ext)s")

        base_ydl_opts = {
            # Chọn chất lượng video+audio tốt nhất sẵn có, ưu tiên mp4 để dễ phát
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "outtmpl": temp_outtmpl,
            "noplaylist": True,
            "progress_hooks": [progress_hook_factory(job_id)],
            "quiet": True,
            "no_warnings": True,
            "retries": 10,
            "fragment_retries": 10,
            "extractor_retries": 3,
            "file_access_retries": 3,
            "socket_timeout": 30,
            # Tránh một số CDN trả 403 cho kết nối IPv6 không ổn định.
            "source_address": "0.0.0.0",
            "ignoreerrors": False,
        }

        if FFMPEG_DIR:
            base_ydl_opts["ffmpeg_location"] = str(FFMPEG_DIR)

        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            auth_attempts = authentication_attempts_for(url)
            last_error = None
            for attempt_number, auth_options in enumerate(auth_attempts, start=1):
                ydl_opts = {**base_ydl_opts, **auth_options}
                download_succeeded = False
                for transfer_attempt in range(1, MAX_HTTP_403_RETRIES + 1):
                    try:
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            # Mỗi vòng tạo YoutubeDL mới để extractor cấp URL media mới.
                            info = ydl.extract_info(url, download=False)
                            if not info or info.get("_type") in ("playlist", "multi_video") or not info.get("id"):
                                raise ValueError("Hãy dùng link của một video riêng lẻ")
                            extractor = str(info.get("extractor_key") or info.get("extractor") or platform_name_for(url)).casefold()
                            video_key = extractor + ":" + str(info["id"])
                            # Chung là một phạm vi riêng. Các ngách có tên khác
                            # vẫn dùng chung khóa để chống trùng chéo với nhau.
                            key = (duplicate_scope(niche_key), video_key)
                            if prevent_duplicates and locked_key != key:
                                if video_lock is not None:
                                    video_lock.release()
                                with VIDEO_LOCKS_LOCK:
                                    video_lock = VIDEO_LOCKS.setdefault(key, threading.Lock())
                                with JOBS_LOCK:
                                    JOBS[job_id]["status"] = "checking" if prevent_duplicates else "queued"
                                video_lock.acquire()
                                locked_key = key
                            existing = find_download(video_key, niche_key) if prevent_duplicates else None
                            if existing:
                                mark_duplicate(job_id, existing)
                                return
                            info = ydl.process_ie_result(info, download=True)
                            temp_filename = ydl.prepare_filename(info)
                        download_succeeded = True
                        break
                    except Exception as attempt_error:
                        last_error = attempt_error
                        if not is_http_403_error(attempt_error):
                            break
                        if transfer_attempt >= MAX_HTTP_403_RETRIES:
                            break

                        # Lần đầu giữ .part để tiếp tục tải. Trước lần cuối, bỏ
                        # phần tải dở của job này để tránh CDN từ chối Range cũ.
                        if transfer_attempt == MAX_HTTP_403_RETRIES - 1:
                            clean_job_partial_files(output_dir, job_id)
                        with JOBS_LOCK:
                            JOBS[job_id]["status"] = "retrying"
                            JOBS[job_id]["retry_attempt"] = transfer_attempt
                            JOBS[job_id]["percent"] = 0
                        time.sleep(1.5 * transfer_attempt)

                if download_succeeded:
                    break
                if attempt_number == len(auth_attempts):
                    raise last_error
            else:
                raise last_error or RuntimeError("Không thể tải video")

            # Sau khi merge, đuôi file sẽ là .mp4
            base, ext = os.path.splitext(temp_filename)
            temp_final = base + ".mp4"
            if not os.path.exists(temp_final):
                temp_final = temp_filename  # fallback nếu không phải mp4
                ext = os.path.splitext(temp_filename)[1]
            else:
                ext = ".mp4"

            # Đổi tên file theo tên chủ tài khoản, tự thêm số nếu trùng tên
            platform = platform_name_for(url)
            owner_name = get_owner_name(info, platform)
            digest = hashlib.sha256()
            with open(temp_final, "rb") if prevent_duplicates else nullcontext() as downloaded:
                if downloaded is not None:
                    for chunk in iter(lambda: downloaded.read(1024 * 1024), b""):
                        digest.update(chunk)
            with FINALIZE_LOCK:
                with history_connection() if prevent_duplicates else nullcontext() as conn:
                    if not prevent_duplicates:
                        existing = None
                    elif niche_key == DEFAULT_NICHE_KEY:
                        existing = conn.execute(
                            "SELECT * FROM downloads WHERE digest=? AND niche_key=? "
                            "ORDER BY downloaded_at, rowid LIMIT 1",
                            (digest.hexdigest(), DEFAULT_NICHE_KEY),
                        ).fetchone()
                    else:
                        existing = conn.execute(
                            "SELECT * FROM downloads WHERE digest=? AND niche_key<>? "
                            "ORDER BY downloaded_at, rowid LIMIT 1",
                            (digest.hexdigest(), DEFAULT_NICHE_KEY),
                        ).fetchone()
                    if existing:
                        # Ghi nhớ mã video thay thế tại chính ngách gốc để lần sau có
                        # thể chặn ngay từ metadata mà không cần tải lại để băm file.
                        conn.execute("INSERT OR IGNORE INTO downloads (niche_key,niche,video_key,title,filepath,digest) VALUES (?,?,?,?,?,?)",
                                     (existing["niche_key"], existing["niche"], video_key,
                                      existing["title"], existing["filepath"], digest.hexdigest()))
                        Path(temp_final).unlink()
                        mark_duplicate(job_id, existing)
                        return
                    final_path = unique_path_with_name(output_dir, owner_name, ext)
                    os.replace(temp_final, final_path)
                    try:
                        if conn is not None:
                            conn.execute("""INSERT OR IGNORE INTO downloads
                                (niche_key,niche,video_key,title,filepath,digest,
                                 source_url,group_name,submitted_at,source_order)
                                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                                         (niche_key, job["niche"], video_key,
                                          info.get("title") or platform, str(final_path),
                                          digest.hexdigest(), job.get("source_url", url),
                                          job.get("group") or "",
                                          job.get("submitted_at", 0),
                                          job.get("source_order", 0)))
                            conn.commit()
                    except Exception:
                        os.replace(final_path, temp_final)
                        raise

            with JOBS_LOCK:
                JOBS[job_id]["status"] = "done"
                JOBS[job_id]["percent"] = 100
                JOBS[job_id]["filename"] = final_path.name
                JOBS[job_id]["filepath"] = str(final_path)
                JOBS[job_id]["title"] = info.get("title", platform)
        except Exception as e:
            traceback.print_exc()
            with JOBS_LOCK:
                JOBS[job_id]["status"] = "error"
                JOBS[job_id]["error"] = clean_error_message(str(e))
        finally:
            if video_lock is not None:
                video_lock.release()


@app.route("/")
def index():
    return render_template_string(HTML_PAGE)


@app.route("/api/browse_folder", methods=["POST"])
def api_browse_folder():
    """Mở hộp thoại chọn thư mục gốc của Windows, trả về đường dẫn đã chọn."""
    global CURRENT_OUTPUT_DIR
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        with OUTPUT_DIR_LOCK:
            initial = str(CURRENT_OUTPUT_DIR)
        selected = filedialog.askdirectory(initialdir=initial, title="Chọn thư mục lưu video")
        root.destroy()

        if selected:
            with OUTPUT_DIR_LOCK:
                CURRENT_OUTPUT_DIR = Path(selected)
            set_setting("output_dir", selected)
            return jsonify({"folder": selected})
        else:
            with OUTPUT_DIR_LOCK:
                current = str(CURRENT_OUTPUT_DIR)
            return jsonify({"folder": current, "cancelled": True})
    except Exception as e:
        with OUTPUT_DIR_LOCK:
            current = str(CURRENT_OUTPUT_DIR)
        return jsonify({"folder": current, "error": str(e)}), 200


@app.route("/api/get_folder")
def api_get_folder():
    with OUTPUT_DIR_LOCK:
        return jsonify({"folder": str(CURRENT_OUTPUT_DIR)})


@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json(force=True)
    prevent_duplicates = (data or {}).get("prevent_duplicates", True)
    if not isinstance(prevent_duplicates, bool):
        return jsonify({"error": "Trạng thái chống trùng phải là bật hoặc tắt"}), 400
    niche, niche_key = None, None
    if prevent_duplicates:
        try:
            niche, niche_key = normalize_niche((data or {}).get("niche", "Chung"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    with OUTPUT_DIR_LOCK:
        selected_root = Path(CURRENT_OUTPUT_DIR)
        normal_output_dir = niche_directory(selected_root, niche, niche_key) if prevent_duplicates else selected_root
    raw_text = (data or {}).get("text")
    try:
        if raw_text is not None:
            entries = parse_download_text(raw_text)
        else:
            urls = (data or {}).get("urls", [])
            if isinstance(urls, str):
                urls = [urls]
            entries = [{"url": url.strip(), "group": None} for url in urls if isinstance(url, str) and url.strip()]
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not entries:
        return jsonify({"error": "Thiếu URL"}), 400

    prepared_entries = []
    try:
        for entry in entries:
            group = entry.get("group")
            # Danh sách có tiêu đề nhóm tạo thư mục con trực tiếp trong folder
            # cha mà người dùng đã chọn. Danh sách thường giữ cách lưu cũ.
            output_dir = grouped_output_directory(selected_root, group) if group else normal_output_dir
            if group:
                # Tạo toàn bộ thư mục nhóm ngay khi nhận danh sách, kể cả các
                # video còn đang chờ giới hạn tải đồng thời.
                output_dir.mkdir(parents=True, exist_ok=True)
            prepared_entries.append({**entry, "output_dir": str(output_dir)})
    except OSError as e:
        return jsonify({"error": f"Không thể tạo thư mục con: {e}"}), 500

    job_ids = []
    response_jobs = []
    submitted_at = time.time()
    for source_order, entry in enumerate(prepared_entries):
        url = entry["url"]
        group = entry.get("group")
        output_dir = entry["output_dir"]
        job_id = str(uuid.uuid4())
        with JOBS_LOCK:
            JOBS[job_id] = {"status": "queued", "percent": 0, "url": url,
                           "niche": niche, "niche_key": niche_key, "output_dir": output_dir,
                           "group": group, "source_url": url,
                           "submitted_at": submitted_at, "source_order": source_order,
                           "prevent_duplicates": prevent_duplicates}
        thread = threading.Thread(target=run_download, args=(job_id, url), daemon=True)
        thread.start()
        job_ids.append(job_id)
        response_jobs.append({"job_id": job_id, "url": url, "group": group})

    return jsonify({"job_ids": job_ids, "jobs": response_jobs, "niche": niche})


@app.route("/api/niches", methods=["GET", "POST", "DELETE"])
def api_niches():
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        try:
            name, key = normalize_niche(data.get("name", ""))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        with history_connection() as conn:
            existing = conn.execute("SELECT name FROM niches WHERE niche_key=? UNION ALL SELECT niche FROM downloads WHERE niche_key=? LIMIT 1", (key, key)).fetchone()
            if existing:
                return jsonify({"error": "Ngách này đã tồn tại", "name": existing[0]}), 409
            conn.execute("INSERT INTO niches (niche_key,name) VALUES (?,?)", (key, name))
        return jsonify({"name": name}), 201

    if request.method == "DELETE":
        data = request.get_json(force=True) or {}
        try:
            name, key = normalize_niche(data.get("name", ""))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        if key == "chung":
            return jsonify({"error": "Không thể xóa ngách Chung mặc định"}), 400

        with JOBS_LOCK:
            active = any(
                job.get("niche_key") == key
                and job.get("status") not in {"done", "duplicate", "error"}
                for job in JOBS.values()
            )
        if active:
            return jsonify({"error": "Ngách đang có video được xử lý. Hãy chờ hoàn tất rồi xóa."}), 409

        with history_connection() as conn:
            niche_row = conn.execute(
                "SELECT name FROM niches WHERE niche_key=? UNION ALL "
                "SELECT niche FROM downloads WHERE niche_key=? LIMIT 1",
                (key, key),
            ).fetchone()
            if not niche_row:
                return jsonify({"error": "Không tìm thấy ngách"}), 404
            stored_name = niche_row["name"] if "name" in niche_row.keys() else niche_row[0]
            download_rows = conn.execute(
                "SELECT rowid, filepath FROM downloads WHERE niche_key=?", (key,)
            ).fetchall()

        expected_dirname = niche_directory(Path("."), stored_name, key).name
        with OUTPUT_DIR_LOCK:
            candidate_dirs = {
                Path(os.path.abspath(niche_directory(CURRENT_OUTPUT_DIR, stored_name, key)))
            }
        for row in download_rows:
            parent = Path(os.path.abspath(Path(row["filepath"]).parent))
            # Hỗ trợ dữ liệu của các bản trước từng đặt nhóm trong thư mục ngách.
            for ancestor in (parent, *parent.parents):
                if ancestor.name == expected_dirname:
                    candidate_dirs.add(ancestor)
                    break

        removed_dirs = []
        removed_files = 0
        deleted_filepaths = set()
        try:
            for directory in candidate_dirs:
                # Chỉ cho phép xóa đúng thư mục có tên được sinh từ ngách và hash.
                if directory.name != expected_dirname or not directory.exists():
                    continue
                if directory.is_symlink():
                    directory.unlink()
                else:
                    removed_files += sum(1 for path in directory.rglob("*") if path.is_file())
                    shutil.rmtree(directory)
                removed_dirs.append(str(directory))

            removed_dir_set = {Path(path) for path in removed_dirs}
            for row in download_rows:
                filepath = Path(os.path.abspath(row["filepath"]))
                if any(filepath.is_relative_to(directory) for directory in removed_dir_set):
                    deleted_filepaths.add(row["filepath"])
                    continue
                # Danh sách chia nhóm được lưu trực tiếp trong folder cha nên
                # chỉ xóa đúng file thuộc ngách; không xóa video của ngách khác.
                if row["filepath"] in deleted_filepaths or not filepath.exists():
                    continue
                if filepath.is_file() or filepath.is_symlink():
                    filepath.unlink()
                    removed_files += 1
                    deleted_filepaths.add(row["filepath"])
                    try:
                        filepath.parent.rmdir()
                        removed_dirs.append(str(filepath.parent))
                    except OSError:
                        pass
        except OSError as e:
            return jsonify({"error": f"Không thể xóa thư mục ngách: {e}"}), 500

        with history_connection() as conn:
            # Xóa cả các bản ghi bí danh cùng trỏ tới file vừa bị xóa để lịch sử
            # chống trùng không giữ đường dẫn không còn tồn tại.
            conn.execute("DELETE FROM downloads WHERE niche_key=?", (key,))
            if deleted_filepaths:
                placeholders = ",".join("?" for _ in deleted_filepaths)
                conn.execute(
                    f"DELETE FROM downloads WHERE filepath IN ({placeholders})",
                    tuple(deleted_filepaths),
                )
            conn.execute("DELETE FROM niches WHERE niche_key=?", (key,))
            selected_niche = conn.execute(
                "SELECT value FROM settings WHERE key='selected_niche'"
            ).fetchone()
            if selected_niche and normalize_niche(selected_niche["value"])[1] == key:
                conn.execute(
                    "INSERT INTO settings(key,value) VALUES ('selected_niche','Chung') "
                    "ON CONFLICT(key) DO UPDATE SET value='Chung'"
                )
        return jsonify({
            "deleted": name,
            "removed_files": removed_files,
            "removed_directories": removed_dirs,
            "selected_niche": "Chung",
        })

    with history_connection() as conn:
        rows = conn.execute("""SELECT n.niche_key, MIN(n.name) AS name, COUNT(DISTINCT d.filepath) AS count
            FROM (SELECT niche_key,name FROM niches UNION SELECT niche_key,niche AS name FROM downloads) n
            LEFT JOIN downloads d ON d.niche_key=n.niche_key GROUP BY n.niche_key ORDER BY name""").fetchall()
    return jsonify({
        "niches": [dict(row) for row in rows],
        "selected_niche": get_setting("selected_niche", "Chung"),
    })


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        if "selected_niche" in data:
            try:
                name, _ = normalize_niche(data["selected_niche"])
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            set_setting("selected_niche", name)
        return jsonify({"selected_niche": get_setting("selected_niche", "Chung")})
    with OUTPUT_DIR_LOCK:
        folder = str(CURRENT_OUTPUT_DIR)
    return jsonify({
        "selected_niche": get_setting("selected_niche", "Chung"),
        "folder": folder,
    })


def export_group_sort_key(group_name):
    match = re.match(r"^\s*(\d+)[.)]\s*", group_name or "")
    if match:
        return (0, int(match.group(1)), (group_name or "").casefold())
    if group_name:
        return (1, 0, group_name.casefold())
    return (2, 0, "")


@app.route("/api/export_links")
def api_export_links():
    try:
        niche, niche_key = normalize_niche(request.args.get("niche", "Chung"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    with history_connection() as conn:
        rows = conn.execute("""SELECT source_url, group_name, submitted_at, source_order, rowid
            FROM downloads
            WHERE niche_key=? AND source_url<>''
            ORDER BY submitted_at, source_order, rowid""", (niche_key,)).fetchall()
    if not rows:
        return jsonify({
            "error": "Ngách này chưa có link được ghi nhận. Lịch sử từ bản cũ không lưu URL gốc."
        }), 404

    grouped = {}
    seen_urls = set()
    for row in rows:
        url = row["source_url"].strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        grouped.setdefault(row["group_name"] or "", []).append(url)

    lines = []
    fallback_number = 1
    for group_name in sorted(grouped, key=export_group_sort_key):
        if group_name:
            heading = group_name
            match = re.match(r"^\s*(\d+)[.)]\s*", group_name)
            if match:
                fallback_number = max(fallback_number, int(match.group(1)) + 1)
        else:
            heading = f"{fallback_number}. Không phân nhóm"
            fallback_number += 1
        if lines:
            lines.append("")
        lines.append(heading)
        lines.extend(f"     {url}" for url in grouped[group_name])

    content = "\r\n".join(lines) + "\r\n"
    filename = f"danh_sach_link_{sanitize_filename(niche).strip() or 'ngach'}.txt"
    return send_file(
        io.BytesIO(content.encode("utf-8-sig")),
        mimetype="text/plain; charset=utf-8",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/api/retry", methods=["POST"])
def api_retry():
    """Tạo job mới cho các job đã lỗi và trả về ánh xạ job cũ -> job mới."""
    data = request.get_json(force=True)
    old_job_ids = (data or {}).get("job_ids", [])
    if not isinstance(old_job_ids, list) or not old_job_ids:
        return jsonify({"error": "Không có URL lỗi để thử lại"}), 400

    retry_jobs = []
    with JOBS_LOCK:
        for old_job_id in old_job_ids:
            old_job = JOBS.get(old_job_id)
            if old_job and old_job.get("status") == "error" and old_job.get("url"):
                new_job_id = str(uuid.uuid4())
                JOBS[new_job_id] = {
                    "status": "queued",
                    "percent": 0,
                    "url": old_job["url"],
                    "retry_of": old_job_id,
                    "prevent_duplicates": old_job.get("prevent_duplicates", True),
                    "niche": old_job["niche"],
                    "niche_key": old_job["niche_key"],
                    "output_dir": old_job["output_dir"],
                    "group": old_job.get("group"),
                    "source_url": old_job.get("source_url", old_job["url"]),
                    "submitted_at": old_job.get("submitted_at", 0),
                    "source_order": old_job.get("source_order", 0),
                }
                retry_jobs.append((old_job_id, new_job_id, old_job["url"]))

    if not retry_jobs:
        return jsonify({"error": "Không tìm thấy URL nào đang bị lỗi"}), 400

    for _, new_job_id, url in retry_jobs:
        threading.Thread(
            target=run_download, args=(new_job_id, url), daemon=True
        ).start()

    return jsonify({
        "jobs": [
            {"old_job_id": old_id, "job_id": new_id}
            for old_id, new_id, _ in retry_jobs
        ]
    })


@app.route("/api/status_batch", methods=["POST"])
def api_status_batch():
    data = request.get_json(force=True)
    ids = (data or {}).get("job_ids", [])
    with JOBS_LOCK:
        result = {jid: JOBS.get(jid, {"status": "unknown"}) for jid in ids}
    return jsonify(result)


@app.route("/api/status/<job_id>")
def api_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Không tìm thấy job"}), 404
    return jsonify(job)


@app.route("/files/<job_id>")
def serve_file(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or "filepath" not in job:
        return jsonify({"error": "Không tìm thấy file"}), 404
    filepath = Path(job["filepath"])
    return send_from_directory(filepath.parent, filepath.name, as_attachment=True)


HTML_PAGE = """
<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<title>Video Downloader</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  :root {
    --bg: #0f1115;
    --card: #171a21;
    --accent: #6c5ce7;
    --accent2: #00d4a0;
    --text: #e8e8ef;
    --muted: #8a8d98;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
    background: linear-gradient(160deg, #0f1115 0%, #181c26 100%);
    color: var(--text);
    min-height: 100vh;
    display: flex;
    align-items: flex-start;
    justify-content: center;
    padding: 48px 16px;
  }
  .container { width: 100%; max-width: 640px; }
  h1 { font-size: 26px; margin-bottom: 4px; }
  p.subtitle { color: var(--muted); margin-top: 0; margin-bottom: 28px; font-size: 14px; }
  .card {
    background: var(--card);
    border-radius: 16px;
    padding: 24px;
    box-shadow: 0 10px 30px rgba(0,0,0,0.35);
    border: 1px solid #232733;
  }
  .input-row { display: flex; gap: 10px; }
  input[type=text] {
    flex: 1;
    padding: 14px 16px;
    border-radius: 10px;
    border: 1px solid #2a2f3d;
    background: #11131a;
    color: var(--text);
    font-size: 15px;
    outline: none;
  }
  input[type=text]:focus { border-color: var(--accent); }
  button {
    padding: 14px 22px;
    border-radius: 10px;
    border: none;
    background: var(--accent);
    color: white;
    font-weight: 600;
    font-size: 15px;
    cursor: pointer;
    transition: transform .15s ease, background .15s ease;
  }
  button:hover { background: #5a4bd4; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  .platforms { margin-top: 16px; display: flex; gap: 8px; flex-wrap: wrap; }
  .badge {
    font-size: 12px;
    color: var(--muted);
    border: 1px solid #2a2f3d;
    border-radius: 20px;
    padding: 4px 10px;
  }
  .jobs { margin-top: 24px; display: flex; flex-direction: column; gap: 12px; }
  .job {
    background: #11131a;
    border: 1px solid #232733;
    border-radius: 12px;
    padding: 14px 16px;
  }
  .job .title { font-size: 14px; margin-bottom: 8px; word-break: break-all; color: var(--muted); }
  .progress-bar {
    height: 8px;
    background: #1f2330;
    border-radius: 6px;
    overflow: hidden;
  }
  .progress-fill {
    height: 100%;
    background: linear-gradient(90deg, var(--accent), var(--accent2));
    width: 0%;
    transition: width .3s ease;
  }
  .meta { display: flex; justify-content: space-between; margin-top: 6px; font-size: 12px; color: var(--muted); }
  .status-done { color: var(--accent2); font-weight: 600; }
  .status-error { color: #ff6b6b; font-weight: 600; }
  .retry-button {
    display: none;
    background: #e17055;
    padding: 8px 16px;
    font-size: 13px;
  }
  .retry-button:hover { background: #c85d45; }
  a.download-link {
    display: inline-block;
    margin-top: 8px;
    color: var(--accent2);
    text-decoration: none;
    font-weight: 600;
    font-size: 14px;
  }
  a.download-link:hover { text-decoration: underline; }
  .note {
    margin-top: 28px;
    font-size: 12px;
    color: var(--muted);
    line-height: 1.6;
  }
  [hidden] { display: none !important; }
  body { padding: 28px 16px; }
  .container { max-width: 720px; }
  .card { padding: 22px; }
  .settings { padding: 14px; background: #11131a; border: 1px solid #2a2f3d; border-radius: 12px; margin-bottom: 18px; }
  .setting-heading, .compact-row { display: flex; align-items: center; gap: 10px; }
  .setting-heading { justify-content: space-between; }
  .switch { display: flex; gap: 8px; align-items: center; font-size: 14px; font-weight: 600; cursor: pointer; }
  .switch input { accent-color: var(--accent2); width: 18px; height: 18px; }
  .hint { color: var(--muted); font-size: 12px; margin: 8px 0 0; line-height: 1.5; }
  .compact-row { margin-top: 12px; }
  .compact-row select, .compact-row input { min-width: 0; flex: 1; background: #171a21; color: var(--text); border: 1px solid #34394b; border-radius: 8px; padding: 10px; font: inherit; font-size: 13px; }
  .secondary { padding: 10px 14px; font-size: 13px; background: #2b3042; white-space: nowrap; }
  .danger { padding: 10px 14px; font-size: 13px; background: #7f2f38; white-space: nowrap; }
  .danger:hover { background: #a33b46; }
  .danger:disabled { opacity: .45; cursor: not-allowed; }
  .field-label { display: block; font-size: 13px; margin-bottom: 8px; color: var(--muted); }
  .input-row { flex-direction: column; }
  .input-row textarea { width: 100%; }
  #downloadBtn { align-self: flex-end; padding: 11px 24px; }
  .note { margin-top: 16px; }
  .note summary { cursor: pointer; }
  @media (max-width: 480px) { .card { padding: 14px; } .compact-row { flex-wrap: wrap; } .compact-row select { flex-basis: 60%; } }
</style>
</head>
<body>
<div class="container">
  <h1>🎬 Video Downloader</h1>
  <p class="subtitle">Dán link video, tải về chất lượng tốt nhất sẵn có (mp4).</p>

  <div class="card">
    <div class="settings">
      <label class="switch">
        <input type="checkbox" id="preventDuplicatesInput" checked>
        Chống trùng footage toàn bộ ngách
      </label>
      <p id="duplicateHelp" class="hint" aria-live="polite"></p>
      <div id="nicheSection">
        <div class="compact-row">
          <label for="nicheInput" class="hint">Ngách</label>
          <select id="nicheInput"><option value="Chung">Chung</option></select>
          <button id="showCreateNicheBtn" class="secondary" aria-expanded="false" aria-controls="createNicheForm">+ Tạo ngách</button>
          <button id="exportLinksBtn" class="secondary" type="button">Xuất link .txt</button>
          <button id="deleteNicheBtn" class="danger" type="button">Xóa ngách</button>
        </div>
        <form id="createNicheForm" class="compact-row" hidden>
          <input type="text" id="newNicheInput" maxlength="80" required aria-label="Tên ngách mới" placeholder="Tên ngách mới, ví dụ: Du lịch">
          <button id="saveNicheBtn" class="secondary" type="submit">Tạo</button>
          <button id="cancelNicheBtn" class="secondary" type="button">Hủy</button>
        </form>
        <p id="nicheSummary" class="hint" aria-live="polite"></p>
      </div>
    </div>
    <div style="margin-bottom:16px; display:flex; gap:10px; align-items:center;">
      <span style="font-size:13px; color:var(--muted); white-space:nowrap;">📁 Lưu vào:</span>
      <span id="folderPathText" style="font-size:13px; color:var(--text); flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; background:#11131a; padding:8px 12px; border-radius:8px; border:1px solid #2a2f3d;"></span>
      <button id="chooseFolderBtn" style="padding:8px 14px; font-size:13px;">Chọn thư mục</button>
    </div>
    <label class="field-label" for="urlInput">Link video · mỗi dòng một link, hoặc chia nhóm bằng “1. Tên thư mục”</label>
    <p class="hint">Danh sách có tiêu đề nhóm sẽ tạo ngay các thư mục con trực tiếp trong thư mục cha đã chọn.</p>
    <div class="input-row">
      <textarea id="urlInput" rows="7" placeholder="Dán link thông thường, hoặc theo nhóm:
1. Hài hước
https://www.facebook.com/reel/...
https://www.instagram.com/reel/...
2. Âu yếm
https://www.facebook.com/reel/..."></textarea>
      <button id="downloadBtn">Tải tất cả</button>
    </div>
    <div class="platforms">
      <span class="badge">YouTube</span>
      <span class="badge">TikTok</span>
      <span class="badge">Facebook</span>
      <span class="badge">Instagram</span>
      <span class="badge">Reddit</span>
      <span class="badge">Douyin</span>
    </div>
    <div style="margin-top:14px; display:flex; gap:10px; justify-content:flex-end; align-items:center; flex-wrap:wrap;">
      <span id="summaryText" style="font-size:13px; color:var(--muted);"></span>
      <button id="retryBtn" class="retry-button">↻ Retry URL bị lỗi</button>
      <button id="downloadAllBtn" style="display:none; background: var(--accent2); padding: 8px 16px; font-size: 13px;">Tải về toàn bộ video đã xong</button>
    </div>

    <div class="jobs" id="jobsContainer"></div>
  </div>

  <details class="note"><summary>Hướng dẫn & lưu ý</summary>
    ⚠️ Chỉ tải nội dung bạn có quyền tải (video công khai, video của bạn, hoặc được phép theo điều khoản sử dụng).
    Với Facebook/Instagram, một số video riêng tư hoặc giới hạn cần file cookie đăng nhập đặt trong thư mục
    <code>cookies/</code> (xem hướng dẫn kèm theo). Ứng dụng không đọc trực tiếp cookie từ Chrome nên bạn có thể để Chrome mở.
    Tối đa 3 link được tải đồng thời, các link còn lại sẽ tự xếp hàng chờ. Với Douyin: hãy mở video một lần trong Chrome,
    hoàn thành CAPTCHA nếu có rồi xuất cookie vào <code>cookies/douyin.txt</code> nếu video vẫn yêu cầu xác thực.
  </details>
</div>

<style>
  textarea#urlInput {
    flex: 1;
    padding: 14px 16px;
    border-radius: 10px;
    border: 1px solid #2a2f3d;
    background: #11131a;
    color: var(--text);
    font-size: 14px;
    outline: none;
    resize: vertical;
    font-family: inherit;
    line-height: 1.5;
  }
  textarea#urlInput:focus { border-color: var(--accent); }
</style>

<script>
const jobsContainer = document.getElementById('jobsContainer');
const urlInput = document.getElementById('urlInput');
const downloadBtn = document.getElementById('downloadBtn');
const downloadAllBtn = document.getElementById('downloadAllBtn');
const retryBtn = document.getElementById('retryBtn');
const summaryText = document.getElementById('summaryText');
const chooseFolderBtn = document.getElementById('chooseFolderBtn');
const folderPathText = document.getElementById('folderPathText');
const nicheInput = document.getElementById('nicheInput');
const deleteNicheBtn = document.getElementById('deleteNicheBtn');
const exportLinksBtn = document.getElementById('exportLinksBtn');
const preventDuplicatesInput = document.getElementById('preventDuplicatesInput');
preventDuplicatesInput.checked = true;
function updateDuplicateHelp() {
  document.getElementById('nicheSection').hidden = !preventDuplicatesInput.checked;
  nicheInput.disabled = !preventDuplicatesInput.checked;
  exportLinksBtn.disabled = !preventDuplicatesInput.checked;
  document.getElementById('duplicateHelp').textContent = preventDuplicatesInput.checked
    ? 'Ngách Chung kiểm tra trùng riêng. Các ngách còn lại vẫn kiểm tra chéo với nhau.'
    : 'Tải thẳng vào thư mục đã chọn. Không phân ngách, không ghi lịch sử.';
}
preventDuplicatesInput.addEventListener('change', updateDuplicateHelp);
updateDuplicateHelp();
function updateDeleteNicheButton() {
  deleteNicheBtn.disabled = !nicheInput.value || nicheInput.value.toLocaleLowerCase('vi') === 'chung';
}
async function saveSelectedNiche() {
  if (!nicheInput.value) return;
  try {
    await fetch('/api/settings', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({selected_niche: nicheInput.value})});
  } catch (e) { /* lần tải trang sau sẽ dùng giá trị đã lưu gần nhất */ }
}
async function refreshNiches(selectedName = null) {
  try {
    const res = await fetch('/api/niches');
    if (!res.ok) throw new Error('history');
    const data = await res.json();
    const options = nicheInput;
    options.replaceChildren();
    if (!data.niches.some(n => n.name.toLocaleLowerCase('vi') === 'chung')) {
      data.niches.unshift({name: 'Chung', count: 0});
    }
    data.niches.forEach(n => {
      const option = document.createElement('option');
      option.value = n.name;
      option.textContent = n.name + ' · ' + n.count + ' video';
      options.appendChild(option);
    });
    const desiredName = selectedName || data.selected_niche || 'Chung';
    nicheInput.value = desiredName;
    if (!nicheInput.value) nicheInput.selectedIndex = 0;
    updateDeleteNicheButton();
    document.getElementById('nicheSummary').textContent = 'Chọn ngách hoặc bấm + Tạo ngách để thêm mới.';
  } catch (e) {
    document.getElementById('nicheSummary').textContent = 'Không đọc được lịch sử ngách. Hãy tải lại trang.';
  }
}
refreshNiches();
nicheInput.addEventListener('change', () => {
  updateDeleteNicheButton();
  saveSelectedNiche();
});
const createNicheForm = document.getElementById('createNicheForm');
const showCreateNicheBtn = document.getElementById('showCreateNicheBtn');
function showNicheForm(show) {
  createNicheForm.hidden = !show;
  showCreateNicheBtn.setAttribute('aria-expanded', String(show));
  if (show) document.getElementById('newNicheInput').focus();
}
showCreateNicheBtn.addEventListener('click', () => showNicheForm(createNicheForm.hidden));
document.getElementById('cancelNicheBtn').addEventListener('click', () => showNicheForm(false));
createNicheForm.addEventListener('submit', async event => {
  event.preventDefault();
  if (!preventDuplicatesInput.checked) return;
  const saveButton = document.getElementById('saveNicheBtn');
  saveButton.disabled = true;
  try {
    const res = await fetch('/api/niches', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: document.getElementById('newNicheInput').value})});
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Không thể tạo ngách');
    await refreshNiches(data.name);
    await saveSelectedNiche();
    createNicheForm.reset();
    showNicheForm(false);
    document.getElementById('nicheSummary').textContent = 'Đã tạo ngách: ' + data.name;
  } catch (err) {
    document.getElementById('nicheSummary').textContent = err.message;
  } finally { saveButton.disabled = false; }
});

deleteNicheBtn.addEventListener('click', async () => {
  const name = nicheInput.value;
  if (!name || name.toLocaleLowerCase('vi') === 'chung') return;
  const confirmed = window.confirm(
    `Xóa ngách "${name}"? Toàn bộ video, thư mục và lịch sử liên quan sẽ bị xóa vĩnh viễn.`
  );
  if (!confirmed) return;
  deleteNicheBtn.disabled = true;
  deleteNicheBtn.textContent = 'Đang xóa...';
  try {
    const res = await fetch('/api/niches', {method: 'DELETE', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name})});
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Không thể xóa ngách');
    await refreshNiches(data.selected_niche || 'Chung');
    await saveSelectedNiche();
    document.getElementById('nicheSummary').textContent =
      `Đã xóa ngách ${name} và ${data.removed_files || 0} file liên quan.`;
  } catch (err) {
    document.getElementById('nicheSummary').textContent = err.message;
  } finally {
    deleteNicheBtn.textContent = 'Xóa ngách';
    updateDeleteNicheButton();
  }
});

exportLinksBtn.addEventListener('click', async () => {
  const niche = nicheInput.value;
  if (!niche) return;
  exportLinksBtn.disabled = true;
  exportLinksBtn.textContent = 'Đang xuất...';
  try {
    const res = await fetch('/api/export_links?niche=' + encodeURIComponent(niche));
    if (!res.ok) {
      const data = await res.json();
      throw new Error(data.error || 'Không thể xuất danh sách link');
    }
    const blob = await res.blob();
    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = objectUrl;
    link.download = 'danh_sach_link_' + niche + '.txt';
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(objectUrl);
    document.getElementById('nicheSummary').textContent = 'Đã xuất danh sách link của ngách ' + niche + '.';
  } catch (err) {
    document.getElementById('nicheSummary').textContent = err.message;
  } finally {
    exportLinksBtn.disabled = !preventDuplicatesInput.checked;
    exportLinksBtn.textContent = 'Xuất link .txt';
  }
});

let allJobs = {}; // job_id -> { url, el, ...status }
let pollIntervalHandle = null;

// Lấy thư mục hiện tại khi mở trang
fetch('/api/get_folder').then(r => r.json()).then(data => {
  folderPathText.textContent = data.folder;
}).catch(() => { folderPathText.textContent = '(chưa xác định)'; });

chooseFolderBtn.addEventListener('click', async () => {
  chooseFolderBtn.disabled = true;
  chooseFolderBtn.textContent = 'Đang mở...';
  try {
    const res = await fetch('/api/browse_folder', { method: 'POST' });
    const data = await res.json();
    if (data.folder) folderPathText.textContent = data.folder;
  } catch (e) {
    // bỏ qua
  } finally {
    chooseFolderBtn.disabled = false;
    chooseFolderBtn.textContent = 'Chọn thư mục';
  }
});

downloadBtn.addEventListener('click', startBatchDownload);
downloadAllBtn.addEventListener('click', downloadAllFinished);
retryBtn.addEventListener('click', retryFailedJobs);

async function startBatchDownload() {
  const raw = urlInput.value.trim();
  if (!raw) return;
  const prevent_duplicates = preventDuplicatesInput.checked;
  const niche = prevent_duplicates ? nicheInput.value.trim() : null;
  if (prevent_duplicates && !niche) { summaryText.textContent = 'Vui lòng chọn ngách.'; nicheInput.focus(); return; }

  downloadBtn.disabled = true;
  downloadBtn.textContent = 'Đang gửi...';

  try {
    const res = await fetch('/api/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: raw, niche, prevent_duplicates })
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Không thể bắt đầu tải');
    data.jobs.forEach(item => {
      const el = createJobElement(item.url, niche, item.group);
      jobsContainer.prepend(el);
      allJobs[item.job_id] = { url: item.url, group: item.group, el };
    });
    const groupCount = new Set(data.jobs.map(item => item.group).filter(Boolean)).size;
    if (groupCount) summaryText.textContent = 'Đã nhận ' + data.jobs.length + ' link trong ' + groupCount + ' thư mục con.';
    startPolling();
    urlInput.value = '';
  } catch (err) {
    summaryText.textContent = 'Lỗi: ' + err.message;
  } finally {
    downloadBtn.disabled = false;
    downloadBtn.textContent = 'Tải tất cả';
  }
}

function createJobElement(url, niche, group = null) {
  const div = document.createElement('div');
  div.className = 'job';
  div.innerHTML = `
    <div class="title"></div>
    <div class="progress-bar"><div class="progress-fill"></div></div>
    <div class="meta">
      <span class="status-text">Đang chờ trong hàng đợi...</span>
      <span class="percent-text">0%</span>
    </div>
  `;
  const location = [niche, group].filter(Boolean).join(' · ');
  div.querySelector('.title').textContent = (location ? '[' + location + '] ' : '') + url;
  return div;
}

function updateJobError(el, msg) {
  const statusText = el.querySelector('.status-text');
  statusText.textContent = 'Lỗi: ' + msg;
  statusText.classList.add('status-error');
}

function startPolling() {
  if (pollIntervalHandle) return; // đã có vòng polling chạy rồi
  pollIntervalHandle = setInterval(pollAllJobs, 1000);
}

async function pollAllJobs() {
  const ids = Object.keys(allJobs);
  if (ids.length === 0) return;

  try {
    const res = await fetch('/api/status_batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_ids: ids })
    });
    const statuses = await res.json();

    let doneCount = 0, errorCount = 0, activeCount = 0, duplicateCount = 0;

    ids.forEach(jobId => {
      const job = allJobs[jobId];
      const status = statuses[jobId];
      if (!status) return;

      const el = job.el;
      const fill = el.querySelector('.progress-fill');
      const statusText = el.querySelector('.status-text');
      const percentText = el.querySelector('.percent-text');

      job.status = status.status;
      const percent = status.percent || 0;
      fill.style.width = percent + '%';
      percentText.textContent = percent + '%';

      if (status.status === 'waiting') { statusText.textContent = 'Đang chờ trong hàng đợi...'; activeCount++; }
      else if (status.status === 'queued') { statusText.textContent = 'Bắt đầu xử lý...'; activeCount++; }
      else if (status.status === 'checking') { statusText.textContent = 'Đang kiểm tra trùng trên toàn bộ ngách...'; activeCount++; }
      else if (status.status === 'retrying') { statusText.textContent = 'HTTP 403 — đang lấy URL mới và thử lại...'; activeCount++; }
      else if (status.status === 'downloading') { statusText.textContent = 'Đang tải...'; activeCount++; }
      else if (status.status === 'processing') { statusText.textContent = 'Đang ghép file...'; activeCount++; }
      else if (status.status === 'done') {
        statusText.innerHTML = '<span class="status-done">Hoàn thành ✓</span>';
        if (!el.querySelector('.download-link')) {
          const link = document.createElement('a');
          link.className = 'download-link';
          link.href = '/files/' + encodeURIComponent(jobId);
          link.textContent = '⬇ Mở / Lưu video: ' + status.title;
          link.target = '_blank';
          el.appendChild(link);
        }
        doneCount++;
      } else if (status.status === 'duplicate') {
        statusText.textContent = status.message + ' ' + (status.title || '');
        statusText.style.color = '#f0bd60';
        percentText.textContent = 'Đã bỏ qua';
        duplicateCount++;
      } else if (status.status === 'error') {
        statusText.textContent = 'Lỗi: ' + (status.error || 'Không xác định');
        statusText.classList.add('status-error');
        errorCount++;
      }
    });

    summaryText.textContent = `Tổng: ${ids.length} | Hoàn thành: ${doneCount} | Trùng: ${duplicateCount} | Lỗi: ${errorCount} | Đang xử lý: ${activeCount}`;
    downloadAllBtn.style.display = doneCount > 0 ? 'inline-block' : 'none';
    retryBtn.style.display = errorCount > 0 ? 'inline-block' : 'none';

    if (activeCount === 0) {
      clearInterval(pollIntervalHandle);
      pollIntervalHandle = null;
      if (preventDuplicatesInput.checked) refreshNiches();
    }
  } catch (e) {
    // bỏ qua lỗi tạm thời của 1 lần poll, vòng sau thử lại
  }
}

async function retryFailedJobs() {
  const failedIds = Object.keys(allJobs).filter(jobId => allJobs[jobId].status === 'error');
  if (failedIds.length === 0) return;

  retryBtn.disabled = true;
  retryBtn.textContent = 'Đang thử lại...';
  try {
    const res = await fetch('/api/retry', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_ids: failedIds })
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Không thể retry');

    data.jobs.forEach(item => {
      const oldJob = allJobs[item.old_job_id];
      if (!oldJob) return;
      delete allJobs[item.old_job_id];
      oldJob.status = 'queued';
      oldJob.el.querySelector('.progress-fill').style.width = '0%';
      oldJob.el.querySelector('.percent-text').textContent = '0%';
      const statusText = oldJob.el.querySelector('.status-text');
      statusText.classList.remove('status-error');
      statusText.textContent = 'Đang chờ thử lại...';
      const oldLink = oldJob.el.querySelector('.download-link');
      if (oldLink) oldLink.remove();
      allJobs[item.job_id] = oldJob;
    });
    retryBtn.style.display = 'none';
    startPolling();
    pollAllJobs();
  } catch (err) {
    summaryText.textContent = 'Không thể retry: ' + err.message;
  } finally {
    retryBtn.disabled = false;
    retryBtn.textContent = '↻ Retry URL bị lỗi';
  }
}

function downloadAllFinished() {
  Object.values(allJobs).forEach(job => {
    const link = job.el.querySelector('.download-link');
    if (link) {
      const a = document.createElement('a');
      a.href = link.href;
      a.download = '';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    }
  });
}
</script>
</body>
</html>
"""

def open_browser():
    time.sleep(1.5)  # đợi server khởi động xong
    url = "http://127.0.0.1:5000"
    try:
        # Mở giao diện bằng Google Chrome theo lựa chọn của người dùng.
        chrome_paths = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            str(Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe"),
        ]
        chrome_exe = next((p for p in chrome_paths if os.path.exists(p)), None)
        if chrome_exe:
            subprocess.Popen([chrome_exe, url])
        else:
            webbrowser.open(url)
    except Exception:
        webbrowser.open(url)


if __name__ == "__main__":
    print("=" * 60)
    print(" Video Downloader đang chạy!")
    print(" Trình duyệt sẽ tự mở: http://127.0.0.1:5000")
    print(" (Đừng tắt cửa sổ này khi đang dùng chương trình)")
    print("=" * 60)
    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host="127.0.0.1", port=5000, debug=False)
