param(
    [string]$Service = "all"
)

switch ($Service) {
    "api" {
        docker compose -f docker-compose.dev.yml build api
        docker compose -f docker-compose.dev.yml up -d api
    }

    "worker" {
        docker compose -f docker-compose.dev.yml build worker
        docker compose -f docker-compose.dev.yml up -d worker
    }

    "frontend" {
        docker compose -f docker-compose.dev.yml build frontend
        docker compose -f docker-compose.dev.yml up -d frontend
    }

    "all" {
        docker compose -f docker-compose.dev.yml up -d --build
    }
}
