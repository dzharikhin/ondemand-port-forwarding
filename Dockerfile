FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        openssh-client sshpass gnupg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV DATA_DIR=/data
VOLUME /data

ENTRYPOINT ["ondemandportforward"]
CMD ["daemon"]
