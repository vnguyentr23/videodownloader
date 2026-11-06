# Hướng dẫn cài đặt & sử dụng (Windows)

Chương trình hỗ trợ **dán hàng loạt link** (mỗi link 1 dòng) và tự động tải về tất cả, chất lượng cao nhất, từ YouTube / TikTok / Facebook / Instagram / Reddit / Douyin.

Có 2 cách sử dụng:

- **Cách A**: Build 1 lần ra file `VideoDownloader.exe` — sau đó dùng mãi, không cần Python.
- **Cách B**: Chạy trực tiếp bằng Python mỗi lần (đơn giản hơn để build, nhưng vẫn cần Python cài sẵn).

> ⚠️ **Nếu bạn đã build file `.exe` từ phiên bản trước**: cần build lại bằng `build.bat` để file exe có các cải tiến mới (tự đọc cookie từ Chrome, thông báo lỗi sạch hơn). File exe cũ vẫn dùng được nhưng sẽ thiếu các tính năng này.

---

## CÁCH A — Build ra file .exe dùng vĩnh viễn (khuyên dùng)

### A1. Cài Python (chỉ cần 1 lần, để build)

1. Vào https://www.python.org/downloads/ , tải bản mới nhất cho Windows.
2. Khi cài, **nhớ tick ô "Add python.exe to PATH"** trước khi bấm Install Now.
3. Mở Command Prompt, kiểm tra: `python --version`

### A2. Chuẩn bị FFmpeg để nhúng vào file exe

1. Vào https://www.gyan.dev/ffmpeg/builds/ , tải bản **"release essentials"** (.zip).
2. Giải nén ra, vào thư mục `bin`, tìm file **`ffmpeg.exe`**.
3. Copy file `ffmpeg.exe` đó, dán vào thư mục **`ffmpeg_bin`** (đã có sẵn trong gói này, nằm cạnh file `build.bat`).

### A3. Build file exe

1. Mở Command Prompt, di chuyển vào thư mục `video-downloader`:
   ```
   cd Desktop\video-downloader
   ```
2. Bấm đúp file **`build.bat`** (hoặc gõ `build.bat` trong Command Prompt rồi Enter).
3. Đợi vài phút để quá trình build hoàn tất (lần đầu sẽ tải PyInstaller và đóng gói).
4. Khi xong, file kết quả nằm tại: `dist\VideoDownloader.exe`

### A4. Sử dụng

1. Copy file `VideoDownloader.exe` ra nơi bạn muốn (ví dụ Desktop).
2. Từ giờ, **chỉ cần bấm đúp vào `VideoDownloader.exe`** — chương trình tự khởi động và tự mở Chrome vào giao diện tải video. Không cần Python, không cần mở Command Prompt nữa.
3. Video tải về sẽ lưu trong thư mục `downloads` nằm cạnh file `VideoDownloader.exe`.

💡 Mẹo: tạo shortcut ra màn hình Desktop cho file `.exe` này (chuột phải → Send to → Desktop) để mở nhanh hơn.

---

## CÁCH B — Chạy trực tiếp bằng Python (không build exe)

### B1. Cài Python + FFmpeg

- Cài Python như mục A1.
- Cài FFmpeg và thêm vào PATH hệ thống:
  1. Tải FFmpeg tại https://www.gyan.dev/ffmpeg/builds/ (bản release essentials), giải nén vào ví dụ `C:\ffmpeg`.
  2. Thêm `C:\ffmpeg\bin` vào PATH: gõ "Environment Variables" vào ô tìm kiếm Windows → "Edit the system environment variables" → "Environment Variables..." → mục "System variables" → chọn `Path` → "Edit" → "New" → dán `C:\ffmpeg\bin` → OK hết.
  3. Mở Command Prompt mới, kiểm tra: `ffmpeg -version`

### B2. Cài thư viện Python

```
cd Desktop\video-downloader
pip install -r requirements.txt
```

### B3. Chạy chương trình

Bấm đúp file **`start.bat`** — chương trình tự khởi động và tự mở Chrome.

(Hoặc chạy thủ công: mở Command Prompt, vào thư mục project, gõ `python app.py`)

---

## Cách sử dụng giao diện (áp dụng cho cả 2 cách)

### Chống trùng footage trên toàn bộ ngách

