# Runs the Telegram bot (bot.py), not the interactive CLI (main.py) - a
# container has no terminal for main.py's input() prompts to read from.
FROM python:3.10-slim

# Python buffers stdout by default when it's not attached to a real terminal
# (true of every container) - without this, `docker logs` shows nothing until
# the buffer fills or the process exits, making the running bot look silent
# even when it's working fine.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first so Docker can cache this layer across rebuilds
# that only change main.py/bot.py, not requirements.txt.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py model_router.py bot.py google_helpers.py github_helpers.py linear_helpers.py deploy_helpers.py railway_helpers.py projects.py ./
COPY projects.json ./
COPY config/ ./config/
COPY files/ ./files/

# memory_db/ (long-term memory) and assistant.log are created here at
# runtime. A plain container's filesystem is wiped on every restart/redeploy -
# mount a persistent volume at /app/memory_db if you want long-term memory to
# actually survive, the same way it does locally. See README for details.

CMD ["python", "bot.py"]
