FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md clawctl.py /app/
RUN pip install --no-cache-dir .

ENTRYPOINT ["clawctl"]
