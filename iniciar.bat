@echo off
rem Inicia o Bot de Garimpo: cria o venv (se nao existir), instala as dependencias e roda o bot.
rem De dois cliques neste arquivo.

set BOT_FILE=bot.py
set VENV_DIR=venv

cd /d "%~dp0"

rem Prefere o launcher "py" (mais confiavel no Windows); se nao existir, tenta "python".
set PY=
py -3 --version >nul 2>nul && set PY=py -3
if not defined PY (
    python --version >nul 2>nul && set PY=python
)
if not defined PY (
    echo [-] Python 3 nao encontrado.
    echo     Instale em https://www.python.org/downloads/ e marque "Add python.exe to PATH".
    pause
    exit /b 1
)

if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo [+] Criando ambiente virtual em .\%VENV_DIR% ...
    %PY% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [-] Falha ao criar o venv.
        pause
        exit /b 1
    )
)

call "%VENV_DIR%\Scripts\activate.bat"

echo [+] Verificando dependencias...
python -m pip install --upgrade pip -q
pip install -r requirements.txt -q
if errorlevel 1 (
    echo [-] Falha ao instalar as dependencias.
    pause
    exit /b 1
)

echo [+] Iniciando o bot...
python "%BOT_FILE%"

echo.
pause