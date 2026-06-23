FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN useradd -u 1000 -m app
WORKDIR /app
COPY pyproject.toml ./
COPY geoecon ./geoecon
# The source registry is data, not code — it lives outside the package, so pip
# install does not bundle it. Ship it explicitly; SOURCES_PATH points the runtime
# at it (see deploy/cronjob.yaml).
COPY config ./config
RUN pip install --no-cache-dir . && mkdir -p /data/archive && chown -R app /data
USER 1000
ENTRYPOINT ["python", "-m", "geoecon.orchestrator"]
CMD ["run"]
