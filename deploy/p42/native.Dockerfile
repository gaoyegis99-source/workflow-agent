# syntax=docker/dockerfile:1.7
FROM ubuntu:24.04@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3
ARG V7_VERSION
LABEL org.opencontainers.image.version=$V7_VERSION
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    OGR_CT_FORCE_TRADITIONAL_GIS_ORDER=YES
RUN apt-get update && apt-get install -y --no-install-recommends \
       python3 python3-pip python3-gdal=3.8.4+dfsg-3ubuntu3 \
       gdal-bin=3.8.4+dfsg-3ubuntu3 libproj25=9.4.0-1build2 \
    && rm -rf /var/lib/apt/lists/*
COPY groundwater_operator_pack-*.whl /tmp/
RUN python3 -m pip install --break-system-packages --no-deps /tmp/groundwater_operator_pack-*.whl && rm /tmp/groundwater_operator_pack-*.whl \
    && groupadd --system platform && useradd --system --uid 10001 --gid platform platform \
    && mkdir -p /input /output && chown -R platform:platform /output
USER 10001:10001
WORKDIR /output
CMD ["platform-spatial", "execute"]
