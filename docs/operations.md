# Operación y despliegue

## Arranque y comprobaciones

```bash
cp .env.example .env              # POSTGRES_PASSWORD, MCP_AUTH_TOKENS, MCP_ALLOWED_HOSTS …
docker compose up -d --build --wait
docker compose ps                 # todos healthy; `migrate` termina con código 0
docker compose exec mountain-intelligence-mcp python manage.py import_sources
docker compose exec mountain-intelligence-mcp python manage.py list_sources
```

Para ejecutar comandos: `docker compose exec mountain-intelligence-mcp python manage.py <comando>`.

## Producción: TLS y proxy inverso

El despliegue de producción está empaquetado: `compose.prod.yaml` añade Caddy (certificados automáticos,
único servicio publicado en 80/443), retira la publicación del puerto 8000, arranca el MCP con
`--proxy-headers` y deriva `MCP_ALLOWED_HOSTS` de `MCP_PUBLIC_DOMAIN`. La configuración del proxy está en
`deploy/Caddyfile` (solo `/mcp` y `/healthz`; el resto responde 404). Guía paso a paso para
`mountain-mcp.ltrapi.com` (DNS, cortafuegos, `.env`, verificación, actualizaciones y problemas frecuentes):
README, sección «Despliegue en VPS».

```bash
# en .env del servidor
COMPOSE_FILE=compose.yaml:compose.prod.yaml
MCP_PUBLIC_DOMAIN=mountain-mcp.ltrapi.com
```

Con otro proxy (nginx, Traefik…): no uses el overlay; publica el MCP solo en `127.0.0.1`, añade el dominio a
`MCP_ALLOWED_HOSTS`, arranca con `--proxy-headers`, fija `FORWARDED_ALLOW_IPS` a la IP del proxy y desactiva
el buffering de respuestas en el proxy.

## Registrar y habilitar una marca nueva

1. **Registro** en `data/sources.yaml`: marca (dominio oficial verificado) y fuente con `adapter`,
   `allowed_domains`, `categories`, `config` y `access_status: unverified`, `enabled: false`.
   `python manage.py import_sources --check` y después `import_sources`.
2. **Validar el acceso**: revisar condiciones de uso, licencias, derechos sobre bases de datos y robots.txt;
   preferir API/feed/dataset licenciado. Documentar en `access_notes` y registrar la decisión:
   `set_source_status <slug> --access authorized --actor <tú> --reason "<referencia del permiso>"`.
   Si no es viable: `--access restricted` o `not_viable` (queda deshabilitada).
3. **Probar el adaptador** con fixtures locales (tests, como `tests/test_jsonld_and_fetching.py`) y después
   contra la fuente sin escribir: `sync_source <slug> --test-disabled --dry-run --max-items 5`.
   Ajustar `property_map`, `category_rules`, `url_include`.
4. **Primera carga** (sin `auto_publish`): `sync_source <slug> --test-disabled --max-items 50`; revisar
   `list_review_items` y decidir con `review_item`. Ejecutar `validate_catalog`.
5. **Habilitar** y programar: `set_source_status <slug> --enable …`, fijar `sync_frequency_hours` en el YAML
   (y reaplicar `import_sources`), `INGESTION_SCHEDULE_ENABLED=true`. Vigilar `list_ingestion_runs --errors`.

> `import_sources` aplica el YAML como fuente de verdad: los cambios hechos con `set_source_status` deben
> reflejarse también en el YAML versionado o se revertirán en la siguiente importación.

### Catálogo completo de un fabricante

1. Fuente con `categories: []` y descubrimiento estructural: `product_url_patterns` (p. ej.
   `^https://tienda\.test/products/[^/.]+$`) y, si es un índice, `sitemap_include` para leer solo los sitemaps de
   productos. Sin palabras clave de categoría en `url_include`.
2. `category_rules` conservadoras (campo `any` = URL + título + tipo + etiquetas + colecciones; `field` para
   acotar). Lo que no casa queda `uncategorized` y se publica igualmente.
