# syntax=docker/dockerfile:1.7
FROM ubuntu:24.04@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3
ARG V7_VERSION
LABEL org.opencontainers.image.version=$V7_VERSION
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    QT_QPA_PLATFORM=offscreen QGIS_PREFIX_PATH=/usr \
    PYTHONPATH=/usr/share/qgis/python:/usr/share/qgis/python/plugins \
    XDG_RUNTIME_DIR=/tmp/runtime-platform HOME=/tmp/home \
    QGIS_CUSTOM_CONFIG_PATH=/tmp/qgis-profile OGR_CT_FORCE_TRADITIONAL_GIS_ORDER=YES
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg python3-pip \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://download.qgis.org/downloads/qgis-archive-keyring.gpg -o /etc/apt/keyrings/qgis-archive-keyring.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/qgis-archive-keyring.gpg] https://qgis.org/ubuntu-ltr noble main" > /etc/apt/sources.list.d/qgis.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       python3-qgis=1:3.44.15+40noble qgis-providers=1:3.44.15+40noble \
       gdal-bin=3.8.4+dfsg-3ubuntu3 libproj25=9.4.0-1build2 \
       fonts-noto-cjk=1:20230817+repack1-3 \
    && rm -rf /var/lib/apt/lists/*
COPY groundwater_operator_pack-*.whl /tmp/
RUN python3 -m pip install --break-system-packages --no-deps /tmp/groundwater_operator_pack-*.whl && rm /tmp/groundwater_operator_pack-*.whl \
    && groupadd --system platform && useradd --system --uid 10001 --gid platform platform \
    && mkdir -p /input /output /tmp/runtime-platform /tmp/home /tmp/qgis-profile \
    && chown -R platform:platform /output /tmp/runtime-platform /tmp/home /tmp/qgis-profile \
    && chmod 0700 /tmp/runtime-platform /tmp/home /tmp/qgis-profile
USER 10001:10001
WORKDIR /output
CMD ["platform-spatial", "execute"]
