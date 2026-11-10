#!/usr/bin/env bash

# No `pygeoapi openapi generate` here: the app discovers the PostGIS tables on
# startup and writes the config and OpenAPI document itself (swissgeo_oaf.discovery),
# next to $PYGEOAPI_OPENAPI -- so that directory has to be writable.

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
