ARG CANN_BASE_IMAGE=quay.io/ascend/cann:8.5.0-a3-ubuntu22.04-py3.10
FROM ${CANN_BASE_IMAGE}

# Local builds use the public mirrors below; CI overrides them with the
# cluster-internal cache services (see build-docker-image.yml build-args).
ARG APTMIRROR=http://mirrors.huaweicloud.com
ARG PIP_INDEX_URL=https://repo.huaweicloud.com/repository/pypi/simple
ARG PIP_TRUSTED_HOST=repo.huaweicloud.com
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu

# torch/torch-npu are pinned per CANN release: the CANN 8.5.0/9.0.0 lines
# (910b/a3) use torch 2.7.1 / torch-npu 2.7.1.post8, while CANN 9.1.0
# (a5/Ascend 950) uses torch 2.12.0 / torch-npu 2.12.0. requirements.txt is
# shared by every chip, so the pins live here — override at build time with
# --build-arg TORCH_VERSION=2.12.0 --build-arg TORCH_NPU_VERSION=2.12.0.
ARG TORCH_VERSION=2.7.1
ARG TORCH_NPU_VERSION=2.7.1.post8

SHELL ["/bin/bash", "-c"]
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ="Asia/shanghai"

RUN sed -i "s|http://archive.ubuntu.com|${APTMIRROR}|g" /etc/apt/sources.list 2>/dev/null || true && \
    sed -i "s|http://security.ubuntu.com|${APTMIRROR}|g" /etc/apt/sources.list 2>/dev/null || true && \
    echo 'Acquire::Retries "3";' > /etc/apt/apt.conf.d/80-retries && \
    echo 'Acquire::http::Timeout "30";' >> /etc/apt/apt.conf.d/80-retries && \
    apt update && \
    apt install --yes --no-install-recommends --no-install-suggests \
    bash \
    ca-certificates \
    clang-15 \
    clang-format \
    cmake \
    ccache \
    curl \
    git \
    gnupg \
    lld-15 \
    make \
    ninja-build \
    sudo \
    unzip \
    vim \
    wget \
    libzstd-dev \
    zlib1g-dev && \
    apt clean && rm -rf /var/lib/apt/lists/* && \
    update-alternatives --install /usr/bin/clang clang /usr/bin/clang-15 100 && \
    update-alternatives --install /usr/bin/clang++ clang++ /usr/bin/clang++-15 100 && \
    update-alternatives --install /usr/bin/lld lld /usr/bin/lld-15 100 && \
    update-ca-certificates

RUN mkdir -p /root/.config/pip && \
    echo -e "[global]\nindex-url = ${PIP_INDEX_URL}\ntrusted-host = ${PIP_TRUSTED_HOST}\nextra-index-url = ${TORCH_INDEX_URL}" > /root/.config/pip/pip.conf

COPY requirements.txt requirements_dev.txt ./

# torch==<ver>+cpu is a transitive dep of torch-npu but only available from
# the PyTorch CPU index, not the standard PyPI mirror.  Install it first so
# the subsequent pip install doesn't need to scan the extra index.
RUN --mount=type=cache,target=/root/.cache/pip \
    python3 -m pip install --timeout 600 \
      --find-links ${TORCH_INDEX_URL}/torch/ \
      torch==${TORCH_VERSION}+cpu

RUN --mount=type=cache,target=/root/.cache/pip \
    python3 -m pip install --timeout 300 -r requirements_dev.txt && \
    python3 -m pip install --timeout 300 -r requirements.txt && \
    python3 -m pip install --timeout 300 torch-npu==${TORCH_NPU_VERSION}

WORKDIR /home/triton
