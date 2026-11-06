@echo off
title Build Video Downloader EXE
cd /d "%~dp0"

echo ============================================
echo   BUOC 1: Cai cac thu vien can thiet
echo ============================================
pip install -r requirements.txt
pip install pyinstaller

echo.
echo ============================================
echo   BUOC 2: Chuan bi ffmpeg de nhung vao file exe
echo ============================================
if not exist "ffmpeg_bin\ffmpeg.exe" (
    echo.
    echo [CANH BAO] Khong tim thay ffmpeg_bin\ffmpeg.exe
    echo Vui long:
    echo   1. Tai ffmpeg ban "release essentials" tai: https://www.gyan.dev/ffmpeg/builds/
    echo   2. Giai nen, vao thu muc "bin", copy file ffmpeg.exe
    echo   3. Tao thu muc "ffmpeg_bin" canh file build.bat nay
    echo   4. Dan ffmpeg.exe vao thu muc ffmpeg_bin do
    echo   5. Chay lai build.bat
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   BUOC 3: Dong goi thanh file .exe duy nhat
echo ============================================
pyinstaller --noconfirm --onefile --console ^
    --name "VideoDownloader" ^
    --add-data "ffmpeg_bin;ffmpeg_bin" ^
    app.py

echo.
echo ============================================
echo   HOAN THANH!
echo ============================================
echo File exe nam tai: dist\VideoDownloader.exe
echo Hay copy file nay ra ngoai (vd: Desktop) de su dung.
echo Khong can Python nua tu day ve sau.
echo.
pause