3. `max_urls` holgado (es un tope de seguridad: alcanzarlo hace la ejecución `incomplete`) y `batch_size` para
   que cada ejecución quepa en `CELERY_TASK_TIME_LIMIT` (≈2–4 s por producto con la pausa por defecto).
4. `sync_source <slug> --dry-run --max-items 10`, después ejecuciones reales. `list_ingestion_runs` muestra
   `complete=yes/no` y el lote (`batch.already_done/total`). El scheduler encadena los lotes
   (`batch_interval_minutes`) y luego espera `sync_frequency_hours`. `--restart-cycle` empieza de cero.
5. Enriquecimiento progresivo: añadir reglas (los productos se reclasifican en la siguiente pasada sin
   duplicarse) o `classify_product <product_id> <categoría> --actor … --reason …` (auditado como
   `ReviewItem` `reclassification`). Buscar candidatos con `search_products` `classification=uncategorized`.

Tiendas Shopify: adaptador `shopify_product` (ver docstring de `ingestion/adapters/shopify.py`):
`capture_offers` + `currency`, `description_attributes`, `jsonld`, `collections`, `brand_from`
(`source`/`vendor`), `property_map`.

Añadir un adaptador nuevo: implementar `discover/fetch/extract` devolviendo `ExtractedProduct`, registrar con
`register(...)` en `ingestion/adapters/` y cubrirlo con tests sobre fixtures. Nunca escribir en modelos desde
el adaptador.

## Añadir una categoría

Añadir un `CategoryDefinition` en `catalog/taxonomy.py` (campos, unidades, alias, campos críticos,
derivados y comprobaciones). Después, reglas en las fuentes de catálogo completo para que los productos
`uncategorized` correspondientes pasen a la nueva categoría en la siguiente sincronización. Si se modifica una categoría existente, incrementar `schema_version`
y ejecutar `validate_catalog`. Las tools MCP no cambian; `list_categories` refleja la nueva definición.

## Cola de revisión

```bash
python manage.py list_review_items                      # pendientes
python manage.py review_item 12 --approve --actor ana --reason "Ficha oficial 2026"
python manage.py review_item 13 --approve --actor ana --reason "PDF fabricante" --value "1250 g"
python manage.py review_item 14 --approve --actor ana --reason "Mismo modelo" --merge-into marca-modelo
python manage.py review_item 15 --reject  --actor ana --reason "Ausencia temporal"
```

Tipos: `new_product`, `critical_change`, `conflict`, `ambiguous_match`, `invalid_value`, `missing_at_source`.

## Perfiles de actividad y reglas

Editar `data/activity_profiles.yaml` / `data/compatibility_rules.yaml` **creando una versión nueva**
(las versiones cargadas son inmutables) y ejecutar `load_activity_profiles` / `load_compatibility_rules`.
Tras la revisión por un profesional: `review_status: reviewed`, `reviewed_by`, `reviewed_at`.

## Copia de seguridad y restauración (PostgreSQL)

En producción, `deploy/backup.sh` automatiza la copia (verificación con `pg_restore --list`, retención
`KEEP_DAYS`, pensado para cron). Equivalente manual:

```bash
# Copia (formato custom, comprimida)
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > backups/mi-$(date +%F).dump

# Restauración en una base nueva y comprobación
docker compose exec -T postgres sh -c 'createdb -U "$POSTGRES_USER" mi_restore'
docker compose exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d mi_restore --no-owner' < backups/mi-AAAA-MM-DD.dump
# Para sustituir la principal: parar mcp/worker/scheduler, pg_restore --clean --if-exists sobre $POSTGRES_DB, arrancar.
```

Redis no contiene estado persistente (broker y bloqueos); el historial de ingestas vive en PostgreSQL.

## Observabilidad

Logs JSON en stderr de todos los servicios (`docker compose logs -f worker`), cada ejecución con `run_id` y
métricas (`discovered/new/updated/unchanged/not_modified/conflicts/invalid/errors/missing/reviews_opened`).
`validate_catalog --strict` es apto para una comprobación programada.
