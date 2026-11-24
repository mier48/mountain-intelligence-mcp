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
  [Despliegue en VPS (mountain-mcp.ltrapi.com)](#despliegue-en-vps-mountain-mcpltrapicom) ·
  [Cargar productos de una marca](#cargar-productos-de-una-marca) ·
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
HTTP sobre las mismas nueve herramientas. PostgreSQL es la fuente de verdad (búsqueda de texto nativa + GIN +
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
ninguna ingesta externa. Para producción con dominio y HTTPS sigue
[Despliegue en VPS](#despliegue-en-vps-mountain-mcpltrapicom).

Los precios del catálogo demo se fechan en el momento de la carga y caducan a los `MI_OFFER_MAX_AGE_DAYS`
(7 por defecto); vuelve a ejecutar `seed_demo_data` (idempotente) para refrescarlos.

## Despliegue en VPS (mountain-mcp.ltrapi.com)

Resultado: `https://mountain-mcp.ltrapi.com/mcp` con HTTPS automático (Let's Encrypt), **acceso público sin
autenticación** (para que ChatGPT y los conectores de claude.ai puedan usarlo; ver
[«Acceso público o con token»](#acceso-público-o-con-token)) y solo los puertos 80/443 abiertos.
Lo añaden dos ficheros sobre el `compose.yaml` base:

| Fichero | Qué hace |
|---|---|
| [`compose.prod.yaml`](compose.prod.yaml) | Añade **Caddy** (TLS, único servicio publicado), deja de publicar el puerto 8000, arranca el MCP con `--proxy-headers` y deriva `MCP_ALLOWED_HOSTS` del dominio |
| [`compose.traefik.yaml`](compose.traefik.yaml) | Alternativa a `compose.prod.yaml` si el VPS ya tiene **Traefik** en 80/443: sin Caddy y sin publicar ningún puerto; enruta con etiquetas ([paso 6b](#6b-si-el-vps-ya-tiene-traefik-en-80443)) |
| [`deploy/Caddyfile`](deploy/Caddyfile) | Proxy solo para `/mcp` y `/healthz` (el resto 404), HSTS, límite de cuerpo 1 MB, logs JSON |
| [`deploy/backup.sh`](deploy/backup.sh) | Copia diaria de PostgreSQL verificada con `pg_restore --list` y retención |

```
Internet ──443/80──► caddy ──red docker──► mountain-intelligence-mcp:8000 ──► postgres / redis (red interna)
                                            worker / scheduler (salida a Internet solo para fuentes autorizadas)
```

### 1. Requisitos

- VPS Linux de 64 bits (p. ej. Ubuntu 24.04 LTS o Debian 12), **2 GB de RAM** y 10 GB de disco
  como mínimo (la imagen se compila en el propio servidor).
- Acceso SSH con un usuario con `sudo`.
- Control del DNS de `ltrapi.com`.

### 2. DNS

Crea en la zona `ltrapi.com`:

| Tipo | Nombre | Valor |
|---|---|---|
| `A` | `mountain-mcp` | IPv4 pública del VPS |
| `AAAA` | `mountain-mcp` | IPv6 del VPS (solo si el VPS tiene IPv6; si no, no crees este registro) |

Comprueba antes de seguir (desde tu PC): `nslookup mountain-mcp.ltrapi.com` debe devolver la IP del VPS.
Si el DNS está en Cloudflare, deja el registro en **«DNS only»** (nube gris): el certificado lo emite Caddy
y el proxy de Cloudflare interferiría con el reto ACME y con las respuestas en streaming.

### 3. Preparar el servidor (una vez)

```bash
# Docker Engine + plugin Compose (script oficial; Compose v2.24+ es obligatorio)
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER" && newgrp docker
docker compose version

# Cortafuegos: SSH, HTTP (reto ACME y redirección) y HTTPS (TCP + UDP para HTTP/3)
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 443/udp
sudo ufw enable

# Actualizaciones de seguridad automáticas (Ubuntu/Debian)
sudo apt-get install -y unattended-upgrades
```

> Docker publica puertos saltándose `ufw`. Por eso el despliegue **solo** publica 80/443 (Caddy):
> PostgreSQL, Redis y el puerto 8000 del MCP no se exponen en el host.

### 4. Subir el código

**Opción A — repositorio Git** (privado, p. ej. en GitHub) tras hacer `git push` de la rama:

```bash
sudo mkdir -p /opt/mountain-intelligence-mcp && sudo chown "$USER": /opt/mountain-intelligence-mcp
git clone <url-del-repositorio> /opt/mountain-intelligence-mcp
cd /opt/mountain-intelligence-mcp
```

**Opción B — sin repositorio remoto**, desde tu PC (PowerShell, en la carpeta del proyecto). `git archive`
empaqueta solo lo confirmado en Git, así que tu `.env` local nunca sale de tu equipo:

```powershell
git archive --format=tar.gz -o mountain-intelligence-mcp.tar.gz HEAD
scp mountain-intelligence-mcp.tar.gz usuario@IP_DEL_VPS:/tmp/
```

y en el VPS:

```bash
sudo mkdir -p /opt/mountain-intelligence-mcp && sudo chown "$USER": /opt/mountain-intelligence-mcp
tar -xzf /tmp/mountain-intelligence-mcp.tar.gz -C /opt/mountain-intelligence-mcp
cd /opt/mountain-intelligence-mcp
```

### 5. Configurar `.env` de producción

```bash
cp .env.example .env
chmod 600 .env
openssl rand -hex 32   # → POSTGRES_PASSWORD
openssl rand -hex 32   # → MCP_AUTH_TOKENS (obligatorio para Compose aunque el acceso sea público)
nano .env
```

Valores a fijar (el resto puede quedarse por defecto):

```dotenv
COMPOSE_FILE=compose.yaml:compose.prod.yaml
MCP_PUBLIC_DOMAIN=mountain-mcp.ltrapi.com
POSTGRES_PASSWORD=<primer valor generado>
MCP_AUTH_TOKENS=<segundo valor generado>
MCP_ALLOW_UNAUTHENTICATED=true
MCP_ALLOWED_ORIGINS=https://chatgpt.com,https://claude.ai
INGESTION_USER_AGENT=MountainIntelligenceBot/0.1 (+https://mountain-mcp.ltrapi.com; contacto: <tu email>)
MI_INCLUDE_SYNTHETIC_DATA=false
```

- `COMPOSE_FILE` hace que cualquier `docker compose …` ejecutado en esta carpeta use el overlay de producción
  (no hace falta repetir `-f`).
- `MCP_ALLOWED_HOSTS` y `FORWARDED_ALLOW_IPS` los fija `compose.prod.yaml`; no hace falta tocarlos.
- `MCP_ALLOW_UNAUTHENTICATED=true` deja el endpoint **abierto**: cualquiera que conozca la URL puede llamar a
  las herramientas (todas de solo lectura). Lo limitan el rate limit por IP, el tamaño máximo de petición y el
  `statement_timeout` de PostgreSQL. Para cerrarlo, ponlo a `false` y `docker compose up -d`: a partir de ese
  momento solo entran clientes con un token de `MCP_AUTH_TOKENS` (ChatGPT dejará de poder conectarse).
- `MCP_ALLOWED_ORIGINS` solo actúa si el cliente envía cabecera `Origin`; incluir ChatGPT y claude.ai evita
  rechazos `403` si lo hacen.
- **Catálogo:** mientras no haya fuentes reales autorizadas, el catálogo real está vacío y las herramientas
  responden `not_found`/`insufficient_data`. Para una instancia de demostración pon
  `MI_INCLUDE_SYNTHETIC_DATA=true` y carga `seed_demo_data` (paso 6): las respuestas incluirán productos
  inventados marcados como sintéticos. No lo actives en una instancia que vaya a usar alguien como datos reales.
- Cambiar `POSTGRES_PASSWORD` después del primer arranque **no** cambia la contraseña ya creada en el volumen
  de PostgreSQL; fíjala bien antes de arrancar.

### 6. Arrancar

```bash
docker compose up -d --build --wait     # compila, migra, carga perfiles/reglas y obtiene el certificado
docker compose ps                       # todo healthy; `migrate` en "exited (0)"

docker compose exec mountain-intelligence-mcp python manage.py import_sources
# Solo si has activado MI_INCLUDE_SYNTHETIC_DATA=true:
docker compose exec mountain-intelligence-mcp python manage.py seed_demo_data
```

El primer arranque tarda unos minutos (compilación de la imagen). Caddy pide el certificado en cuanto arranca;
sigue el proceso con `docker compose logs -f caddy` (busca `certificate obtained successfully`).

### 6b. Si el VPS ya tiene Traefik en 80/443

Si otro stack (aquí `n8n-stack-traefik-1`) ya publica el 80/443, no se levanta Caddy ni se publica ningún
puerto: [`compose.traefik.yaml`](compose.traefik.yaml) conecta el MCP a la red de ese Traefik
(`traefik_proxy`) y le añade las etiquetas de enrutado: solo `/mcp` y `/healthz`, entrypoint `websecure`, HSTS.

Ese Traefik no tiene resolvedor ACME: sirve los certificados de su proveedor de ficheros
(`/srv/n8n-stack/traefik/certs.yml`). **Uno de ellos debe cubrir `mountain-mcp.ltrapi.com`**; si no, Traefik
responde con su certificado por defecto y los clientes MCP rechazan la conexión. Compruébalo:

```bash
sudo cat /srv/n8n-stack/traefik/certs.yml
sudo openssl x509 -in /srv/n8n-stack/traefik/tls/fullchain.crt -noout -subject -issuer -enddate -ext subjectAltName
```

Si no lo cubre, genera uno para el dominio (p. ej. un certificado de origen de Cloudflare si `ltrapi.com` va
proxificado por Cloudflare), cópialo a `/srv/n8n-stack/traefik/tls/` y añádelo a `certs.yml` (Traefik lo
recarga solo, tiene `watch`):

```yaml
tls:
  certificates:
    - certFile: /certs/fullchain.crt          # el existente, sin tocar
      keyFile: /certs/privkey.key
    - certFile: /certs/ltrapi.crt
      keyFile: /certs/ltrapi.key
```

Los ficheros nuevos deben estar dentro de un directorio montado en el contenedor de Traefik; hoy solo se
montan esos dos ficheros sueltos, así que añade el montaje en el compose del stack n8n y recrea solo Traefik.

Con el certificado listo:

```dotenv
# .env
COMPOSE_FILE=compose.yaml:compose.traefik.yaml
MCP_PUBLIC_DOMAIN=mountain-mcp.ltrapi.com
```

```bash
docker compose up -d --build --wait --remove-orphans   # elimina también el contenedor de Caddy que no arrancó
docker compose ps                                      # ninguna columna PORTS con 0.0.0.0
docker logs --since 5m n8n-stack-traefik-1 2>&1 | grep -i mountain
```

La verificación del paso 7 es la misma.

### 7. Verificar

Desde cualquier máquina con `bash` (en el VPS sirve):

```bash
URL=https://mountain-mcp.ltrapi.com

curl -s $URL/healthz                                    # {"status": "ok"}
curl -s -o /dev/null -w '%{http_code}\n' $URL/            # 404: nada más está expuesto

curl -s $URL/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'    # lista las 9 herramientas (sin token: acceso público)
```

Con `MCP_ALLOW_UNAUTHENTICATED=false`, la misma petición sin token devuelve `401`; añade
`-H "Authorization: Bearer $TOKEN"`.

Desde Windows PowerShell:

```powershell
Invoke-RestMethod https://mountain-mcp.ltrapi.com/healthz
$body = '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
(Invoke-RestMethod https://mountain-mcp.ltrapi.com/mcp -Method Post -Body $body -ContentType 'application/json' `
  -Headers @{ Accept = 'application/json, text/event-stream' }).result.tools.name
```

### 8. Conectar los clientes

URL del servidor para cualquier cliente: **`https://mountain-mcp.ltrapi.com/mcp`** (transporte Streamable HTTP).

**ChatGPT** (web; la disponibilidad depende del plan y, en espacios de trabajo, de los permisos que fije el
administrador):

1. Abre [chatgpt.com/plugins](https://chatgpt.com/plugins), pulsa **+** y elige **Add custom MCP server**.
2. Nombre: `Mountain Intelligence`; descripción opcional (p. ej. «Catálogo técnico de material de montaña»).
3. En **Connection** → **Server URL**: `https://mountain-mcp.ltrapi.com/mcp`.
4. **Authentication**: **No authentication**.
5. Acepta el aviso de riesgo, elige **Create as a plugin** e **instálalo**; después actívalo en la
   conversación en la que quieras usarlo.

Las 9 herramientas declaran `readOnlyHint`, así que ChatGPT las trata como de solo lectura y no pide la
confirmación que exige a las acciones de escritura.

**Claude (claude.ai / app de escritorio)**: añade un conector personalizado con la misma URL y sin
autenticación (Ajustes → Conectores).

**Claude Code**:

```bash
claude mcp add --transport http mountain-intelligence https://mountain-mcp.ltrapi.com/mcp
```

o en `.mcp.json` (detalles en [Conectar un cliente MCP](#conectar-un-cliente-mcp)).

#### Acceso público o con token

| `MCP_ALLOW_UNAUTHENTICATED` | Quién puede conectarse | Clientes compatibles |
|---|---|---|
| `true` (este despliegue) | Cualquiera con la URL; solo lectura, con rate limit | ChatGPT, claude.ai, Claude Code, Inspector… |
| `false` | Solo quien envíe `Authorization: Bearer <token>` de `MCP_AUTH_TOKENS` | Claude Code, Inspector, apps propias (no ChatGPT ni claude.ai) |

Para restringir el acceso y seguir usando ChatGPT hace falta OAuth 2.1 (está en el
[backlog](docs/backlog.md)): ChatGPT solo admite OAuth o «sin autenticación», no tokens fijos.

### 9. Operación diaria

```bash
cd /opt/mountain-intelligence-mcp
docker compose ps                                   # estado y healthchecks
docker compose logs -f --since 10m mountain-intelligence-mcp caddy
docker compose exec mountain-intelligence-mcp python manage.py list_sources
docker compose exec mountain-intelligence-mcp python manage.py validate_catalog
```

**Actualizar a una versión nueva** (las migraciones, perfiles y reglas se aplican solos vía `migrate`):

```bash
deploy/backup.sh                                    # copia antes de actualizar
git pull                                            # opción A; en la opción B, vuelve a extraer el tar.gz
docker compose up -d --build --wait
docker compose exec mountain-intelligence-mcp python manage.py import_sources   # si cambió data/sources.yaml
docker image prune -f
```

**Copias de seguridad** — `deploy/backup.sh` vuelca PostgreSQL a `backups/` (formato custom de `pg_dump`),
comprueba que el volcado es legible y borra los de más de 14 días (`KEEP_DAYS`). Prográmalo con `crontab -e`:

```cron
15 3 * * * /opt/mountain-intelligence-mcp/deploy/backup.sh >> /opt/mountain-intelligence-mcp/backups/backup.log 2>&1
```

Copia `backups/` fuera del VPS con regularidad (otro servidor, almacenamiento de objetos…): una copia en
el mismo disco no protege de la pérdida del servidor. Restauración: [docs/operations.md](docs/operations.md).
El volumen `caddy_data` guarda los certificados; no lo borres (Let's Encrypt limita las reemisiones).

**Ingesta programada:** desactivada por defecto. Solo tiene efecto con fuentes habilitadas y autorizadas
(procedimiento en [docs/operations.md](docs/operations.md)); entonces pon `INGESTION_SCHEDULE_ENABLED=true`
y `docker compose up -d`.

### 10. Problemas frecuentes

| Síntoma | Causa y solución |
|---|---|
| `docker compose` dice `required variable … is missing a value` | Falta `MCP_AUTH_TOKENS`, `POSTGRES_PASSWORD` o `MCP_PUBLIC_DOMAIN` en `.env` |
| `ports: !reset` o error de sintaxis al leer `compose.prod.yaml` | Compose anterior a v2.24: actualiza Docker (`docker compose version`) |
| El navegador/curl da error de certificado o `caddy` registra errores ACME | El DNS aún no apunta al VPS, el puerto 80/443 está cerrado (ufw o cortafuegos del proveedor) o el registro está proxificado en Cloudflare. Corrige y `docker compose restart caddy` |
| `Bind for 0.0.0.0:80 failed: port is already allocated` (o `:443`) | Otro proxy del VPS ya usa el puerto; no lo pares. Si es Traefik, usa `compose.traefik.yaml` ([paso 6b](#6b-si-el-vps-ya-tiene-traefik-en-80443)), que no publica ningún puerto |
| `401 Unauthorized` | `MCP_ALLOW_UNAUTHENTICATED=false` y falta la cabecera `Authorization: Bearer …` o el token no está en `MCP_AUTH_TOKENS` |
| ChatGPT no consigue conectar o crear el plugin | Comprueba `tools/list` sin token (paso 7) y revisa `docker compose logs --since 10m mountain-intelligence-mcp` mientras lo intentas: un `401` indica que el acceso público no está activo, un `403` que falta su origen en `MCP_ALLOWED_ORIGINS`, un `421` que la URL no es la del dominio |
| `421 Misdirected Request` | El `Host` no coincide con `MCP_PUBLIC_DOMAIN` (¿usas otra URL o la IP directamente?) |
| `403 Forbidden` desde una aplicación web | Su `Origin` no está en `MCP_ALLOWED_ORIGINS` (vacío = solo clientes no navegador) |
| `429 Too Many Requests` | Límite por IP (`MCP_RATE_LIMIT_PER_MINUTE`); súbelo si varios clientes comparten IP |
| `/healthz` devuelve `{"status": "unavailable"}` | El MCP no llega a PostgreSQL: `docker compose logs postgres migrate` |
| `migrate` termina con error | `docker compose logs migrate`; tras corregir, `docker compose up -d --wait` lo reintenta |

Para editar el Caddyfile: valida con
`docker compose run --rm --no-deps caddy caddy validate --config /etc/caddy/Caddyfile` y aplica con
`docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile`.

## Cargar productos de una marca

Los productos no se buscan en Internet en el momento de la consulta: se cargan antes en PostgreSQL desde las
fuentes de [`data/sources.yaml`](data/sources.yaml). Para una web de marca, el adaptador `jsonld_product`
lee su sitemap (si es un índice, recorre todos los sitemaps hijos), se queda con las URLs que casan con
`url_include`, descarga cada ficha respetando `robots.txt` y una pausa entre peticiones, y extrae los datos
`schema.org/Product` (JSON-LD). Solo sincroniza fuentes con `access_status: authorized` o `terms_permit`.

Ejemplo ya configurado: `arcteryx-website` (solo fichas `https://arcteryx.com/es/es/shop/…`, precios en €).
`altus-website` está preparada pero deshabilitada hasta decidir su `access_status`.

### 1. Configurar la fuente

En `data/sources.yaml` (en tu repositorio, luego `git push`):

```yaml
  - slug: arcteryx-website
    adapter: jsonld_product
    allowed_domains: [arcteryx.com]          # el sitemap y las fichas deben estar aquí
    access_status: terms_permit              # o authorized; sin esto no se sincroniza
    enabled: true
    auto_publish: true                       # publica sin revisión manual (ver paso 5)
    sync_frequency_hours: 168                # resincronización semanal
    config:
      discovery: sitemap
      sitemap_url: https://arcteryx.com/sitemap.xml
      url_include: ['^https://arcteryx\.com/es/es/shop/…']   # regex entre comillas simples
      category_rules:                        # categoría según la URL; la primera que casa gana
        - {pattern: "backpack", category: backpack}
      max_urls: 600                          # tope de fichas por ejecución (máx. 5000)
```

Si tienes un fichero de sitemap en vez de su URL: `discovery: urls` y `urls: [https://…, https://…]`.
Si la marca publica especificaciones como `additionalProperty` en su JSON-LD, mapéalas con
`property_map: {"Peso": {field: weight_g, unit: g}}`.

### 2. Aplicar el registro en el VPS

```bash
cd /opt/mountain-intelligence-mcp
git pull
docker compose exec mountain-intelligence-mcp python manage.py import_sources --check   # valida el YAML
docker compose exec mountain-intelligence-mcp python manage.py import_sources           # aplica
docker compose exec mountain-intelligence-mcp python manage.py list_sources
```

`import_sources` aplica el YAML como fuente de verdad: los cambios de estado se hacen en el YAML (o se
perderán en la siguiente importación). Si la imagen copia `data/` en lugar de montarla, ejecuta antes
`docker compose up -d --build --wait`.

### 3. Probar sin guardar nada

```bash
docker compose exec mountain-intelligence-mcp python manage.py sync_source arcteryx-website --dry-run --max-items 5
```

Ejecuta todo el pipeline con 5 fichas y deshace los cambios. Revisa las métricas (`discovered`, `new`,
`invalid`, `errors`) y los errores por URL. Si `discovered` es mayor que 0 pero no se extrae ningún producto,
esa web no publica JSON-LD de producto utilizable. Ajusta `url_include`, `category_rules` o `property_map` y
repite. Para probar una fuente aún con `enabled: false` (pero ya autorizada) añade `--test-disabled`.

### 4. Carga real

```bash
# Empieza con pocos:
docker compose exec mountain-intelligence-mcp python manage.py sync_source arcteryx-website --max-items 50
# Todos (hasta max_urls), en segundo plano a través del worker:
docker compose exec mountain-intelligence-mcp python manage.py sync_source arcteryx-website --async
docker compose logs -f worker      # para seguirlo
docker compose exec mountain-intelligence-mcp python manage.py list_ingestion_runs --source arcteryx-website
docker compose exec mountain-intelligence-mcp python manage.py list_ingestion_runs --errors
```

Hay una pausa entre peticiones (`INGESTION_MIN_DELAY_SECONDS`, 2 s por defecto), así que 600 fichas tardan
unos 20 minutos. Repetir la carga es seguro: no duplica productos y solo registra lo que ha cambiado.

### 5. Publicación y revisión

Con `auto_publish: true` (Arc'teryx y Altus) los productos nuevos se publican al cargarse y aparecen ya en
ChatGPT. Aun así, pasan por la cola de revisión los posibles duplicados, los conflictos entre fuentes, los
cambios en campos críticos y los valores inválidos. Sin `auto_publish`, también los productos nuevos:

```bash
docker compose exec mountain-intelligence-mcp python manage.py list_review_items
docker compose exec mountain-intelligence-mcp python manage.py list_review_items --kind new_product
docker compose exec mountain-intelligence-mcp python manage.py review_item <id> --approve --actor alberto --reason "Ficha oficial Arc'teryx"
docker compose exec mountain-intelligence-mcp python manage.py review_item <id> --reject --actor alberto --reason "No es producto técnico"
docker compose exec mountain-intelligence-mcp python manage.py validate_catalog
```

### 6. Resincronización automática

Con `enabled: true` en la fuente y `INGESTION_SCHEDULE_ENABLED=true` en `.env` (después `docker compose up -d`),
el scheduler vuelve a sincronizar cada fuente cada `sync_frequency_hours`.

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
| `MCP_AUTH_TOKENS` | vacío | Tokens bearer aceptados por HTTP (Compose exige al menos uno; ignorados en modo público) |
| `MCP_ALLOW_UNAUTHENTICATED` | `false` | `true` = endpoint público sin token (necesario para ChatGPT/claude.ai). Con `false` y sin tokens el servidor HTTP se niega a arrancar |
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
claude mcp add --transport http mountain-intelligence https://mountain-mcp.ltrapi.com/mcp \
  --header "Authorization: Bearer $MCP_TOKEN"
```

o en `.mcp.json`:

```json
{"mcpServers": {"mountain-intelligence": {"type": "http", "url": "https://mountain-mcp.ltrapi.com/mcp",
  "headers": {"Authorization": "Bearer ${MCP_TOKEN}"}}}}
```

**MCP Inspector:** `npx @modelcontextprotocol/inspector` y conecta a `http://127.0.0.1:8000/mcp` con la
cabecera `Authorization: Bearer <token>`.

Compatibilidad verificada: el cliente oficial del SDK de Python (v2.3) sobre stdio y Streamable HTTP, tanto
con el handshake `initialize` (protocolo 2025-06-18/2025-11-25) como con el descubrimiento moderno
(`mode="auto"`). La autenticación por token estático **no** es el flujo OAuth de MCP: los clientes que solo
admiten OAuth o «sin autenticación» (ChatGPT, conectores personalizados de claude.ai) necesitan el modo
público `MCP_ALLOW_UNAUTHENTICATED=true` (ver [Conectar los clientes](#8-conectar-los-clientes) y
[docs/security.md](docs/security.md)).

## Herramientas MCP

Todas son de solo lectura (`readOnlyHint: true`), validan la entrada con esquemas tipados y devuelven
`structuredContent` con el mismo sobre: `status` (`ok | partial | not_found | insufficient_data`), `data`,
`warnings`, `coverage`, `pagination` y `meta`. Los argumentos inválidos devuelven `isError: true` con prefijo
`validation_error:`. `null` significa **desconocido**, nunca falso ni cero.

| Tool | Para qué |
|---|---|
| `list_categories` | Categorías, campos filtrables (tipo, unidad canónica, operadores, valores) y perfiles de actividad |
| `list_brands` | Marcas del catálogo con su número de productos publicados y las categorías que cubren; filtrable por categoría |
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
- La autenticación HTTP es por token estático o acceso público; OAuth 2.1 de MCP (necesario para restringir
  el acceso a ChatGPT) está en el backlog.
- Rate limiting en memoria por proceso; con varias réplicas usa el proxy inverso o un límite compartido.
- La compatibilidad bota/crampón nunca sustituye la comprobación física; ninguna herramienta evalúa la
  seguridad de una ruta.
