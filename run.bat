@echo off
REM One-click launcher for the Sales & Credit Control web app (Windows).
cd /d "%~dp0"

if not exist ".venv\" (
    echo Creating virtual environment...
    py -3.12 -m venv .venv 2>nul || python -m venv .venv
    call .venv\Scripts\activate.bat
    echo Installing dependencies...
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt
) else (
    call .venv\Scripts\activate.bat
)

if not exist "data\sample_sales.csv" (
    echo Generating sample data...
    python scripts\make_sample_data.py
)

echo Starting web app... a browser tab will open.
streamlit run app.py
pause
