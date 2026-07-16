FROM python:3.11-slim

WORKDIR /app

# Install deps first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY go_manga_monitor.py bot_realtime.py ./

# State files (monitor_state.json / bookmarks.json / bot_state.json) are written
# next to the code. Mount a persistent volume at /app for them to survive restarts.

CMD ["python", "-u", "bot_realtime.py"]
