@echo off
cd /d "%~dp0"
rem pyw/pythonw run without a console window; the server stops when the page is closed.
where pyw >nul 2>nul && (start "" pyw server.py & exit /b)
start "" pythonw server.py
