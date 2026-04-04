FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md openclaw_k.py /app/
RUN pip install --no-cache-dir .

ENTRYPOINT ["openclaw-k"]
