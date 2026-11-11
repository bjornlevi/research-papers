FROM docker.io/library/python:3.12-slim-bookworm@sha256:2ed6491b93cd49272ee6de2b5a38440c3448360322c089fc23e370722d74179d AS build
WORKDIR /build
RUN python -m pip install --no-cache-dir setuptools==82.0.1
COPY pyproject.toml README.md ./
COPY src/ src/
COPY tools/ tools/
RUN python -m pip wheel --no-cache-dir --no-deps --no-build-isolation --wheel-dir /wheels .

FROM docker.io/library/python:3.12-slim-bookworm@sha256:2ed6491b93cd49272ee6de2b5a38440c3448360322c089fc23e370722d74179d
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 RESEARCH_LIBRARY_HTTP_PORT=8000
COPY deploy/runtime-requirements.txt /opt/runtime-requirements.txt
RUN python -m pip install --no-cache-dir -r /opt/runtime-requirements.txt
COPY --from=build /wheels/ /wheels/
RUN python -m pip install --no-cache-dir --no-deps /wheels/*.whl && rm /wheels/*.whl \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin evidence
USER 10001:10001
WORKDIR /home/evidence
EXPOSE 8000
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s \
    CMD ["python", "-c", "import os; from urllib.request import Request,urlopen; urlopen(Request('http://127.0.0.1:'+os.environ['RESEARCH_LIBRARY_HTTP_PORT']+'/readyz', headers={'Host': os.environ['RESEARCH_LIBRARY_HEALTH_HOST']}), timeout=8).close()"]
ENTRYPOINT ["research-http"]
