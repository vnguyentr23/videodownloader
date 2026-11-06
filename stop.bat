@echo off
title Dong Video Downloader
echo Dang dong server...
taskkill /f /fi "WINDOWTITLE eq VideoDownloaderServer*" >nul 2>&1
taskkill /f /im python.exe /fi "WINDOWTITLE eq VideoDownloaderServer*" >nul 2>&1
echo Da dong. Ban co the dong cua so nay.
pause
