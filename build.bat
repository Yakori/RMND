@echo off
cd /d "%~dp0"
python -m pip install -q pyinstaller fastapi "uvicorn[standard]" "httpx[brotli]" brotli pydantic python-multipart
python -m PyInstaller --noconfirm --onefile --windowed --name FaceitPanel --add-data "index.html;." --add-data "config.example.json;." --collect-submodules uvicorn --hidden-import brotli --hidden-import multipart app.py
echo.
echo Готово: dist\FaceitPanel.exe (один файл, больше ничего не нужно)
pause
