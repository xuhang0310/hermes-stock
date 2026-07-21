ARG PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.12-slim-bookworm
FROM ${PYTHON_IMAGE}

ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    LANG=C.UTF-8 \
    MPLCONFIGDIR=/tmp/matplotlib \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DEFAULT_TIMEOUT=30 \
    PIP_RETRIES=3

RUN sed -i 's|http://deb.debian.org|https://mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update -o Acquire::Retries=3 -o Acquire::https::Timeout=30 \
    && apt-get install -y --no-install-recommends fonts-noto-cjk fontconfig \
    && rm -rf /var/lib/apt/lists/*

COPY deploy/sandbox/requirements.txt /opt/hermes-sandbox/requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check -r /opt/hermes-sandbox/requirements.txt

COPY deploy/sandbox/runner.py /opt/hermes-sandbox/runner.py
RUN chmod 0555 /opt/hermes-sandbox/runner.py \
    && mkdir -p /workspace \
    && chmod 0777 /workspace

WORKDIR /workspace
CMD ["sleep", "infinity"]
