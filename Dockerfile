# BUILD WITH:  docker build --platform linux/amd64 -t jalalhussein/geoecon-pipeline:<tag> .
#
# --platform is mandatory. The dev machine is Apple Silicon (arm64); the CERIT-SC
# nodes are amd64. A plain `docker build` produces an arm64-only manifest that
# pushes to Docker Hub without complaint and then fails at pull time with
# "no match for platform in manifest". deploy/cronjob.yaml and
# deploy/watchdog-cronjob.yaml are deliberately kept on the same tag, so that
# breaks the pipeline AND the watchdog together — no brief and no alert. This
# happened on 2026-07-30 and was caught only by an in-cluster pull check; a local
# `docker run` smoke test cannot catch it, because on an arm64 host it passes for
# the wrong reason. Verify with:
#   docker manifest inspect jalalhussein/geoecon-pipeline:<tag> | grep architecture
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
