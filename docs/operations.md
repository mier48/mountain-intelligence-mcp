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

Publica solo el proxy. Ejemplo con Caddy (certificados automáticos):

```
mcp.tu-dominio.example {
    reverse_proxy 127.0.0.1:8000
}
```

y en `.env`: `MCP_ALLOWED_HOSTS=mcp.tu-dominio.example`, `FORWARDED_ALLOW_IPS=<ip del proxy>`; añade
`--proxy-headers` al comando del servicio MCP para que el rate limit vea la IP real del cliente.

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

Añadir un adaptador nuevo: implementar `discover/fetch/extract` devolviendo `ExtractedProduct`, registrar con
`register(...)` en `ingestion/adapters/` y cubrirlo con tests sobre fixtures. Nunca escribir en modelos desde
el adaptador.

## Añadir una categoría

Añadir un `CategoryDefinition` en `catalog/taxonomy.py` (campos, unidades, alias, campos críticos,
derivados y comprobaciones). Si se modifica una categoría existente, incrementar `schema_version`
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
