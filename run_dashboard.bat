@echo off
echo ==========================================
echo  AI Sales Follow-Up Agent — Dashboard
echo ==========================================
echo.
echo Starting Streamlit dashboard...
echo Dashboard will open at: http://localhost:8501
echo.
py -3.12 -m streamlit run ui\streamlit\app.py --server.port 8501
pause
