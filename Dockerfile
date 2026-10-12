FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml ./
COPY autopilot ./autopilot
RUN pip install --no-cache-dir .

CMD ["python", "-m", "autopilot", "loop", "30"]
