FROM ubuntu:22.04
LABEL maintainer="life-compute" \
      description="LIFE Compute Validator — decentralized cancer drug discovery"

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.10 python3.10-dev python3-pip \
        curl nodejs npm \
        libssl-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir \
    boltz==2.2.1 \
    rdkit-pypi \
    pyyaml \
    anchorpy==0.20.1 \
    solders==0.21.0 \
    solana==0.34.0 \
    base58==2.1.1 \
    requests==2.32.0

WORKDIR /app
COPY validator_daemon.py life_validate.js ./
COPY stats.json.template stats.json

RUN useradd -m validator
USER validator

CMD ["python3", "validator_daemon.py"]
