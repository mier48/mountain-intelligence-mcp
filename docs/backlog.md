# Backlog priorizado

## P0 — Para pasar de MVP a uso real

1. **Fuentes autorizadas**: solicitar feeds/datasets o permiso escrito (Scarpa, La Sportiva, Petzl; después
   Ortovox y Arc'teryx). Importar vía `structured_file` o habilitar JSON-LD solo con `authorized/terms_permit`.
2. **Revisión experta de perfiles de actividad** (`winter_mountaineering`, `winter_hiking`, `hiking_trekking`,
   `trail_running`) y de la regla bota/crampón; publicar nuevas versiones con `reviewed_by/reviewed_at`.
3. **OAuth 2.1 para MCP** (servidor de autorización, `TokenVerifier` del SDK, metadatos RFC 9728) para clientes
   que solo aceptan conectores OAuth (p. ej. restringir el acceso de ChatGPT, hoy en modo público).
4. Verificar el despliegue con las imágenes oficiales (job `docker` de la CI) y fijar digests de imagen.

## P1 — Calidad del catálogo

0. Campo `seams_taped` en `hardshell_jacket` (exigido por reglamentos de carreras) y `back_length_cm` en
   mochilas; adaptador de feeds Awin si se firma un acuerdo de datos con un distribuidor.

5. Adaptadores específicos por fabricante (tablas técnicas HTML) cuando JSON-LD no cubra los campos.
6. Aserciones de compatibilidad a nivel de par desde listas de ajuste oficiales; reglas para otras relaciones
   (mochila/piolet, casco/linterna).
7. Feeds autorizados de distribuidores (Barrabés, Deporvillage, Snowleader, Bergfreunde) solo para ofertas.
8. Extracción asistida por LLM opcional (presupuesto, caché por hash, salida validada, nunca fuente de verdad).
9. Retención configurable de documentos originales cuando los derechos lo permitan (`store_raw_documents`).

## P2 — Herramientas y operación

10. Tools posteriores: `get_product_offers`, `get_price_history`, `get_brand_catalog`, `find_alternatives`,
    `evaluate_equipment`.
11. Rate limit compartido entre réplicas (Redis) y métricas Prometheus/OpenTelemetry (el SDK ya integra OTel).
12. Soporte de proxy de salida con allowlist para el fetcher.
13. Optimizar la búsqueda relajada por trigramas y las consultas N+1 de resúmenes si las mediciones reales
    lo requieren.
14. Ampliar sinónimos es/en y alias de enumerados con datos reales; nuevas categorías (bastones, esquí de
    montaña, arneses).
