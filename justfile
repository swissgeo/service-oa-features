set shell := ["bash", "-c"]

# Start PostGIS, collector, Jaeger, Prometheus and (re)apply the schema and sample data
services-up:
    make services-up

# Stop the backing services (the PostGIS data volume is kept)
services-down:
    make services-down

# Serve the application locally (needs `just services-up` first)
serve:
    make serve

# Open a psql shell on the local PostGIS container
dbshell:
    make dbshell

# Re-apply the PostGIS bootstrap schema/seed data to the running database
reset-db:
    make reset-db

lint:
    uv run ruff check . --fix
