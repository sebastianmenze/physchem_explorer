FROM python:3.11-slim

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir \
    streamlit \
    folium \
    streamlit-folium \
    plotly \
    duckdb \
    pandas \
    numpy \
    openpyxl \
    xarray \
    h5netcdf

# Copy app
COPY ctd_explorer.py .

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8501/_stcore/health || exit 1

CMD ["python", "-m", "streamlit", "run", "ctd_explorer.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--server.fileWatcherType=none", \
     "--server.maxUploadSize=500"]
