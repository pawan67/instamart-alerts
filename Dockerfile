FROM python:3.13-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_BROWSERS_PATH=/playwright

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY . .
RUN uv sync --frozen

RUN uv run playwright install --with-deps chromium

EXPOSE 8080

CMD ["uv", "run", "im", "serve", "--host", "0.0.0.0", "--port", "8080"]
