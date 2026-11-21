# Wealthfolio Companion as a container image (waldonso2/wealthfolio-importer-addon#148).
# Everything from requirements.txt with hashes and wheels only (#58), Scalable's sc only from its signed release
# (sc_install), a user without rights, no key in the image: it comes as the secret companion-key.
# Build: docker build -t companion .   Run: deploy/docker-compose.example.yml
# The base image by digest, written out in both stages: Dependabot (docker) proposes a new digest only for a FROM it
# can read, and a weekly security patch of Debian/Python arrives as such a pull request (#155).
FROM python:3.13-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS build
# 1: the build fails without Scalable's sc (the published image); 0: an image without Scalable (local tests).
ARG REQUIRE_SC=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /src
COPY requirements.txt pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m venv /opt/companion \
 && /opt/companion/bin/pip install --require-hashes --only-binary :all: -r requirements.txt \
 && /opt/companion/bin/pip install --no-deps --no-build-isolation .
RUN /opt/companion/bin/wealthfolio-companion install-sc --dir /opt/companion/bin || [ "$REQUIRE_SC" = 0 ]

FROM python:3.13-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY --from=build /opt/companion /opt/companion
RUN useradd --system --uid 10001 --user-group --home-dir /data --no-create-home --shell /usr/sbin/nologin companion \
 && mkdir -p /data /tls \
 && chown 10001:10001 /data /tls \
 && chmod 0700 /data /tls
ENV PATH=/opt/companion/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    COMPANION_CONTAINER=1 \
    COMPANION_DATA=/data \
    COMPANION_TLS_DIR=/tls \
    COMPANION_KEY_FILE=/run/secrets/companion-key \
    COMPANION_SC=/opt/companion/bin/sc
USER 10001:10001
VOLUME ["/data", "/tls"]
# 8443: the UI over HTTPS (own CA, /tls), 8090: redirect - or the UI itself with COMPANION_TLS=off behind a proxy.
EXPOSE 8443 8090
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s \
  CMD ["python", "-c", "import http.client as h,sys; c=h.HTTPConnection('127.0.0.1',8090,timeout=5); c.request('GET','/healthz'); sys.exit(c.getresponse().status>=500)"]
ENTRYPOINT ["wealthfolio-companion"]
CMD ["container"]
