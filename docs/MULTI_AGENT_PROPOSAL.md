# Multi-agent split — propuesta

> Daniel preguntó: *"hay que ir dividiendo las tareas en distintos AI agents
> verdad?"*. Acá está el análisis y la propuesta concreta para que decidas si
> seguir adelante o no.

## Estado actual

El bot vive en un solo archivo `main.py` de **~4,800 líneas**. Funciona, pero
el system prompt monolítico (`SUMIN_SYSTEM`, ~700 líneas) le pide a un solo
modelo que sea simultáneamente:

- vendedor cordial,
- intérprete de mm→fracciones,
- especialista en MIG/TIG/oxicorte/discos,
- experto en equivalencias de marca (A.A./W.A./SafeCut/etc.),
- detector de "no manejo" vs "consultá catálogo",
- generador de cotizaciones formales en Zoho.

Ese prompt es la mayor fuente de fricción cuando agregamos reglas: cada nueva
regla compite con las existentes y a veces el modelo "olvida" reglas que ya
estaban (ej: el flow MIG después del handoff que volvía a pedir foto, o
boquilla Victor #1 que se iba a soldadura en lugar de oxicorte).

## Problema concreto que resolveríamos

Los bugs últimos son del tipo "una regla pisa otra":

| Bug | Causa raíz |
|---|---|
| 7018 1/8 → E-Strike (debería ser ESPECIAL) | matcher LLM elige por similitud sin saber la línea por defecto |
| Boquilla Victor #1 → boquilla para soldar | matcher no conoce el default oxicorte sin sufijo |
| MIG post-handoff → vuelve a pedir foto | regla "siempre pedir foto" del SUMIN_SYSTEM gana sobre el contexto de "ya atendido" |
| "vidrio #12" → boquilla TIG #12 | matcher comparte el "#12" entre productos no relacionados |
| "son boquillas de corte" como nombre cliente | filtro débil entre aclaración técnica y nombre |

Patrón: **un solo modelo intentando aplicar reglas heterogéneas a la vez**.

## Propuesta — 5 agentes especializados

```
                       ┌─────────────────┐
        WA webhook ───▶│  ROUTER agent   │ ← clasifica el intent
                       └────────┬────────┘
                                │
        ┌──────────────┬────────┼────────┬──────────────┐
        ▼              ▼        ▼        ▼              ▼
  ┌──────────┐   ┌──────────┐ ┌────┐  ┌────────┐   ┌──────────┐
  │  SALES   │   │  QUOTE   │ │MIG │  │CONFIRM │   │  CUSTOMER│
  │  agent   │   │  agent   │ │HAND│  │ agent  │   │  NAME    │
  │          │   │          │ │OFF │  │        │   │  agent   │
  └──────────┘   └──────────┘ └────┘  └────────┘   └──────────┘
```

### 1. **Router agent** (Haiku, instant, ~$0.0001/llamada)

**Input**: mensaje del cliente + último estado de la conv.

**Output**: `{intent: "sales|quote|mig|confirm|name|cancel|chat", confidence: 0..1}`

Reemplaza al spaghetti de `if`s en `orchestrate()` que hoy decide a dónde
rutear (`detect_quote_request`, `_explicit_new_quote_request`,
`pending_confirmation`, etc.). El router es 1 prompt corto, 1 LLM call,
salida JSON pura. Si confidence < 0.7 hace ask para clarificar, no asume.

### 2. **Sales agent** (lo que hoy es `claude_respond`)

**Cuando**: intent=sales (preguntas generales, info, ubicación, EPP no
específico).

**Input**: mensaje + historia + ciudad + Zoho stock context.

**Output**: respuesta natural al cliente.

**Reglas**: solo las de tono, ciudad, formas de pago. Ya NO incluye reglas
de productos / MIG / equivalencias — esas se delegan al Quote agent.

### 3. **Quote agent** (lo que hoy es `quote_agent` + `extract_items_for_quote`)

**Cuando**: intent=quote.

Sub-componentes que YA existen y se quedan así:
- `extract_items` (Sonnet — extracción JSON estricta).
- `match_product_to_catalog` (Haiku con prefilter de Zoho).
- `_apply_corrections` (Haiku para parsing de respuestas).

**El cambio**: el system prompt de cada uno se vuelve **mucho más corto y
focalizado**. Ahora `match_product_to_catalog` recibe un prompt de 200 líneas
con todas las reglas de tono — no las necesita; solo necesita reglas de
matching.

### 4. **MIG handoff agent** (estado finite-state, no LLM)

**Cuando**: intent=mig (foto de consumible MIG).

Es 100% determinístico — no necesita LLM. Detecta foto, agrega a queue de
handoffs, y cuando Daniel responde número 1-6 dispatcha al cliente. Hoy ya
funciona así (v22). Solo lo dibujamos como agente separado para claridad
arquitectónica.

### 5. **Confirm agent** + 6. **Customer name agent**

Ya existen como funciones (`confirmation_agent`, `_parse_quote_name_response_open`).
Las dejamos como están — son agentes especializados con prompts cortos.

## Beneficios

| Beneficio | Antes (monolítico) | Después (split) |
|---|---|---|
| Tiempo p/agregar regla nueva | Editar SUMIN_SYSTEM (700 líneas), riesgo de pisar otra | Editar 1 prompt focalizado |
| Costo por mensaje | ~1500 tokens system + 500 user → Sonnet | Router Haiku (300 tokens) + agente especializado (300-800) → ~30% menos costo |
| Latencia | 1 call Sonnet ~2-4s | 1 Haiku router (~400ms) + 1 agente especializado (~1-2s). En paralelo si aplica. |
| Testabilidad | Caja negra; difícil aislar qué regla rompió | Cada agente con sus tests propios |
| Debug | Logs sueltos | Cada agente loguea su decisión + confidence → trazabilidad clara |

## Costos

| Item | Estimado |
|---|---|
| Refactor inicial | 2-3 días de trabajo |
| Riesgo de regresión | Medio — voy a tener que mover el SUMIN_SYSTEM en pedazos. La suite de backtests (`tests/run_backtests.py`) cubre lo crítico. |
| Costo de tokens (mensual) | Probablemente baja un 20-30% por usar Haiku en el router |
| Latencia por mensaje | Igual o mejor (Haiku router es muy rápido) |

## Riesgos

1. **El router se equivoca** y manda al agente incorrecto → mensaje raro al
   cliente. Mitigación: confidence threshold + fallback al sales agent
   (que siempre da una respuesta razonable).
2. **Pierdo contexto entre agentes**. Mitigación: pasar `conv_meta` (ciudad,
   pending_quote, mig_attended, etc.) entre agentes — ya lo tenemos.
3. **Más superficie de bugs nuevos**. Mitigación: extender la suite de
   backtests antes de migrar cada agente.

## Plan de migración (si decidís proceder)

**Fase 1 (1 día)** — Solo router, sin tocar el resto. Si el router dice
`intent=quote`, llamamos al `quote_agent` actual; si dice `sales`, a
`claude_respond` actual. Cero cambios en los prompts. Solo desacoplo el
ruteo.

**Fase 2 (1 día)** — Recortar `SUMIN_SYSTEM` para Sales agent: removerle
todas las reglas de productos / MIG / discos / equivalencias. Solo queda
tono, ciudad, EPP genérico.

**Fase 3 (1 día)** — Crear prompt focalizado para `match_product_to_catalog`:
solo reglas de matching (A.A./W.A./vidrio/boquilla Victor/7018 ESPECIAL/
INCONEL→NiCrFe-3). Sin tono ni ciudad.

**Fase 4 (medio día)** — Tests. Agregar caso por caso al `run_backtests.py`.

## Recomendación

**Hacerlo cuando agreguemos la siguiente regla grande** (ej: "memoria de
cliente preferido" o "promociones por mes"). Hacerlo ahora *sin* una regla
nueva es trabajo puro de plomería sin payoff inmediato.

Si querés que avance en la Fase 1 (solo el router, sin tocar prompts),
respondé "fase 1" y arranco. Si preferís quedarnos con el monolito y solo
agregar reglas con la suite de backtests como red de seguridad, también es
una decisión válida — el monolito hoy funciona y el costo del refactor no
es trivial.
