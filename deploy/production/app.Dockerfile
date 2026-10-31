FROM docker@sha256:9190b0613792e658a7783cf14b2d5ace5941bb68ede7276922ea36ee457d76ad AS docker_cli
FROM python@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0

ARG V7_VERSION
ARG V7_RELEASE_SOURCE_DIGEST
LABEL org.opencontainers.image.version="$V7_VERSION" \
      org.opencontainers.image.revision="$V7_RELEASE_SOURCE_DIGEST" \
      org.opencontainers.image.title="Groundwater Platform V7 unified runtime"

COPY --from=docker_cli /usr/local/bin/docker /usr/local/bin/docker
COPY wheels /wheels
COPY dependencies /dependencies
COPY studio /opt/v7-studio
RUN pip install --no-index --find-links=/wheels --find-links=/dependencies \
      "groundwater-agent-runtime==$V7_VERSION" \
      "groundwater-checker-pack==$V7_VERSION" \
      "groundwater-data-service==$V7_VERSION" \
      "groundwater-execution-service==$V7_VERSION" \
      "groundwater-knowledge-service==$V7_VERSION" \
      "groundwater-operator-pack==$V7_VERSION" \
      "groundwater-platform-api==$V7_VERSION" \
      "groundwater-platform-contracts==$V7_VERSION" \
      "groundwater-platform-core==$V7_VERSION" \
      "groundwater-platform-infrastructure==$V7_VERSION" \
      "groundwater-tabular-operator-pack==$V7_VERSION" \
      "groundwater-temporal-workflow-runtime==$V7_VERSION" \
    && test -f /opt/v7-studio/index.html \
    && test -d /opt/v7-studio/assets \
    && test -n "$(find /opt/v7-studio/assets -type f -print -quit)" \
    && ! find /opt/v7-studio -type l -o -name '*.map' | grep -q . \
    && rm -rf /wheels /dependencies

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp \
    V7_STUDIO_ROOT=/opt/v7-studio
USER 65532:65532
CMD ["platform-api", "--host", "0.0.0.0", "--port", "8007"]
