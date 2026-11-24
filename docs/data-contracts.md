# Contratos de datos

## Taxonomía (v1 en todas las categorías salvo `hiking_boot` v2)

Fuente única: `mountain_intelligence/catalog/taxonomy.py`. La herramienta `list_categories` expone la versión
viva (campos, tipo, unidad, operadores, valores permitidos). Unidades canónicas: `g`, `mm`, `l`, °C, `EUR`;
otras unidades declaradas (`m2pa_w` para RET, `g_m2_24h` para MVTR, `g_m2` para gramaje) no se convierten
entre sí.

| Categoría | Campos principales (critical = cambios a revisión) |
|---|---|
| `mountaineering_boot` | `weight_g`, `weight_basis`, `reference_size_eu`, `weight_per_boot_g` (derivado), `rigidity`, `boot_rating`*, `crampon_attachment_types`*, `toe_welt`*, `heel_welt`*, `waterproof`, `membrane`, `insulated` |
| `hiking_boot` (v2) | peso y base, `rigidity`, `boot_rating`*, `cuff_height`, `waterproof`, `membrane`, `crampon_attachment_types`* |
| `trail_running_shoe` | peso y base, `drop_mm`, `lug_depth_mm`, `terrain`, `waterproof` |
| `hardshell_jacket` | `weight_g`, `construction`, `waterproof_rating_mm` (+método), `breathability_ret` / `breathability_mvtr` (+protocolo), `ventilation`, `hood` |
| `midlayer` | `fabric_type`, `material`, `fabric_weight_gsm`, `hood`, `zip` |
| `insulated_jacket` | `insulation_type`, `fill_weight_g`, `fill_power` (+método), `hood` |
| `backpack` | `capacity_l`, `weight_g`, `back_sizes`, `max_load_g`, `ice_axe_attachments`, `rope_attachment`, `ski_carry`, `access`, `hip_belt`, `back_system` |
| `crampon` | `binding_types`*, `crampon_rating`*, `points`, `front_points`, `material`, peso y base, `weight_per_pair_g` (derivado), `boot_size_min_eu`*/`boot_size_max_eu`*, `anti_balling_plates`, `declared_standards`* |
| `ice_axe` | `intended_use`, `length_mm`, `weight_g`, `shaft`, `en13089_shaft_type`*, `en13089_head_type`*, `declared_standards`* |
| `climbing_helmet` | `weight_g`, `head_circumference_min/max_mm`, `construction`, `declared_standards`* |

| `uncategorized` | Sin esquema técnico (`technical: false`). Respaldo para productos que ninguna regla clasifica: solo metadatos del fabricante y atributos declarados; excluido de recomendaciones, comparaciones y compatibilidad |

Reglas: `null` = desconocido; no se infieren valores del marketing (`waterproof` no implica columna de agua);
RET y MVTR nunca se comparan; las normas son «declaradas por el fabricante», no certificaciones del sistema.
Comprobaciones de coherencia: welts frente a tipos de crampón declarados, rangos mín/máx, `fill_power` solo
para plumón.

## Contrato de extracción (`ingestion/contracts.py`)

`ExtractedProduct`: `external_id`, `canonical_url`, `brand`, `name`, `generation`, `category`, `mpn`,
`aliases`, `summary` (breve, neutro), `specifications` (valores crudos), `units_from_field_names`,
`locators` (`campo → {locator, excerpt ≤280, unit}`), `variants` (`sku`, `mpn`, `gtin` con dígito de control
GS1, talla, color, `external_id`, `options` {nombre de opción → valor}, `image_url`, `spec_overrides`) y
`offers` (`retailer`, `currency` ISO, `price > 0`, `availability`, `variant_sku`; hasta 500, una por variante).

Metadatos del catálogo del fabricante (se conservan tal cual, nunca son especificaciones validadas):
`category_basis` (`explicit | rule | default | fallback`), `source_category`, `product_type`, `tags`,
`collections`, `external_ids` (p. ej. `shopify_product_id`, `shopify_handle`), `images` (`url` http(s), `alt`,
`position`, `variant_skus`) y `source_attributes` (`name`, `value`, `unit`, `locator`: características tal y
como las declara la fuente). En un producto `uncategorized`, las `specifications` crudas que traiga el
adaptador se convierten en `source_attributes` (nada se normaliza sin esquema).

En `Product` estos datos se guardan en columnas homónimas; `external_ids` se agrupa por fuente
(`{"altus-website": {...}}`) y cada `source_attribute` lleva la fuente que lo declaró. Un valor no vacío
de la última observación sustituye al anterior; un valor vacío nunca borra lo aportado por otra observación.

## Formato de importación estructurada

