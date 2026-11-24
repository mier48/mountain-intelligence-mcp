# Verificación, pruebas y rendimiento

Fecha: 2026-11-24. Entorno: Linux x86_64, Python 3.13.18, PostgreSQL 16.15, Redis local.
Versiones fijadas (`uv.lock`): Django 5.2.19, mcp 2.3.0, Celery 5.7.0, psycopg 3.3.9, Pydantic 2.14.4,
httpx2 2.16.2, selectolax 1.0.2 (lexbor), Starlette 1.10.0, uvicorn 0.54.0.

## Resultados

| Comprobación | Resultado |
|---|---|
| `ruff check .` / `ruff format --check .` | sin errores |
| `manage.py makemigrations --check` | sin cambios pendientes |
| Migraciones desde base vacía + `seed_demo_data` | OK; segunda ejecución: 34 `unchanged`, 0 nuevos |
| `pytest` | **177 pruebas superadas** y 1 opcional omitida (integración con fuente real, `-m live_source`) (≈70 s, Python 3.12) |
| Migración desde la versión anterior | base creada con el código previo (registro, catálogo demo, producto Altus con su `SourceRecord`) ⇒ `migrate` aplica `catalog.0002`, `ingestion.0002`, `sources.0002`; productos, registros y búsqueda intactos; `uncategorized` creado; una sincronización Altus simulada reutiliza el registro existente (misma categoría) y añade los productos nuevos como `uncategorized` |
| Protocolo MCP real, stdio | cliente oficial del SDK lanza `python -m mountain_intelligence.mcp_server stdio`: `initialize` (modo `legacy`) y descubrimiento moderno (`auto`), `tools/list` (9) y `tools/call` |
| Protocolo MCP real, Streamable HTTP | servidor uvicorn en subproceso: mismos flujos con token; sin token/erróneo ⇒ 401; `Host` ajeno ⇒ 421/400; `Origin` ajeno ⇒ 403; sin tokens configurados el servidor no arranca |
| Docker Compose (ver nota) | `docker compose up --wait`: los 5 servicios `healthy` y `migrate` con salida 0; `import_sources`, `seed_demo_data`, tarea Celery encolada con `sync_source --async` y procesada por el worker; `tools/call` vía `curl` con token; 401 sin token; `pg_dump`/`pg_restore` verificados (34 productos restaurados) |

### Nota sobre la verificación Docker

El sandbox de desarrollo no puede descargar imágenes de registros (Docker Hub, GHCR, ECR y mirror.gcr.io
bloqueados). Para no dejar el despliegue sin probar se construyó la imagen con imágenes base **sustitutas
locales** (Ubuntu 24.04 con Python 3.12 vía `debootstrap`, el binario de uv 0.11 del host, y PostgreSQL 16 /
Redis de Ubuntu con un entrypoint equivalente) usando los `ARG PYTHON_IMAGE`/`UV_IMAGE` del `Dockerfile` y un
fichero de override de Compose fuera del repositorio. Quedan, por tanto, **sin verificar en este entorno**: la
construcción con `python:3.13-slim-bookworm`, `ghcr.io/astral-sh/uv:0.11`, `postgres:17-alpine` y
`redis:8-alpine` reales. El job `docker` de `.github/workflows/ci.yml` ejecuta exactamente esa comprobación
con las imágenes oficiales.

**Actualización (2026-11-24, equipo del usuario, Docker sobre WSL2):** con las imágenes oficiales la imagen
se construye (Python 3.13); `postgres` 17.12 y `redis` 8 arrancan sanos; `migrate` aplica las migraciones y
carga perfiles y reglas (código 0); `worker` y `scheduler` arrancan. El servicio MCP no arrancó porque
`MCP_AUTH_TOKENS` estaba vacío (bloqueo de seguridad previsto). Desde entonces `compose.yaml` comprueba esa
variable y aborta `docker compose up` con un mensaje claro, en lugar de dejar el contenedor en un bucle de
reinicios.

## Cobertura funcional de las pruebas

- Unidades/normalización: separadores decimales ambiguos, conversión de unidades, unidades ausentes, alias
  es/en, booleanos, `null` = desconocido, coherencia (welts, rangos, fill power), esquemas de las 10 categorías.
- Pipeline: idempotencia (productos, variantes, evidencias), historial de evidencias, cambios críticos a
  revisión y aprobación/rechazo auditados, certificaciones, valores inválidos bloqueados, publicación tras
  revisión, generaciones distintas (regresión), mismo nombre sin generación ⇒ revisión, nombres similares no
  fusionados, peso por par/unidad/desconocido (regresión), conflictos entre fuentes, desaparición en fuente sin
  borrado, aislamiento de errores, dry-run, fuentes no autorizadas, bloqueo concurrente, histórico de precios.
- JSON-LD y red: ProductGroup/@graph/listas, GTIN inválido descartado, texto comercial no almacenado, sitemap +
  `url_include`, ETag/304, 404 ⇒ marcado, robots.txt, SSRF (esquemas, puertos, credenciales, IP literales,
  subdominios, redirecciones a metadata/dominios ajenos, resolución a IP privada en el backend de conexión),
  límite de tamaño, reintentos con `Retry-After`, bucle de redirecciones.
