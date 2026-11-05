#==============================================================================
FROM python:3.14-slim AS builder

WORKDIR /pygeoapi

RUN pip install uv

# Set to true to include debugpy, for the PYDEBUG mode of docker-entrypoint.sh
ARG INSTALL_DEBUGPY=false

# Install the dependencies first, so that this layer is cached when only the
# sources change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project \
    $([ "$INSTALL_DEBUGPY" = "true" ] && echo "--group debug")

COPY swissgeo_oaf ./swissgeo_oaf

# Overwrite the version.py from source with the actual version
ARG VERSION=unknown
RUN echo "__version__ = '$VERSION'" > swissgeo_oaf/version.py

# Install the package itself into the virtualenv (not as an editable link to the
# sources, which are not copied to the production image).
RUN uv sync --frozen --no-dev --no-editable \
    $([ "$INSTALL_DEBUGPY" = "true" ] && echo "--group debug")

#==============================================================================
FROM python:3.14-slim AS production

WORKDIR /pygeoapi

ENV PYGEOAPI_CONFIG=/pygeoapi/pygeoapi-config.yml
ENV PYGEOAPI_OPENAPI=/pygeoapi/pygeoapi-openapi.yml
ENV PATH="/pygeoapi/.venv/bin:$PATH"
# Fewer glibc malloc arenas keep the memory freed by the worker threads from
# fragmenting across arenas and inflating the RSS.
ENV MALLOC_ARENA_MAX=2
# uvicorn answers 503 once this many connections or requests are open, instead
# of accepting more work than fits in memory.
ENV UVICORN_LIMIT_CONCURRENCY=100

RUN groupadd --gid 1001 pygeoapi \
 && useradd --uid 1001 --gid pygeoapi --no-create-home pygeoapi \
 && chown -R pygeoapi:pygeoapi /pygeoapi

COPY --from=builder --chown=pygeoapi:pygeoapi /pygeoapi/.venv /pygeoapi/.venv
COPY --chown=pygeoapi:pygeoapi pygeoapi-config.yml /pygeoapi/pygeoapi-config.yml
COPY --chown=pygeoapi:pygeoapi docker-entrypoint.sh /pygeoapi/docker-entrypoint.sh
COPY --chown=pygeoapi:pygeoapi config-files /pygeoapi/config-files
# Database bootstrap, run by the init container (see README, Database)
COPY --chown=pygeoapi:pygeoapi scripts /pygeoapi/scripts

# Set build infos as docker labels
ARG VERSION=unknown
ARG GIT_HASH=unknown
ARG GIT_BRANCH=unknown
ARG GIT_DIRTY=""
ARG AUTHOR=unknown
LABEL git.hash=$GIT_HASH
LABEL git.branch=$GIT_BRANCH
LABEL git.dirty=$GIT_DIRTY
LABEL author=$AUTHOR
LABEL version=$VERSION

USER pygeoapi

EXPOSE 80

ENTRYPOINT [ "./docker-entrypoint.sh" ]
