#--- Builder Image ---
    FROM python:3.12-bookworm AS builder
    WORKDIR /app
    COPY . /app
    
    # Install system dependencies
    RUN apt-get update && apt-get install -y --no-install-recommends \
        libfontconfig1 \
        chromium \
        && apt-get clean && rm -rf /var/lib/apt/lists/*
    
    # Install Python dependencies in a virtual environment
    RUN python -m venv .venv && \
        . .venv/bin/activate && \
        pip install --no-cache-dir -r requirements.txt && \
        pip install playwright==1.50.0
    
    # Install Playwright
    RUN . .venv/bin/activate && playwright install --with-deps
    
    #--- Final Image ---
    FROM python:3.12-slim-bookworm
    WORKDIR /app
    COPY --from=builder /app/scheduler.py /app/scheduler.py
    COPY --from=builder /app/config.py /app/config.py
    COPY --from=builder /app/main.py /app/main.py
    COPY --from=builder /app/.venv ./.venv
    CMD [".venv/bin/python", "scheduler.py"]