FROM node@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS node
FROM python@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY wheels /wheels
COPY requirements.txt /requirements.txt
RUN pip install --no-index --find-links=/wheels --require-hashes -r /requirements.txt && rm -rf /wheels
COPY engine.py bridge.py engine_profile.py /opt/v7-cwl/
ENV PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
USER 65532:65532
ENTRYPOINT ["python", "/opt/v7-cwl/engine.py"]
