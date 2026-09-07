FROM python:3.12-slim-trixie@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN addgroup --system app && adduser --system --ingroup app app
COPY pyproject.toml requirements.lock README.md ./
COPY src ./src
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps .
COPY alembic.ini ./
COPY database ./database
COPY scripts ./scripts
RUN mkdir -p /app/storage && chown -R app:app /app
USER app
EXPOSE 8000
CMD ["uvicorn", "linkedin_automation.main:app", "--host", "0.0.0.0", "--port", "8000"]
