set shell := ["bash", "-c"]

run-docker-compose:
    docker compose up

# Open a psql shell on the local PostGIS container
dbshell:
    docker compose exec postgres psql -U swissgeo -d swissgeo

# Re-apply the PostGIS bootstrap schema/seed data to the running database
reset-db:
    docker compose run --rm init-database

lint:
    uv run ruff check . --fix
