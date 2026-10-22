FROM groundwater-v7-operator:0.3.8
COPY --chown=65532:65532 tests/performance/long_operator.py /opt/v7-performance/long_operator.py
ENTRYPOINT ["python", "/opt/v7-performance/long_operator.py"]
