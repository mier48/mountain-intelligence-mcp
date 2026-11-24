# Mountain Intelligence MCP

Servidor **Model Context Protocol** (`mountain-intelligence`) de solo lectura que expone un catálogo técnico
trazable de material de montaña (botas, crampones, mochilas, chaquetas…) y un motor de recomendación
determinista basado en reglas. MCP es la única interfaz de consulta: no hay web, API REST, admin ni chatbot.
El cliente LLM (Claude, una app propia…) interpreta la intención y redacta la respuesta; el servidor devuelve
datos estructurados, reproducibles y con procedencia. No necesita ninguna clave de proveedor LLM.

> **Estado (MVP 0.1.0).** Cadena completa operativa: registro de fuentes → ingesta → normalización →
> validación → revisión → publicación → búsqueda / comparación / recomendación / compatibilidad por MCP
> (stdio y Streamable HTTP). El catálogo incluido es **sintético** (marcas y productos inventados) y está
> excluido de las respuestas salvo que se active `MI_INCLUDE_SYNTHETIC_DATA=true`. **Ninguna fuente real
> está habilitada**: el registro (`data/sources.yaml`) documenta 27 marcas, 14 fuentes preconfiguradas y
> 6 rutas de datos candidatas, pero no se ha encontrado una autorización verificable de reutilización
> (ver [docs/source-evaluation.md](docs/source-evaluation.md)). Perfiles de actividad disponibles:
> `winter_mountaineering`, `summer_alpine_mountaineering`, `winter_hiking`, `hiking_trekking` y
> `trail_running`, con referencias publicadas y pendientes de revisión experta.

## Contenido

