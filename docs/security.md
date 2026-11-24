# Seguridad, privacidad y fiabilidad

## Superficie expuesta

- Única interfaz de negocio: MCP. Las ocho tools son de solo lectura, no aceptan URLs para descargar y no
  modifican el catálogo. Toda escritura pasa por la CLI y los workers.
- HTTP: `POST/GET/DELETE /mcp` (Streamable HTTP, sin estado) y `GET /healthz` (sin autenticación, solo
  `{"status": "ok" | "unavailable"}`). PostgreSQL y Redis no se publican y están en una red Docker interna.

## Autenticación (MVP)

- `MCP_AUTH_TOKENS`: tokens bearer comparados en tiempo constante sobre su SHA-256. Sin token válido ⇒ `401`
  con `WWW-Authenticate: Bearer`. Si no hay tokens configurados el servidor HTTP **se niega a arrancar**,
  salvo `MCP_ALLOW_UNAUTHENTICATED=true` (solo desarrollo local; se registra un aviso).
- Es un mecanismo para despliegues privados con clientes que permiten cabeceras (Claude Code, Inspector,
  aplicaciones propias). **No** es la autorización OAuth 2.1 de la especificación MCP ni es interoperable con
  clientes que solo admiten conectores OAuth. Para una integración pública: implementar `TokenVerifier` +
  `AuthSettings` del SDK (metadatos RFC 9728 en `/.well-known/oauth-protected-resource`) con un servidor de
  autorización real (backlog).
- stdio no tiene autenticación: hereda los permisos del usuario que lanza el proceso.

## Protecciones HTTP

- Validación de `Host` y `Origin` del SDK (`TransportSecuritySettings`): `MCP_ALLOWED_HOSTS` /
  `MCP_ALLOWED_ORIGINS`. Host desconocido ⇒ `421`; origen no permitido ⇒ `403`.
- Tamaño máximo de petición (`MCP_MAX_REQUEST_BODY_BYTES`, 256 KiB), límite por IP (`MCP_RATE_LIMIT_PER_MINUTE`,
  en memoria por proceso), `statement_timeout` en PostgreSQL, límites de tamaño en todas las entradas
  (listas, textos, filtros ≤ 12, `limit` ≤ 25, offset ≤ 500, comparar ≤ 5).
- TLS: obligatorio en producción mediante proxy inverso. `compose.prod.yaml` + `deploy/Caddyfile` lo
  resuelven con Caddy: HTTPS automático con HSTS, solo `/mcp` y `/healthz` expuestos, cuerpo ≤ 1 MB, y la
  cabecera `Authorization` redactada en sus logs (comportamiento por defecto de Caddy). El puerto 8000 no se
  publica, de modo que `FORWARDED_ALLOW_IPS="*"` solo confía en contenedores de la propia red Compose; con un
  proxy propio, restringirlo a la IP del proxy.

## Ingesta segura (SSRF)

`ingestion/http.py`:

- Solo `http/https`, puertos 80/443, sin credenciales en la URL, host dentro de `allowed_domains` de la fuente.
- Redirecciones seguidas manualmente y revalidadas en cada salto (máx. `INGESTION_MAX_REDIRECTS`).
- Resolución DNS en el *backend de red* en el momento de conectar: todas las IP resueltas deben ser globales
  (se bloquean privadas, loopback, link-local/metadata, CGNAT, multicast, reservadas, IPv4-mapeadas); el socket
  se conecta a la IP verificada ⇒ sin ventana de DNS rebinding. Se ignoran los proxies del entorno para que el
  control sea efectivo.
- Timeouts, tamaño máximo de respuesta en streaming, descompresión gzip acotada, reintentos con backoff
  (429/5xx, `Retry-After` limitado), retardo mínimo por dominio, `robots.txt` (si no se puede leer, no se rastrea),
  peticiones condicionales ETag/Last-Modified.
- Una fuente solo se sincroniza si está habilitada **y** su `access_status` es `authorized` o `terms_permit`
  (validación en YAML, comprobación en código y restricción `CHECK` en la base de datos).

## Contenido externo no confiable

- Los adaptadores solo leen metadatos estructurados (`ld+json`); descripciones y texto comercial no se
  almacenan. Cualquier texto externo se valida contra el esquema y se recorta; nunca se ejecutan instrucciones
  encontradas en él (la evaluación de fuentes detectó un `robots.txt` con texto dirigido a agentes de IA).
- Los errores de herramientas no devuelven trazas, HTML ni secretos.

## Privacidad y registros

- No se almacenan conversaciones ni consultas de usuarios. Los logs son JSON a stderr, sin argumentos de las
  tools, y con redacción de `Bearer …`, `password=`, `token=` y credenciales en URLs.
- Secretos solo por variables de entorno; `.env` está en `.gitignore`; `DJANGO_SECRET_KEY` no firma nada.

## Fiabilidad de datos

- Un 404, un fallo transitorio o la desaparición de la descubierta nunca borran productos: se marcan
  (`source_status`, `missing_at_source`) y se abre revisión para retirarlos.
- Cambios en campos críticos (compatibilidad, normas, rangos de talla) de productos publicados no se aplican
  hasta revisión; mientras, la tool advierte del campo pendiente.
- Frescura: `last_verified_at` + `MI_STALE_AFTER_DAYS`; precios válidos `MI_OFFER_MAX_AGE_DAYS`.

## Riesgos conocidos

- Rate limit por proceso (no compartido entre réplicas).
- El fetcher no admite proxy de salida (deliberado para el control de IP); entornos con proxy obligatorio
  necesitan una variante con allowlist en el proxy.
- Los perfiles de actividad no han sido revisados por un profesional.
