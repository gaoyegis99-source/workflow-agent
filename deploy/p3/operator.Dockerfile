FROM python@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0
ARG V7_RELEASE_SOURCE_DIGEST
ARG V7_VERSION
LABEL org.opencontainers.image.version="$V7_VERSION" \
      org.opencontainers.image.revision="$V7_RELEASE_SOURCE_DIGEST"
COPY wheels /wheels
RUN pip install --no-index --find-links=/wheels "groundwater-operator-pack==$V7_VERSION" && rm -rf /wheels
USER 65532:65532
ENV PYTHONDONTWRITEBYTECODE=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
ENTRYPOINT ["platform-operator"]
