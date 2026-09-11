# One image, one container per tenant. Mount the tenant directory at /tenant
# and, for metricflow_local, the dbt project wherever tenant.yml points.
FROM python:3.13-slim

ENV UV_SYSTEM_PYTHON=1 PYTHONUNBUFFERED=1
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra metricflow --extra bigquery

ENV PATH="/app/.venv/bin:$PATH"
ENV UNDERSTORY_TENANT=/tenant
ENV UNDERSTORY_LOG_DIR=/var/understory-log
EXPOSE 8000

CMD ["understory", "serve", "--tenant", "/tenant", "--host", "0.0.0.0", "--port", "8000"]
