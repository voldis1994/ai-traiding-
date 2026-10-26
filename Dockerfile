FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NTA_PRODUCTION=1 \
    NTA_BIND_HOST=0.0.0.0 \
    NEURAL_AI_DATA_DIR=/data \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    TORCH_NUM_THREADS=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# Includes CPU torch via --extra-index-url in requirements.txt (fixes Render ModuleNotFoundError: torch).
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY launcher.py ./launcher.py
COPY PASMACISANAS_MASINA_LV.md README.md ./

VOLUME ["/data"]
EXPOSE 8765

# PORT is injected by Render/Railway/Fly; default for plain Docker.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8765}"]
