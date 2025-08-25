# Optional: Container runner (Railway/Render/Fly)
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY job_watcher.py config.yaml ./
CMD ["bash", "-lc", "while true; do python job_watcher.py; sleep 600; done"]
