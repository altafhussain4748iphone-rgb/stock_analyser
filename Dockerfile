FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt pyproject.toml ./
COPY technical_analysis ./technical_analysis

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir .

# Cloud Run Jobs pass the workspace ("crypto", "stocks", ...) as the container
# args, set per-job in Terraform. This default lets `docker run <image>` work
# out of the box for local testing.
ENTRYPOINT ["python", "-m", "technical_analysis.cli"]
CMD ["crypto"]
