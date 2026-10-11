FROM python:3.12-slim
# git for repositories; Java + structurizr-cli for DOCSYNC_C4_MODE=structurizr (push of the workspace to a Structurizr server)
ARG STRUCTURIZR_CLI_VERSION=2025.11.09
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates curl unzip default-jre-headless \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /opt/structurizr-cli \
    && curl -fsSL -o /tmp/cli.zip "https://github.com/structurizr/cli/releases/download/v${STRUCTURIZR_CLI_VERSION}/structurizr-cli.zip" \
    && unzip -q /tmp/cli.zip -d /opt/structurizr-cli && rm /tmp/cli.zip && chmod +x /opt/structurizr-cli/structurizr.sh
WORKDIR /app
COPY pyproject.toml README.md ./
COPY docsync ./docsync
COPY architecture-model ./architecture-model
RUN pip install --no-cache-dir -e ".[llm]"
ENV DOCSYNC_DATA_DIR=/data STRUCTURIZR_CLI=/opt/structurizr-cli/structurizr.sh
EXPOSE 8088
CMD ["docsync", "serve", "--port", "8088"]
