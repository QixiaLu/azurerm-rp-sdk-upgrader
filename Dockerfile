# upgrader sandbox: runs the RP API-version upgrade loop against a mounted azurerm fork.
FROM golang:1.26-bookworm

# Let Go auto-fetch the exact toolchain pinned in the repo's go.mod (e.g. >= 1.26.4),
# so the build isn't blocked when the fork's go directive outpaces the base image.
ENV GOTOOLCHAIN=auto

# --- system deps: python, build tooling, gh CLI -------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv make git curl ca-certificates gnupg unzip \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# --- Terraform CLI -------------------------------------------------------------------
# Pre-install Terraform and point the acctest harness at it via TF_ACC_TERRAFORM_PATH.
# This avoids the framework's auto-download + GPG signature check, which fails when
# HashiCorp's release key has expired ("openpgp: key expired").
ARG TERRAFORM_VERSION=1.14.3
RUN curl -fsSL "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_$(dpkg --print-architecture).zip" -o /tmp/terraform.zip \
    && unzip -o /tmp/terraform.zip -d /usr/local/bin \
    && rm /tmp/terraform.zip \
    && chmod +x /usr/local/bin/terraform \
    && terraform version
ENV TF_ACC_TERRAFORM_PATH=/usr/local/bin/terraform

# --- install upgrader (and the Copilot SDK) -----------------------------------------
WORKDIR /opt/ai-api-upgrade
COPY pyproject.toml README.md ./
COPY upgrader ./upgrader
COPY .github ./.github
RUN python3 -m pip install --break-system-packages -e .

# The azurerm fork is mounted here at runtime; loop writes .upgrader/ under it.
WORKDIR /workspace/azurerm
ENTRYPOINT ["python3", "-m", "upgrader"]
