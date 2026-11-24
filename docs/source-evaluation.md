# Evaluación de fuentes y datos de referencia (revisión 2026-11-24)

Este documento resume la investigación que respalda `data/sources.yaml`, `data/activity_profiles.yaml`
y `data/compatibility_rules.yaml`. Las URL de cada afirmación están en los propios YAML (`evidence`,
`references`).

## 1. Conclusión

- **Ninguna fuente real está habilitada.** No se ha encontrado una licencia o permiso que autorice
  reutilizar comercialmente catálogos de fabricantes. La vía recomendada sigue siendo pedir un feed
  o dataset a fabricantes o distribuidores e importarlo con `import_products`.
- **El registro está preparado para activar fuentes en cuanto haya permiso.** Contiene 27 marcas
  (25 con dominio verificado), 14 fuentes con configuración de sitemap, filtros de URL y reglas de
  categoría, y 6 rutas de datos candidatas sin adaptador.
- **El permiso técnico de `robots.txt` no concede derechos de reutilización.** Se registra solo como
  hecho técnico.

## 2. Marcas y dominios

La verificación consistió en descargar el `robots.txt` del dominio o encontrar la página corporativa
con los datos legales de la empresa.

| Estado | Marcas |
|---|---|
| Dominio verificado (25) | La Sportiva, Scarpa, Salewa, Bestard (bestard.com), Lowa, Petzl, Grivel, Black Diamond, Arc'teryx, Altus (wearealtus.com), Rab (rab.equipment), Mammut, The North Face, Patagonia, Montane, Ortovox, Osprey, Deuter, Gregory (gregorypacks.com redirige a gregory.com), Ferrino, NNormal, Salomon, Hoka, Leki, RUMM (rummclub.com, Rumm Culture SL, Murcia) |
| Pendiente (2) | Asolo (el sitio no respondió), CAMP (el sitio solo carga con JavaScript) |

### Hallazgos técnicos relevantes en `robots.txt`

- **Shopify (Scarpa, Grivel, Black Diamond, Montane):** las páginas `/products/` están permitidas y
  hay un sitemap único. Scarpa y Montane incluyen comentarios dirigidos a agentes de IA que piden
  instalar «skills». Se tratan como contenido externo no confiable y se ignoran.
- **Grivel** permite explícitamente a varios rastreadores de IA (`ClaudeBot`, `anthropic-ai`,
  `CCBot`, `PerplexityBot`…).
- **Arc'teryx** aplica a los bots de IA las mismas reglas que al resto, con `Allow: /`.
- **Patagonia** bloquea `Google-Extended` y `GoogleOther` en todo el sitio, lo que se interpreta como
  una señal contraria a la reutilización por IA.
- **Ferrino** pone `Crawl-delay: 10` a los bots de IA y **NNormal** `Crawl-delay: 5` a todos.
- **Lowa y Salomon (Magento)** bloquean las URL internas `/catalog/product/view/`. Solo deben
  descargarse las URL públicas que aparecen en los sitemaps.
- **Mammut** bloquea las rutas de filtros técnicos (peso, columna de agua, aislamiento…).
- **Rab, Ferrino y Salomon** no publican la directiva `Sitemap`.
- **RUMM** (WordPress/WooCommerce) declara un sitemap alojado en un dominio de terceros
  (`pixelwisestudio.me`), probablemente de la agencia web; el fetcher lo bloquearía por estar fuera de
  la allowlist. Es una marca de ropa y accesorios de trail, sin calzado.

## 3. Condiciones de uso revisadas

| Fuente | Hallazgo | `access_status` |
|---|---|---|
| Petzl | Su aviso legal limita el sitio y su contenido a uso privado, personal y no comercial, y prohíbe reproducirlo o distribuirlo sin consentimiento escrito. | `restricted` |
| La Sportiva | La entidad estadounidense solo concede un privilegio de uso personal y limitado. No se encontraron términos de uso para el sitio europeo. | `unverified` |
| Scarpa | Solo se encontraron condiciones de venta (GTCS). La política de uso del sitio no se ha revisado. | `unverified` |
| Resto de fuentes | Solo se ha revisado `robots.txt`. | `unverified` |

## 4. Rutas de datos alternativas (`candidate_sources`)