- Consultas: exclusión de sintéticos por defecto, búsqueda bilingüe, identificadores, filtros tipados y
  rechazo de campos/operadores/valores, desconocidos que no pasan filtros, paginación estable y cursores
  ligados a la consulta, ficha con evidencias, comparación con limitaciones (base de peso, talla de referencia,
  RET vs MVTR, protocolos).
- Recomendación: exclusión por obligatorios y presupuesto, precio desconocido ⇒ verificación, datos ausentes
  puntúan 0, contexto desconocido aplicado de forma conservadora, marcas preferidas, material en propiedad.
- Compatibilidad: `compatible`, `conditional`, `incompatible` (tipos y talla), `unknown` (sin declaración y
  evidencia contradictoria), aserciones sintéticas ignoradas fuera de demo.
- Catálogo universal: URLs canónicas (idiomas, rutas de colección, parámetros, barra final), descubrimiento
  completo sin palabras clave, sitemaps no de producto no leídos, sitemap hijo ilegible o tope `max_urls` ⇒
  descubrimiento incompleto, extracción Shopify (variantes, opciones, GTIN válido/no válido, precio en céntimos,
  disponibilidad, imágenes por variante, etiquetas, ids externos, atributos `Nombre: valor` sin prosa),
  reglas de clasificación por campo y respaldo `uncategorized`, reglas reales de Altus (casos positivos y
  falsos positivos evitados), publicación y búsqueda de `uncategorized`, ficha con metadatos y disponibilidad,
  idempotencia (ETag/304 y hash), lotes con cursor y reanudación, ciclo completo solo al final, scheduler por
  lotes, ejecuciones truncadas sin marcar ausencias, reactivación al reaparecer, alcance restringido
  (`out_of_scope`), reclasificación progresiva sin duplicados y sin degradación, re-identificación de registros
  antiguos, importación estructurada `uncategorized`, exclusión de recomendaciones/comparación/compatibilidad/
  filtros, productos sin datos técnicos nunca recomendados, validación del registro, `classify_product` auditado
  y exposición MCP (`list_categories`, `recommend_products`, `search_products`).
- CLI y operación: registro de fuentes y restricción de habilitación (también a nivel BD), importación con
  dry-run, cola de revisión, ejecuciones, `validate_catalog --strict`, versionado de perfiles, despachador
  programado, redacción de secretos en logs.

### Sin verificar en este entorno: Altus en vivo

El entorno de desarrollo no tiene salida de red hacia `wearealtus.com` (proxy 403), así que la sincronización
real no se ha ejecutado aquí. Lo que sí se comprobó desde fuera (2026-11-24): el `sitemap.xml` es un índice
Shopify con `sitemap_products_1.xml` y su copia `/en/`, colecciones, páginas y blogs; `robots.txt` permite
`/products/` y contiene texto dirigido a agentes de IA (ignorado); el JSON público de producto
(`/products/<handle>.js`) expone variantes con SKU, código de barras, precio en céntimos, disponibilidad,
opciones Color/Talla, etiquetas e imágenes, que es el formato que cubren las pruebas con fixtures sintéticos.
Primera prueba recomendada en el servidor: `sync_source altus-website --dry-run --max-items 10`.

## Rendimiento medido

`python manage.py benchmark_search --products 5000 --repeat 30` (5.000 productos sintéticos generados con
semilla fija dentro de una transacción revertida; PostgreSQL 16 local, sin `ANALYZE` previo):

| Consulta | p50 | p95 |
|---|---|---|
| Texto «botas alpinismo glacier» | 23,9 ms | 27,8 ms |
| Texto sin coincidencia completa → relajado + trigramas | 148,6 ms | 169,4 ms |
| Filtros: botas `weight_per_boot_g ≤ 800` + tipo de crampón | 19,2 ms | 24,5 ms |
| Texto + filtros: crampones de acero | 31,6 ms | 31,9 ms |
| Búsqueda por identificador | 13,3 ms | 13,6 ms |
| `get_product` | 8,8 ms | 10,3 ms |
| `compare_products` (3) | 7,0 ms | 8,7 ms |

No se han fijado objetivos de latencia; estas cifras son la línea base. El camino relajado es el más lento
(similitud de trigramas sobre `search_document`); es el primer candidato a optimizar si las mediciones con datos
reales lo justifican.

## Revisión de datos de referencia (2026-11-24, tarde)

- `data/` reescrito con la investigación de `docs/source-evaluation.md`: registro de marcas y fuentes
  ampliado, perfiles de actividad v2 más `summer_alpine_mountaineering`, y regla de compatibilidad v2
  con matriz B/C.
- Probada la ruta de actualización sobre una base con la versión 1 cargada: `load_activity_profiles`
  crea y activa la v2 (la v1 queda inactiva), y `load_compatibility_rules` crea la regla v2 y retira la v1.
- `hiking_boot` pasa a la versión de esquema 2 (añade `boot_rating`). Los productos existentes siguen
  siendo válidos; `validate_catalog` avisa de la versión antigua hasta la siguiente ingesta.
