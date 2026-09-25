# Minimal updater sidecar: docker CLI + compose plugin + python3 stdlib server.
FROM docker:27-cli

RUN apk add --no-cache python3

COPY server.py /app/server.py
WORKDIR /app

EXPOSE 9000
CMD ["python3", "server.py"]