- [Arquitectura](#arquitectura) · [Inicio rápido (Docker Compose)](#inicio-rápido-docker-compose) ·
  [Desarrollo local](#desarrollo-local) · [Variables de entorno](#variables-de-entorno) ·
  [Conectar un cliente MCP](#conectar-un-cliente-mcp) · [Herramientas MCP](#herramientas-mcp) ·
  [Operación (CLI)](#operación-cli) · [Pruebas](#pruebas) · [Limitaciones](#limitaciones)
- Documentación técnica: [arquitectura y decisiones](docs/architecture.md) ·
  [contratos de datos](docs/data-contracts.md) · [seguridad](docs/security.md) ·
  [operación y despliegue](docs/operations.md) · [evaluación de fuentes](docs/source-evaluation.md) ·
  [verificación y rendimiento](docs/verification.md) · [backlog](docs/backlog.md)

## Arquitectura

Monolito modular en Python 3.13. Django 5.2 LTS se usa solo para ORM, migraciones, configuración y comandos
de gestión (sin servidor HTTP de Django). El SDK oficial de MCP (v2.3, `MCPServer`) sirve stdio y Streamable
HTTP sobre las mismas ocho herramientas. PostgreSQL es la fuente de verdad (búsqueda de texto nativa + GIN +
trigramas, especificaciones en JSONB validadas con Pydantic). Celery + Redis ejecutan la ingesta programada.

```
fuentes autorizadas ─► adaptadores (JSON/CSV, JSON-LD) ─► normalización + validación ─► revisión ─► PostgreSQL
                                                                                             │
CLI de operación (manage.py) ───────────────────────────────────────────────────────────────┤
Celery beat ─► worker ─► pipeline de ingesta                                                │
                                                       servicios de dominio ◄────────────────┘
                                         (búsqueda, comparación, recomendación, compatibilidad)
                                                                 │
                                               servidor MCP (stdio | Streamable HTTP /mcp)
                                                                 │
                                                       clientes MCP / LLMs
```

| Paquete | Responsabilidad |
|---|---|
| `catalog/` | Taxonomía declarativa (10 categorías), unidades, normalización, modelos, búsqueda y comparación |
| `sources/` | Fuentes, registros de fuente, evidencias por campo, registro YAML de marcas/fuentes |
| `ingestion/` | Contratos de adaptadores, fetcher seguro (SSRF), pipeline idempotente, revisión, tareas Celery |
| `recommendations/` | Perfiles de actividad versionados, motor de recomendación, compatibilidad bota/crampón |
| `offers/` | Ofertas e histórico de precios (independientes del catálogo técnico) |
| `mcp_server/` | Herramientas MCP, contratos tipados, transporte HTTP (auth, rate limit, Host/Origin) |

## Inicio rápido (Docker Compose)

Requisitos: Docker con Compose v2.

```bash
cp .env.example .env
# Obligatorio en .env: POSTGRES_PASSWORD y MCP_AUTH_TOKENS. Genera el token con:
#   openssl rand -hex 32        (PowerShell: -join ((1..32) | % { '{0:x2}' -f (Get-Random -Max 256) }))
# Si MCP_AUTH_TOKENS está vacío, `docker compose` se niega a arrancar y lo indica.
# Para la demo con datos sintéticos: MI_INCLUDE_SYNTHETIC_DATA=true
docker compose up -d --build --wait

# Registro de marcas/fuentes (todas quedan deshabilitadas) y catálogo demo sintético:
docker compose exec mountain-intelligence-mcp python manage.py import_sources
docker compose exec mountain-intelligence-mcp python manage.py seed_demo_data

curl -s http://127.0.0.1:8000/healthz          # {"status": "ok"}
```

En Windows PowerShell, `curl` es un alias de `Invoke-WebRequest`: usa `curl.exe` o
`Invoke-RestMethod http://127.0.0.1:8000/healthz`.

Servicios: `postgres`, `redis`, `migrate` (one-shot: migraciones + perfiles + reglas),
`mountain-intelligence-mcp` (HTTP en `127.0.0.1:8000/mcp`), `worker` y `scheduler` (Celery beat). Todos con
healthcheck. PostgreSQL y Redis no se publican y viven en una red interna sin salida. Al arrancar no se ejecuta
ninguna ingesta externa. En producción coloca un proxy inverso TLS delante del puerto 8000
([docs/operations.md](docs/operations.md)).

Los precios del catálogo demo se fechan en el momento de la carga y caducan a los `MI_OFFER_MAX_AGE_DAYS`
(7 por defecto); vuelve a ejecutar `seed_demo_data` (idempotente) para refrescarlos.

## Desarrollo local

Requisitos: Python 3.12+ (3.13 recomendado), [uv](https://docs.astral.sh/uv/), PostgreSQL 16+ y Redis
(Redis solo para Celery).

```bash
uv sync                                    # dependencias fijadas en uv.lock
export POSTGRES_PASSWORD=... POSTGRES_HOST=localhost MI_INCLUDE_SYNTHETIC_DATA=true
uv run python manage.py migrate
uv run python manage.py load_activity_profiles
uv run python manage.py load_compatibility_rules
uv run python manage.py import_sources
uv run python manage.py seed_demo_data

uv run mountain-intelligence-mcp stdio                                   # transporte local
MCP_AUTH_TOKENS=dev-token uv run mountain-intelligence-mcp http --port 8000   # transporte remoto
uv run celery -A mountain_intelligence.config.celery worker -l INFO
uv run celery -A mountain_intelligence.config.celery beat -l INFO
```

## Variables de entorno

Todas documentadas en [`.env.example`](.env.example). Las más relevantes:

| Variable | Por defecto | Uso |
|---|---|---|
| `POSTGRES_HOST/PORT/DB/USER/PASSWORD` | `localhost/5432/mountain_intelligence/mountain/—` | Conexión a PostgreSQL |
| `REDIS_URL`, `CELERY_BROKER_URL` | — / `redis://localhost:6379/0` | Caché de bloqueos y broker Celery |
| `MCP_AUTH_TOKENS` | vacío | Tokens bearer aceptados por HTTP (obligatorio salvo modo desarrollo) |
| `MCP_ALLOW_UNAUTHENTICATED` | `false` | Solo desarrollo local; sin token el servidor HTTP se niega a arrancar |
| `MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS` | localhost / vacío | Validación de `Host` y `Origin` (anti DNS-rebinding) |
| `MCP_RATE_LIMIT_PER_MINUTE` | `120` | Límite por IP cliente y proceso |
| `MI_INCLUDE_SYNTHETIC_DATA` | `false` | Incluir el catálogo demo sintético en las respuestas |
| `MI_STALE_AFTER_DAYS` / `MI_OFFER_MAX_AGE_DAYS` | `120` / `7` | Frescura de datos y validez de precios |
| `INGESTION_SCHEDULE_ENABLED` | `false` | Activa el despacho programado de fuentes habilitadas |
| `INGESTION_*` | ver `.env.example` | User-Agent, timeouts, tamaño máximo, retardo por dominio, robots.txt |
| `DB_STATEMENT_TIMEOUT_MS` | `15000` | Límite por sentencia SQL |

## Conectar un cliente MCP

**Local (stdio), Claude Code** — sintaxis de `claude mcp add`; todo lo que va tras `--` se pasa al servidor
sin cambios:

```bash
claude mcp add --transport stdio \
  --env POSTGRES_HOST=localhost --env POSTGRES_PASSWORD=... --env MI_INCLUDE_SYNTHETIC_DATA=true \
  mountain-intelligence -- uv --directory /ruta/a/mountain-intelligence-mcp run mountain-intelligence-mcp stdio
```

**Local (stdio), Claude Desktop** — `claude_desktop_config.json` (formato `mcpServers` con
`command`/`args`/`env`; no probado en este entorno):

```json
{
  "mcpServers": {
    "mountain-intelligence": {
      "command": "uv",
      "args": ["--directory", "/ruta/a/mountain-intelligence-mcp", "run", "mountain-intelligence-mcp", "stdio"],
      "env": {"POSTGRES_HOST": "localhost", "POSTGRES_PASSWORD": "...", "MI_INCLUDE_SYNTHETIC_DATA": "true"}
    }
  }
}
```

**Remoto (Streamable HTTP) con token bearer, Claude Code:**

```bash
claude mcp add --transport http mountain-intelligence https://mcp.tu-dominio.example/mcp \
  --header "Authorization: Bearer $MCP_TOKEN"
```

o en `.mcp.json`:

```json
{"mcpServers": {"mountain-intelligence": {"type": "http", "url": "https://mcp.tu-dominio.example/mcp",
  "headers": {"Authorization": "Bearer ${MCP_TOKEN}"}}}}
```

**MCP Inspector:** `npx @modelcontextprotocol/inspector` y conecta a `http://127.0.0.1:8000/mcp` con la
cabecera `Authorization: Bearer <token>`.

Compatibilidad verificada: el cliente oficial del SDK de Python (v2.3) sobre stdio y Streamable HTTP, tanto
con el handshake `initialize` (protocolo 2025-06-18/2025-11-25) como con el descubrimiento moderno
(`mode="auto"`). La autenticación por token estático **no** es el flujo OAuth de MCP: los clientes que solo
admiten conectores OAuth (p. ej. conectores personalizados de claude.ai) no pueden usarla todavía
(ver [docs/security.md](docs/security.md)).

## Herramientas MCP

Todas son de solo lectura (`readOnlyHint: true`), validan la entrada con esquemas tipados y devuelven
`structuredContent` con el mismo sobre: `status` (`ok | partial | not_found | insufficient_data`), `data`,
`warnings`, `coverage`, `pagination` y `meta`. Los argumentos inválidos devuelven `isError: true` con prefijo
`validation_error:`. `null` significa **desconocido**, nunca falso ni cero.

| Tool | Para qué |
|---|---|
| `list_categories` | Categorías, campos filtrables (tipo, unidad canónica, operadores, valores) y perfiles de actividad |
| `search_products` | Texto es/en (sinónimos, SKU/MPN/GTIN) + filtros tipados `eq/in/gte/lte/contains…`, paginación estable |
| `get_product` | Ficha completa: cada campo con unidad y evidencias (URL, fecha, estado), variantes, ofertas, fuentes |
| `compare_products` | Matriz homogénea de 2–5 productos, desconocidos explícitos y limitaciones (peso por unidad/par, RET vs MVTR…) |
| `recommend_products` | Filtros obligatorios + presupuesto, separación de candidatos a verificar, ranking explicado y versionado |
| `check_compatibility` | Bota/crampón: `compatible / incompatible / conditional / unknown` con motivos, condiciones y evidencias |
| `get_activity_requirements` | Requisitos orientativos por actividad y condiciones, contexto que falta y estado de revisión del perfil |
| `build_equipment_list` | Lista por categorías: lo que tienes, lo que falta y candidatos; total solo si todos los precios se conocen |

Ejemplos de llamadas (`tools/call`, argumentos JSON) para consultas en español:

```jsonc
// «Botas para alpinismo invernal de iniciación en la Cordillera Cantábrica por menos de 350 €»
{"name": "recommend_products", "arguments": {"activity": "winter_mountaineering", "category": "mountaineering_boot",
  "experience": "beginner", "budget_eur": 350, "conditions": {"snow": true, "ice": true, "min_temperature_c": -5}}}

// «Chaqueta impermeable para trekking con ventilaciones y menos de 450 g»
{"name": "search_products", "arguments": {"query": "chaqueta impermeable", "category": "hardshell_jacket",
  "filters": [{"field": "ventilation", "op": "contains", "value": ["pit_zips"]},
              {"field": "weight_g", "op": "lte", "value": 450}]}}

// «¿Qué crampones son compatibles con estas botas?»
{"name": "check_compatibility", "arguments": {"product_ids": ["example-outdoor-alpine-gtx-example",
  "demo-peaks-ice-lever-12"], "boot_size_eu": 43}}

// «Tengo chaqueta impermeable, polar y plumas: ¿qué me falta para una salida invernal?»
{"name": "build_equipment_list", "arguments": {"activity": "winter_mountaineering",
  "conditions": {"snow": true, "ice": true, "min_temperature_c": -8},
  "owned_items": [{"category": "hardshell_jacket"}, {"category": "midlayer"}, {"category": "insulated_jacket"}]}}
```

Los identificadores anteriores pertenecen al catálogo demo sintético.

## Operación (CLI)

Sin interfaz web: toda la operación interna se hace con comandos (`python manage.py <comando>`, admiten
`--json`, códigos de salida coherentes y transacciones). Detalle y procedimientos en
[docs/operations.md](docs/operations.md).

| Comando | Responsabilidad |
|---|---|
| `import_sources [ruta] [--check]` | Validar y aplicar `data/sources.yaml` (marcas y fuentes) |
| `list_sources` | Estado de acceso, habilitación y última ejecución |
| `set_source_status <slug> [--access …] [--enable/--disable] --actor --reason` | Cambios auditados de acceso/habilitación |
| `sync_source <slug> [--dry-run] [--max-items N] [--async] [--test-disabled]` | Ejecutar o encolar una ingesta |
| `import_products <fichero> --source <slug> --actor … [--dry-run]` | Importación JSON/CSV autorizada |
| `list_ingestion_runs [--source] [--errors]` | Métricas y errores de ejecuciones |
| `list_review_items [--status] [--kind] [--product]` | Cola de revisión |
| `review_item <id> --approve/--reject --actor --reason [--value] [--merge-into/--create-new]` | Decisión auditada |
| `validate_catalog [--strict]` | Esquemas, evidencias, datos caducados o con incidencias de fuente |
| `load_activity_profiles` / `load_compatibility_rules` | Cargar configuración versionada (editar exige subir versión) |
| `seed_demo_data` | Catálogo demo sintético (idempotente, vía el pipeline normal) |
| `benchmark_search [--products N]` | Medición de latencia sobre un dataset sintético reproducible |

## Pruebas

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest                       # requiere PostgreSQL accesible (crea test_mountain_intelligence)
uv run pytest -m "not protocol"     # sin las pruebas que lanzan procesos MCP reales
```

145 pruebas (más 1 de integración real opcional, desactivada por defecto): unidades y normalización, pipeline (idempotencia, historial, revisión, regresiones de
generación/peso/precio/certificaciones), JSON-LD y seguridad de red (SSRF, redirecciones, DNS rebinding,
tamaño, reintentos, robots.txt), búsqueda, comparación, recomendación, compatibilidad (4 estados), CLI,
programación, y protocolo MCP real en stdio y Streamable HTTP (auth, Host/Origin). Resultados y método en
[docs/verification.md](docs/verification.md).

## Limitaciones

- Sin fuentes reales habilitadas: el catálogo publicado real está vacío hasta disponer de permisos o feeds.
- Los perfiles de actividad están marcados `pending_expert_review`; cada respuesta lo advierte.
- La autenticación HTTP es por token estático (despliegues privados); OAuth 2.1 de MCP está en el backlog.
- Rate limiting en memoria por proceso; con varias réplicas usa el proxy inverso o un límite compartido.
- La compatibilidad bota/crampón nunca sustituye la comprobación física; ninguna herramienta evalúa la
  seguridad de una ruta.
