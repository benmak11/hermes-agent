# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Playwright base image (Ubuntu noble, Python 3.12) ships Chromium + all the
# system libraries the browser needs — required for the Phase 7 Application agent
# to drive Greenhouse forms headlessly on Cloud Run. Tag must match the installed
# playwright pip version (1.60.0). Browsers live at /ms-playwright.
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

RUN pip install --no-cache-dir uv==0.8.13

# Let Playwright find the browsers preinstalled in the base image (this env var
# must persist to runtime, not just build).
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /code

COPY ./pyproject.toml ./README.md ./uv.lock* ./

# All packages listed in [tool.hatch.build.targets.wheel] must be present so the
# project wheel builds, and so the API routers can import them at runtime
# (api.routes.companies -> tools.companies, agents -> models, etc.).
COPY ./agents ./agents
COPY ./api ./api
COPY ./models ./models
COPY ./tools ./tools
COPY ./cli ./cli
COPY ./obs ./obs

# Company lists read by the companies endpoint (data/companies/*.yaml). The PII
# profile (data/profile.yaml) is gitignored and intentionally not shipped.
COPY ./data/companies ./data/companies

RUN uv sync --frozen

ARG COMMIT_SHA=""
ENV COMMIT_SHA=${COMMIT_SHA}

ARG AGENT_VERSION=0.0.0
ENV AGENT_VERSION=${AGENT_VERSION}

EXPOSE 8080

CMD ["uv", "run", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080"]