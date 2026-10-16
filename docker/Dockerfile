FROM python:3.11-slim

#镜像只准备编码工具；不复制 Agent 代码、config.json 或目标仓库。
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash git ripgrep \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pytest ruff

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
CMD ["/bin/bash"]
