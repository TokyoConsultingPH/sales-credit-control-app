# Container image for deploying the app to Google Cloud Run (or any container host).
FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (better layer caching).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code (data/, .venv, Sample file/, etc. are excluded via .dockerignore).
COPY . .

# Cloud Run provides $PORT (defaults to 8080). Streamlit must bind 0.0.0.0.
ENV PORT=8080
EXPOSE 8080
CMD streamlit run app.py \
    --server.port ${PORT} \
    --server.address 0.0.0.0 \
    --server.headless true \
    --server.enableCORS false \
    --server.enableXsrfProtection false
