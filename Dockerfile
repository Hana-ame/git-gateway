# syntax=docker/dockerfile:1
FROM node:22-bookworm-slim

ENV NODE_ENV=production

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl gpg \
    && mkdir -p -m 755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && apt-get purge -y --auto-remove gpg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --chown=node:node package.json index.js repos.txt ./
COPY --chown=node:node src ./src

RUN mkdir -p /var/lib/git-gateway/content /home/node/.config/gh \
    && chown -R node:node /app /var/lib/git-gateway /home/node

USER node
EXPOSE 8000
ENV GATEWAY_HOST=0.0.0.0 \
    GATEWAY_PORT=8000 \
    CONTENT_DIR=/var/lib/git-gateway/content \
    REPO_RULES_FILE=/app/repos.txt \
    GH_CONFIG_DIR=/home/node/.config/gh

CMD ["node", "index.js"]
