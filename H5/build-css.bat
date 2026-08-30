@echo off
rem =====================================================
rem  One-click compile SCSS -> style/css/main.css
rem  Double-click to run, or: build-css.bat
rem =====================================================
setlocal
cd /d "%~dp0"

echo Compiling style/scss/main.scss ...

if exist "node_modules\.bin\sass.cmd" goto use_local
echo [WARN] local sass not found, trying npx (network may be required)...
call npx.cmd sass style/scss/main.scss style/css/main.css --style=expanded --no-source-map
goto done

:use_local
call "node_modules\.bin\sass.cmd" style/scss/main.scss style/css/main.css --style=expanded --no-source-map

:done
if errorlevel 1 (
    echo.
    echo [ERROR] compile failed. Check the sass errors above.
    pause
    exit /b 1
)

echo.
echo [OK] style/css/main.css updated.
pause
