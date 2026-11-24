# Arquitectura y decisiones

## Visión general

Un único repositorio y una única imagen de aplicación ejecutan cuatro procesos que comparten modelos y
servicios de dominio: servidor MCP (stdio o Streamable HTTP), worker Celery, Celery beat y CLI de operación.
No hay peticiones HTTP internas entre componentes: todos usan el ORM directamente.

```
mountain_intelligence/
├── config/            settings (solo variables de entorno), app Celery
├── catalog/           taxonomy.py (fuente única de esquemas), units.py, normalization.py,
│                      models.py, services.py, queries.py (búsqueda/detalle/comparación), synonyms.py
├── sources/           Source, SourceRecord, SpecificationEvidence, registry.py (YAML)
├── ingestion/         contracts.py, http.py (fetcher seguro), adapters/, pipeline.py, review.py, tasks.py
├── recommendations/   profiles.py, criteria.py, engine.py, compatibility.py, equipment.py
├── offers/            Offer, PriceObservation, precio comparable
├── mcp_server/        tools.py (8 tools), schemas.py (contratos), http.py (ASGI), __main__.py (entrypoint)
└── ops/               logging JSON con redacción, healthchecks, helpers de CLI
```

## Flujo de datos

`Discover → Fetch → Extract` (adaptador) `→ Normalize → Validate` (`catalog.normalization`, esquema Pydantic
de la categoría) `→ Match` (`ingestion.pipeline.match_product`) `→ Review/Publish` (cola `ReviewItem`).
Las herramientas MCP solo leen productos `published` (y excluyen los sintéticos salvo configuración).

## Decisiones (ADR resumidos)

| # | Decisión | Motivo / alternativas descartadas |
|---|---|---|
| 1 | **Django 5.2 LTS** solo como ORM/migraciones/CLI; `INSTALLED_APPS` sin admin, auth, sessions ni contenttypes | LTS hasta abril de 2028. Django 6.x no es LTS. Ninguna superficie HTTP de Django |
| 2 | **SDK oficial MCP 2.3 (`MCPServer`)**; HTTP con `streamable_http_app(stateless_http=True, json_response=True)` montado en Starlette | Sin estado de sesión → escalado horizontal sencillo; las tools no necesitan streaming servidor→cliente. No se añade FastAPI ni DRF |
| 3 | Tools **síncronas**: el SDK las ejecuta en hilos (`anyio.to_thread`), donde el ORM síncrono es seguro; `close_old_connections()` antes y después de cada llamada | Evita `SynchronousOnlyOperation` sin envolver cada consulta en `sync_to_async` |
| 4 | **Taxonomía declarativa** (`FieldSpec`/`CategoryDefinition`) que genera el modelo Pydantic versionado, los filtros permitidos, la comparación, la cobertura y la detección de cambios críticos | Una sola fuente de verdad; añadir una categoría no toca las tools. Se descarta EAV |
| 5 | Especificaciones en **JSONB** (`specifications` declaradas + `derived_specifications` calculadas) con índice GIN `jsonb_path_ops`; identidad y relaciones en columnas | Flexibilidad por categoría con validación estricta en escritura |
| 6 | Búsqueda: `tsvector` generado (`GeneratedField`, configuración `simple`, texto sin acentos) + índice GIN; trigramas (`pg_trgm`) solo como relajación y para detectar posibles duplicados | Sin Elasticsearch, pgvector ni embeddings. Sinónimos es/en controlados en `synonyms.py`; nunca validan requisitos |
| 7 | Filtros numéricos con `Cast(KeyTextTransform(...))` | El orden de `jsonb` sitúa `null` por debajo de los números: comparar `jsonb` directamente haría que «desconocido» pasara `lte` |
| 8 | **httpx2** para la ingesta | Ya es dependencia del SDK MCP (compatible con la API de httpx); evita una segunda librería HTTP |
| 9 | **selectolax (backend lexbor)** para JSON-LD | El backend Modest está retirado en selectolax 1.x; solo se extraen scripts `ld+json`, nunca texto comercial |
| 10 | Bloqueo por fuente con `cache.add` sobre Redis | Evita ejecuciones simultáneas de la misma fuente entre workers |
| 11 | Un único **dispatcher** periódico (beat) que decide qué fuentes tocan según su `sync_frequency_hours` | Frecuencias en BD, sin `django-celery-beat`; desactivado por defecto (`INGESTION_SCHEDULE_ENABLED`) |
| 12 | Perfiles y reglas en **YAML versionado**; la lógica de cada regla es código probado (`evaluator`) y la fila guarda versión, parámetros y referencias | Trazabilidad: editar una versión cargada se rechaza, hay que publicar otra |
| 13 | Compatibilidad: coincidencia de tipos ⇒ `conditional`, nunca `compatible`; `compatible` exige una declaración documentada a nivel de par (`CompatibilityAssertion`) sin evidencias contrarias | Requisito explícito de no confirmar ajustes físicos por clasificación genérica |
| 14 | Ranking con pesos versionados (`rules-v1`): los datos ausentes puntúan 0 en su componente y marcan el ranking como parcial; la normalización de componentes se hace sobre los criterios aplicables a la consulta (iguales para todos los candidatos) | No premiar la ausencia de datos. Requisitos obligatorios y presupuesto son filtros, no puntuación |
| 15 | Datos sintéticos con `data_status=synthetic_fixture`, marcas/GTIN inventados (prefijo GS1 interno 200) y excluidos por defecto | Aislamiento de demo frente a catálogo real |
| 16 | Token bearer estático para HTTP en el MVP | Despliegue privado y clientes que permiten cabeceras. OAuth 2.1 (protected resource metadata, `TokenVerifier` del SDK) queda en backlog |

## Suposiciones registradas

- Moneda de filtrado EUR; las ofertas en otra moneda se muestran pero no se convierten.
- Peso de calzado: se compara `weight_per_boot_g` (derivado de `weight_g` + `weight_basis`); sin base conocida no
  se deriva y no entra en filtros de peso.
- Un producto «falta en la fuente» solo si la descubierta fue completa (sin `--max-items` ni tope de URLs).
- Un valor existente aportado por otra fuente nunca se sobrescribe automáticamente: se abre un conflicto.
