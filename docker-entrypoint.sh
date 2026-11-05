#!/usr/bin/env bash

pygeoapi openapi generate pygeoapi-config.yml --output-file "${PYGEOAPI_OPENAPI:-/pygeoapi/pygeoapi-openapi.yml}"

if [ "${PYDEBUG}" = "true" ]; then
    echo PYDEBUG mode enabled!
    if ! python -c "import debugpy" 2>/dev/null; then
        echo "PYDEBUG requires an image built with --build-arg INSTALL_DEBUGPY=true" >&2
        exit 1
    fi
    python -m debugpy --listen 0.0.0.0:5678 -m uvicorn swissgeo_oaf.app:APP --host 0.0.0.0 --port 8080 --log-config /pygeoapi/config-files/logging-conf.yaml
else
    uvicorn swissgeo_oaf.app:APP --host 0.0.0.0 --port 8080 --log-config /pygeoapi/config-files/logging-conf.yaml
fi
