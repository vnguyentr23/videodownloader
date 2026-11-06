# Video Downloader

Công cụ tải nhiều video bằng `yt-dlp`, chạy cục bộ trên Windows với giao diện web.

## Tính năng chính

- Tải đồng thời nhiều URL từ YouTube, TikTok, Facebook, Instagram, Reddit, Douyin và các nền tảng được `yt-dlp` hỗ trợ.
- Phân ngách và chống trùng footage theo URL, mã video và hash nội dung.
- Ngách `Chung` có phạm vi chống trùng riêng.
- Tự tạo thư mục con từ danh sách dạng `1. Tên nhóm`.
- Retry các URL tải lỗi.
- Xuất danh sách link đã tải thành file `.txt`, sắp xếp theo thứ tự nhóm.
- Ghi nhớ thư mục lưu và ngách đã chọn.

## Chạy từ mã nguồn

Yêu cầu Python 3.12+ và FFmpeg.

```powershell
python -m pip install -r requirements.txt
python app.py
```

Ứng dụng mở tại `http://127.0.0.1:5000` và tự mở bằng Google Chrome khi có thể.

## Đóng gói Windows

Đặt `ffmpeg.exe` vào `ffmpeg_bin/`, sau đó chạy:

```powershell
./build.bat
```

File kết quả nằm trong `dist/VideoDownloader.exe`.

## Cookie và dữ liệu riêng tư

Cookie xuất từ trình duyệt có thể đặt trong thư mục `cookies/` theo hướng dẫn tại [HUONG_DAN.md](HUONG_DAN.md). Thư mục cookie, video tải về, lịch sử SQLite, log và file build đều được loại khỏi Git.

## Kiểm thử

```powershell
python -m unittest -v test_duplicates.py
```

Xem hướng dẫn đầy đủ tại [HUONG_DAN.md](HUONG_DAN.md).