JSON (`format: "mountain-intelligence/products"`, `version: 1`, `products: [ExtractedProduct…]`). Las claves
llevan la unidad en el nombre (`weight_g`), por lo que los números sin unidad se interpretan en esa unidad;
un texto con unidad explícita (`"1,6 kg"`) se convierte. CSV: una fila por producto con columnas
`external_id, brand, name, category` y `spec.<campo>` (listas separadas por `;`). Números con separadores
ambiguos (`1.250`) solo se aceptan si una única interpretación cae en el rango plausible del campo; si no,
van a revisión.

## Normalización y evidencia

Cada valor escrito genera una `SpecificationEvidence` con valor original, valor normalizado, unidades,
fuente, URL, localizador, fragmento breve, fecha y estado (`source_verified`, `manual_import`,
`review_approved`, `pending_review`, `superseded`, `rejected`, `synthetic_fixture`). El historial permite
saber por qué cambió un dato.

## Identidad e idempotencia

- `SourceRecord(source, external_id)` único; `external_id` = URL canónica descubierta (sin parámetros,
  fragmento, prefijo de idioma ni ruta de colección; ver `ingestion/discovery.py`). Si la canonicalización
  cambia, los registros antiguos se re-identifican (`rekeyed`) conservando producto, ETag e historial.
  Se guarda el hash SHA-256 del contrato validado: mismo hash ⇒ `unchanged` sin escrituras de catálogo.
- Variantes: GTIN → SKU → id externo → talla+color dentro del producto. Colores y tallas de una misma ficha
  son variantes, no productos.
- Clasificación progresiva: una clasificación técnica también empareja con los productos `uncategorized` de la
  marca (se reclasifican, no se duplican); una extracción `uncategorized` empareja en cualquier categoría y
  nunca degrada un producto ya clasificado. Una reclasificación que chocaría con otro producto abre un conflicto.
- `Product.public_id` estable (slug marca+nombre+generación). Emparejamiento: registro previo → GTIN → marca+MPN
  → marca+categoría+nombre+generación exactos. Mismo nombre con generación desconocida en un lado ⇒ revisión
  (`ambiguous_match`). Nombre similar (trigramas > 0,6) ⇒ producto nuevo en revisión con posibles duplicados;
  nunca se fusiona automáticamente.

## Contrato MCP

Sobre común (`mcp_server/schemas.py`): `status`, `data`, `warnings`, `coverage{catalog_scope,
matched_products, data_coverage}`, `pagination{next_cursor,total}`, `meta{server, server_version,
catalog_scope, generated_at}`. `outputSchema` publicado por cada tool. Errores de validación ⇒ `isError` con
`validation_error: …`; errores internos ⇒ mensaje genérico del SDK sin traza.

Resúmenes de producto: `technical_classification`, `source_category`, `product_type`, `image_url` además de
`key_specs`/`coverage` (0 sin esquema técnico). `get_product` añade `category_basis`, `tags`, `collections`,
`external_ids`, `images`, `source_attributes`, variantes con `options`/`external_id`/`image_url`, ofertas con
`variant_sku` y `availability` (ofertas recientes por estado, agotadas incluidas). `search_products` acepta
`classification: any | technical | uncategorized`. `recommend_products` y `OwnedItem` solo aceptan categorías
técnicas.

## Registro de marcas y fuentes (`data/sources.yaml`)

- `brands[]`: `name`, `official_domain`, `domain_verified` (exige `verified_at`), `specialties`,
  `integration_status` se guardan en BD; `country`, `robots`, `sitemaps`, `evidence`, `notes` documentan la
  evaluación (no se persisten).
- `sources[]`: configuración del adaptador + `access_status` + `references` (se añaden a `access_notes`).
  `categories: []` = sin restricción (catálogo completo, incluido `uncategorized`); una lista restringe el
  alcance (lo demás es `out_of_scope`). Se validan las regex (`url_include`, `product_url_patterns`,
  `url_exclude`, `sitemap_include`, reglas), que las reglas apunten a categorías existentes y dentro de
  `categories`, los enteros (`batch_size`, `max_urls`, `max_sitemaps`, `batch_interval_minutes`) y que
  `capture_offers` en Shopify declare `currency`. `Source.sync_state` (cursor de lotes) lo gestiona la ingesta,
  nunca el YAML.
- `candidate_sources[]`: rutas de datos sin adaptador (feeds de distribuidores, pools sectoriales),
  validadas pero no persistidas. Nombres de marca y slugs de fuente duplicados se rechazan.

## Reglas de compatibilidad parametrizadas

`binding_type_match` lee de `parameters`: `boot_categories`, `welt_requirements` (tipo de fijación →
rebordes), `rating_matrix` (B → C admitidos; fuera de la matriz = incompatibilidad documentada) y
`large_boot_size_eu` (aviso de flexión en tallas grandes). Los parámetros se validan al cargar.
Las normas declaradas (`declared_standards`) se comparan de forma tolerante en los perfiles
(`EN 12492:2012` == `EN 12492`).