- Công tắc **Chống trùng footage toàn bộ ngách** mặc định bật mỗi khi mở/tải lại trang.
- Bấm **+ Tạo ngách**, nhập tên rồi bấm **Tạo**. Ngách được lưu ngay và tự chọn, kể cả khi chưa có video. Chọn ngách cũ từ danh sách bên cạnh.
- Ngách đang chọn và thư mục lưu video được ghi nhớ khi tải lại trang hoặc mở lại chương trình.
- Bấm **Xóa ngách** để xóa ngách đang chọn. Sau khi xác nhận, chương trình xóa toàn bộ video, thư mục và lịch sử liên quan. Ngách **Chung** không thể xóa; ngách đang có video tải dở phải chờ hoàn tất trước khi xóa.
- Bấm **Xuất link .txt** để tải danh sách URL đã tải thành công trong ngách đang chọn. File được gom theo tên nhóm và sắp xếp tự nhiên theo thứ tự thư mục `1`, `2`, ... `10`; link không có nhóm nằm cuối. Lịch sử từ các phiên bản cũ không có URL gốc nên không thể xuất ngược lại.
- Ngách **Chung** có phạm vi chống trùng riêng: video trong Chung không bị so sánh với các ngách khác và ngược lại. Các ngách có tên khác vẫn kiểm tra trùng chéo với nhau.
- Khi tắt công tắc, phần chọn/tạo ngách được ẩn. Video tải thẳng vào thư mục lưu đã chọn, không tạo thư mục ngách, không đọc/ghi lịch sử chống trùng và không gán video vào ngách nào. Lịch sử cũ vẫn giữ nguyên. File trùng tên có số phía sau để không ghi đè.
- Công tắc áp dụng cho lượt gửi tiếp theo; các lượt đang chạy/chờ và Retry giữ trạng thái lúc gửi ban đầu.
- Một video đã tải trong một ngách có tên sẽ được báo rõ ngách đang chứa footage và bị bỏ qua ở các ngách có tên khác. Ngách **Chung** vẫn là phạm vi riêng như mô tả ở trên.
- Tên ngách không phân biệt chữ hoa/thường và khoảng trắng thừa. Mỗi ngách có thư mục con riêng trong thư mục lưu đã chọn.
- Lịch sử nằm trong `download_history.sqlite3` cạnh chương trình và được giữ sau khi tắt/mở lại, kể cả khi đổi thư mục lưu. Khi di chuyển chương trình, mang theo file lịch sử này.
- Nếu hai mã video khác nhau tải ra file giống hệt nhau, ứng dụng cũng bỏ bản trùng trong cùng phạm vi chống trùng. Không nhận diện bằng hình ảnh các bản đăng lại đã chỉnh sửa/nén khác nhau.
- Chỉ những lần tải thành công từ phiên bản này mới được ghi nhận. Video cũ không có mã/link và thông tin ngách nên không tự nhập vào lịch sử. Xóa file video không xóa dấu đã tải trong lịch sử.
- Các lượt đang chờ và nút Retry giữ nguyên ngách, thư mục của lần gửi ban đầu.


1. Dán nhiều link vào ô nhập, **mỗi link 1 dòng**, ví dụ:
   ```
   https://www.youtube.com/watch?v=abc123
   https://www.tiktok.com/@user/video/12345
   https://www.instagram.com/reel/xyz
   ```
   Nếu muốn tự tạo thư mục con, thêm dòng đánh số và tên thư mục trước các link:

       1. Đeo bám
       https://www.facebook.com/reel/...
       https://www.instagram.com/reel/...
       2. Âu yếm
       https://www.facebook.com/reel/...

   Ứng dụng sẽ giữ cả số thứ tự và tự tạo các thư mục **1. Đeo bám**, **2. Âu yếm** trực tiếp trong thư mục cha đã chọn, sau đó lưu đúng video vào từng thư mục. Danh sách không có dòng nhóm vẫn được lưu theo cách thông thường.
2. Bấm **"Tải tất cả"**.
3. Mỗi link sẽ hiện 1 thanh tiến trình riêng. Tối đa 3 link được tải đồng thời, các link còn lại tự xếp hàng chờ (tránh nghẽn mạng).
4. Khi video xong, sẽ có link **"⬇ Mở / Lưu video"** ngay dưới thanh tiến trình của link đó.
5. Có thể bấm **"Tải về toàn bộ video đã xong"** để mở/lưu tất cả video đã hoàn thành cùng lúc.
6. Tất cả video cũng được lưu sẵn trong thư mục `downloads`.

---

## Lưu ý về Facebook / Instagram / TikTok (video riêng tư hoặc bị chặn)

Ứng dụng luôn thử tải video công khai mà không dùng cookie trước. Ứng dụng **không đọc trực tiếp cơ sở dữ liệu cookie của Chrome**, vì Windows khóa file này khi Chrome đang chạy và có thể gây lỗi `Could not copy Chrome cookie database`. Bạn có thể để Chrome mở bình thường khi tải.

### Với video cần đăng nhập: dùng file cookie đã xuất

1. Cài extension **"Get cookies.txt LOCALLY"** hoặc **"J2Team Cookies"** trên Chrome.
2. Đăng nhập vào trang cần tải (Facebook/Instagram/TikTok).
3. Dùng extension xuất cookie ra file, chọn định dạng **"Netscape"** (không chọn JSON).
4. Đặt file vào thư mục `cookies/`, đặt tên:
   - Facebook → `cookies/facebook.txt`
   - Instagram → `cookies/instagram.txt`
   - TikTok → `cookies/tiktok.txt`
5. Chương trình sẽ thử chế độ công khai trước rồi tự dùng file cookie này nếu cần.

---

## Xử lý lỗi thường gặp

- **"python không được nhận diện là lệnh"**: chưa tick "Add python.exe to PATH" khi cài Python → cài lại và tick ô đó.
- **Build exe báo thiếu ffmpeg_bin\ffmpeg.exe**: làm lại bước A2.
- **1 link cụ thể không tải được**: nền tảng đó có thể vừa thay đổi cách bảo vệ video. Cập nhật yt-dlp:
  ```
  pip install -U yt-dlp
  ```
  rồi build lại exe (Cách A) hoặc chạy lại (Cách B).
- **Windows Defender/SmartScreen cảnh báo file exe lạ**: vì file chưa có chứng chỉ số (bình thường với các exe tự build) — chọn "More info" → "Run anyway" nếu bạn tin tưởng file do chính mình build ra.

---

## Lưu ý sử dụng có trách nhiệm

Công cụ này chỉ nên dùng để tải video bạn có quyền tải — video công khai cho phép tải, video của chính bạn, hoặc theo đúng điều khoản sử dụng (Terms of Service) của từng nền tảng. Việc tải và phát hành lại nội dung có bản quyền của người khác mà không được phép có thể vi phạm pháp luật về sở hữu trí tuệ tại nhiều quốc gia.
