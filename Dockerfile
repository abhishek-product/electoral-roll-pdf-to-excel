FROM python:3.12-slim

# tesseract = the OCR engine; libglib/libgl are opencv's runtime deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-eng \
        libglib2.0-0 libgl1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# One tesseract thread per worker: parallelism comes from the thread pool, and
# letting each tesseract fan out as well oversubscribes the CPU badly.
ENV OMP_THREAD_LIMIT=1 \
    PYTHONUNBUFFERED=1 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    PORT=8501

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://localhost:%s/_stcore/health' % os.environ.get('PORT','8501'))"

# shell form so $PORT expands -- Render, Fly and Cloud Run inject it,
# Hugging Face Spaces expects 7860
CMD streamlit run streamlit_app.py --server.port=${PORT} --server.address=0.0.0.0
