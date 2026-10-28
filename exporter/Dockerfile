FROM docker.io/library/python:3.12.7-alpine3.20

# stdlib only: no pip install, nothing to pin besides the base image
RUN adduser -D -u 10001 exporter
COPY --chown=exporter gateway_exporter.py /app/gateway_exporter.py
USER exporter
EXPOSE 9105
ENTRYPOINT ["python3", "-I", "/app/gateway_exporter.py"]