| Ruta | Qué aporta | Estado |
|---|---|---|
| Bergfreunde ES (Awin) | Feeds diarios de producto (hasta unos 270.000 productos), precios y disponibilidad | `requires_agreement`: el feed se licencia a afiliados con enlaces de seguimiento, y la afiliación está fuera del alcance del proyecto |
| Deporvillage ES (Awin) | Precios y disponibilidad | `requires_agreement`: sus términos exigen que las ofertas lleguen vía Awin y no se extraigan de la web |
| Barrabés | Precios | `to_evaluate`: no se encontró programa ni feed público |
| Snowleader | Precios | `to_evaluate`: campaña CPC en Cuelinks, sin condiciones de feed |
| GS1 GDSN | GTIN y datos maestros | `requires_agreement` con cada marca |
| EOG/BSI SDEX | Datos de sostenibilidad | `out_of_scope` en el MVP |

## 5. Reglas de compatibilidad (v2)

`boot-crampon-binding-type@v2` añade tres cosas, todas parametrizadas en el YAML:

- **Rebordes necesarios por tipo de fijación:**
  - De correas: no necesita rebordes.
  - Semiautomática: reborde de talón.
  - Automática: rebordes de puntera y talón.
- **Matriz B/C** (esquema de Brian Hall, recogido por el BMC y el comercio especializado):
  - B0: ningún crampón.
  - B1: solo C1.
  - B2: C1 o C2.
  - B3: C1, C2 o C3.

  Una combinación fuera de la matriz cuenta como incompatibilidad documentada.
- **Aviso para tallas grandes** (EU 46 o más): las botas grandes flexan más que su clasificación
  nominal, y algunos modelos necesitan una barra de unión larga.

La v1 queda como `retired`. Los alias del taxonomía reconocen ahora los nombres comerciales de las
fijaciones:

| Tipo genérico | Grivel | Petzl | Black Diamond |
|---|---|---|---|
| De correas (`strap_on`) | New-Classic | FLEXLOCK | Strap |
| Semiautomática (`semi_automatic`) | New-Matic | LEVERLOCK UNIVERSEL | Clip |
| Automática (`automatic`) | Cramp-O-Matic | LEVERLOCK FIL | Pro |

## 6. Perfiles de actividad (v2 y nuevo)

- **`winter_mountaineering` v2.** Las preferencias proceden de listas de material de cursos
  (Plas y Brenin, FEMECV) y de Petzl:
  - Botas B2/B3.
  - Crampón de 12 puntas con antizuecos.
  - Piolet de marcha o alpinismo de 50 a 60 cm.
  - Mochila de 30 a 50 L.
  - Normas declaradas: EN 893 (crampones), EN 13089 (piolets) y EN 12492 (cascos).

  Añade notas sobre ARVA, pala y sonda (FAM), sobre formación y sobre ajustar el crampón a la bota.
- **`summer_alpine_mountaineering` v1 (nuevo).** Glaciar y vías normales en verano, con base en la
  lista de material básico de Petzl.
- **`winter_hiking` v2.** B1 como mínimo para crampones, crampón C1 de correas para tramos helados
  cortos, y advertencia de que los microcrampones no son EPI ni sirven en pendiente (BMC).
- **`trail_running` v2.** Umbrales de los reglamentos de las series UTMB:
  - Chaqueta: impermeabilidad recomendada de al menos 10.000 Schmerber, RET recomendado por debajo
    de 13 y capucha.
  - Capa cálida: al menos 180 g.
- **`hiking_trekking` v2.** Añade criterios para recorridos de varios días: caña media o alta y
  cinturón lumbar acolchado.

Todos los perfiles siguen como `pending_expert_review`. Las normas se comparan de forma tolerante
(`EN 12492:2012` equivale a `EN 12492`) y siempre se consideran declaradas por el fabricante.

## 7. Limitaciones de esta revisión

- **Lectura indirecta:** las páginas se leyeron con herramientas de búsqueda y lectura web, no desde
  el contenedor de ingesta. No se ha descargado ninguna ficha de producto.
- **Interpretación jurídica:** las condiciones legales se resumen tal como están publicadas; esto no
  es una opinión jurídica.
- **Caducidad:** los `robots.txt` cambian con frecuencia. Conviene revisarlos de nuevo antes de
  habilitar cualquier fuente.
