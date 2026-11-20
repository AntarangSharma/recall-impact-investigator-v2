FROM ghcr.io/astral-sh/uv:0.11.9 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy RECALL_DATA_DIR=/runtime
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project
COPY recall recall
COPY data data
RUN mkdir /runtime && chown 65532:65532 /runtime
USER 65532:65532
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s CMD ["/app/.venv/bin/python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/health', timeout=2).close()"]
CMD ["/app/.venv/bin/uvicorn", "recall.app:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8000"]
