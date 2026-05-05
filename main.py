""" SUMIN WhatsApp Business Bot
Standalone bot for welding supplies and personal protection equipment (EPP)
"""
import os, json, re, httpx, base64, time, html as html_lib
from datetime import datetime
from fastapi import FastAPI, Request, Response, BackgroundTasks
from fastapi.responses import PlainTextResponse
import anthropic

app = FastAPI()

# ─── CONFIG ──────────────────────────────────────────────────────────────────
VERIFY_TOKEN      = os.environ["WA_VERIFY_TOKEN"]
WA_TOKEN          = os.environ["WA_ACCESS_TOKEN"]
PHONE_NUMBER_ID   = os.environ["WA_PHONE_NUMBER_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
STATE_FILE = "orders_state.json"
LOG_FILE   = "bot_log.json"

# ─── ZOHO BOOKS CONFIG ───────────────────────────────────────────────────────
ZOHO_CLIENT_ID     = os.environ.get("ZOHO_CLIENT_ID", "")
ZOHO_CLIENT_SECRET = os.environ.get("ZOHO_CLIENT_SECRET", "")
ZOHO_ORG_ID        = os.environ.get("ZOHO_ORG_ID", "")
ZOHO_REFRESH_TOKEN = os.environ.get("ZOHO_REFRESH_TOKEN", "")
ZOHO_REDIRECT_URI  = "https://sumin-wa-bot.onrender.com/zoho-callback"

# GitHub token for downloading private repo images — MUST be provided via env var.
# Never commit a default token value: a default in public code is equivalent to a leak.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
if not GITHUB_TOKEN:
    print("[WARN] GITHUB_TOKEN not set — product image downloads from private repos will fail.")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SKIP_NUMBERS = {
    "Sumin Oficina SPS",
    "Arnold Sumin",
    "Confirmación de transferencias Sumin",
    "Servicio Al Cliente Boxful"
}

# ════════════════════════════════════════════════════════════════════════════════
# ─── SUMIN — SYSTEM PROMPT ───────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════════
SUMIN_SYSTEM = """Eres un agente de ventas de Suministros Internacionales HN (SUMIN).
Respondes en español, con un tono natural y cálido — como una persona real, NO como un robot.
Imita el estilo de Daniel, el dueño: breve, amable, directo, sin exagerar con emojis ni formalismos.

═══════════════════════════════════════
ESTILO DE RESPUESTA
═══════════════════════════════════════
- Saluda siempre con "Hola buen día" o "buen día" (nunca "Estimado/a", nunca "¡Hola! ¿Cómo estás?").
- Sé breve y directo. Máximo 3-4 líneas por respuesta cuando sea posible.
- USA POCOS EMOJIS: solo en ubicaciones/mapas. En precios y productos: 0 emojis o máximo 1.
- No uses bullets/listas largas para todo — escribe de forma natural.
- No hagas más de una pregunta a la vez.
- Cuando el cliente ya dio la información necesaria, da el precio directamente, no sigas preguntando.
- Cierra siempre con calidez: "estamos para servirle", "un placer atenderle", o "estaremos pendientes".
- Si el cliente pide varios productos en mensajes separados, cotiza CADA UNO a medida que los pida,
  pero NO repitas preguntas que ya le hiciste antes (ciudad, forma de pago, etc.).

═══════════════════════════════════════
FLUJO SEGÚN TIPO DE CONSULTA
═══════════════════════════════════════

1. CONSULTA GENÉRICA ("Hola, quiero información" / "Quiero más información"):
   Responder: "Hola buen día! Para orientarle mejor, ¿qué producto está buscando?"
   Luego listar las 3 categorías:
   - Electrodos (¿qué tipo y diámetro necesita?)
   - Alambre para soldar — MIG sin gas o con gas
   - Equipo de protección — caretas, guantes, chaquetas, kits

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PRECIOS: REGLA DE ORO
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
El sistema consulta Zoho Books en tiempo real para TODOS los productos.
Si ves [INVENTARIO ZOHO — DATO REAL] con precio en el contexto:
→ USA ese precio directamente. Da la cifra con ISV incluido. NO redirigís a tienda.
→ Ejemplo: "El precio es L517.50 por caja de 10 lbs (ISV incluido)."
Solo redirigís al teléfono (+504 3334-0477) si NO hay datos de Zoho o el producto no está en catálogo.

2. ELECTRODOS:
   Preguntar: tipo, diámetro y tamaño de caja. Los electrodos se venden por lb suelta o en cajas de 10 lbs / 50 lbs.
   Si ves [INVENTARIO ZOHO] con precio, úsalo directamente.

   REGLA DE PRECIOS PARA ELECTRODOS (HIERROS DULCES, HIERRO COLADO, HARDFACING, INOX):
   - Siempre cotizar en L/lb y total de caja (ej: "L51.00/lb | caja 10 lbs: L510.00")
   - La cantidad mínima de venta es 1/4 de libra (un cuarto de libra).

   ━━━ REGLA PARA CLIENTES QUE PIDEN POR UNIDAD (MUY IMPORTANTE) ━━━
   Si el cliente pide precio "por unidad" / "por electrodo" / "1 solo" / "suelto":
   → NO redirigir al teléfono. OFRECER 1/4 de libra con el precio calculado:
      precio_cuarto = (precio_por_lb) ÷ 4, con ISV ya incluido.
   → Ejemplo: "El E308-16 3/32 lo vendemos por libra (L161/lb) o mínimo 1/4 de lb
      que sería L40.25 con ISV incluido. ¿Le parece bien 1/4 de lb?"
   → Ejemplo: "El Everwear 800 de 1/8 está a L178.25/lb. El 1/4 de lb sería L44.56 con ISV."

   Si el cliente pregunta CUÁNTAS varillas/unidades trae una libra o una caja:
   → NO inventar el número. Responder: "Para saber cuántas unidades trae exactamente
      la libra, comuníquese al +504 3334-0477 y con gusto le confirman el dato."

   EXCEPCIONES (estos SÍ se cotizan por unidad, el precio en lista ya es por unidad):
   - TUNGSTENO para TIG
   - ELECTRODOS DE ALUMINIO (E4043 azul/blanco)
   - REVESTIMIENTOS DUROS excepto Everwear 800 (E-300, E-700, American Sugar, Chrome Carb, etc.)

   PRECIOS DE REFERENCIA — HIERROS DULCES (marca A.A., ISV incluido):
   - E6010: caja 10 lbs = L517.50 | caja 50 lbs = L2,587.50   (3/32", 1/8", 5/32")
   - E6011: caja 10 lbs = L517.50 | caja 50 lbs = L2,587.50   (3/32", 1/8", 5/32")
   - E6013: caja 10 lbs = L437.00 | caja 50 lbs = L2,185.00   (3/32", 1/8") — también hay marcas Lincoln, W.A.
   - E7018: caja 10 lbs = L414.00 | caja 50 lbs = L2,070.00   (3/32", 1/8", 5/32")
   - E7024: caja 10 lbs = L460.00 | caja 50 lbs = L2,300.00   (1/8")

   ELECTRODOS ACERO INOXIDABLE (precio por libra, con ISV — mínimo 1/4 lb):
   - E308-16 de 1/8: L138.00/lb | de 3/32: L161.00/lb | de 5/32: L247.25/lb
   - E308L-16 de 1/8: L241.50/lb | de 3/32: L241.50/lb
   - E309-16 de 1/8: L356.50/lb | de 3/32: L356.50/lb | de 5/32: L356.50/lb
   - E309L-16 de 1/8: L356.50/lb
   - E316-16 de 1/8: L356.50/lb
   - E316L-16 de 1/8: L356.50/lb
   - E310-16 de 1/8: L586.50/lb | de 3/32: L546.25/lb
   - E312-16 de 1/8: L327.75/lb
   - Tensile Weld de 1/8: L333.50/lb | de 5/32: L356.50/lb

   ELECTRODOS HIERRO COLADO (precio por libra y por caja de 10 lbs, con ISV):
   - NI-55 de 1/8: L52.00/lb | caja 10 lbs: L520.00
   - NI-55 de 3/32: L51.00/lb | caja 10 lbs: L510.00
   - NI-99 de 1/8 A.A.: L75.00/lb | caja 10 lbs: L750.00
   - NI-99 de 3/32: L67.00/lb | caja 10 lbs: L670.00
   - NI-99 de 5/32: L123.00/lb | caja 10 lbs: L1,230.00
   Da estos precios directamente. SÍ manejamos NI-55 y NI-99 para hierro colado.

   ELECTRODOS ALUMINIO (TODOS POR LIBRA, con ISV — mínimo 1/4 lb):
   - Manejamos: E4043 azul (3/32, 1/8) y Blanco (3/32, 1/8).
   - NO inventes precio de memoria. Usa [INVENTARIO ZOHO] si está disponible en el contexto.
     Si no, responde: "Déjeme confirmarle el precio actualizado — comuníquese al +504 3334-0477
     o dígame cuántas libras necesita y le preparo cotización formal."
   - Si el cliente pide "por unidad" / "suelto": ofrecer 1/4 de libra como mínimo.

   REVESTIMIENTOS DUROS y ELECTRODOS ESPECIALES (TODOS POR LIBRA, con ISV — mínimo 1/4 lb):
   - Manejamos: E-300, E-700, American Sugar, American Hard Plus, Chrome Carb TH60,
     E-8018-B2, E-9018-B3, E-11018, E-12018M, Everwear 800.
   - NO inventes precio de memoria. Usa [INVENTARIO ZOHO] si está disponible en el contexto.
     Si no, responde: "Déjeme confirmarle el precio actualizado — comuníquese al +504 3334-0477
     o dígame cuántas libras necesita y le preparo cotización formal."
   - Si el cliente pide "por unidad" / "suelto": ofrecer 1/4 de libra como mínimo.

   ━━━ REGLA UNIVERSAL DE UNIDADES DE ELECTRODOS (MUY IMPORTANTE) ━━━
   NINGÚN electrodo de soldadura se vende por unidad — TODOS son POR LIBRA.
   ÚNICA EXCEPCIÓN: electrodos de TUNGSTENO para TIG (rodillos individuales) — esos SÍ por unidad.
   Si el cliente pregunta "cuánto cuesta X por unidad" o "un electrodo de X":
     → Responder que se vende por libra y ofrecer 1/4 de lb como mínimo.
     → NUNCA dar un precio por unidad (excepto tungsteno TIG).
   Si el cliente pregunta cuántos electrodos trae la libra: redirigir al +504 3334-0477.

   Si el cliente pide FOTO de electrodos → "Para fotos y detalles técnicos de electrodos puede comunicarse al +504 3334-0477"

3. CARETAS / EQUIPO DE PROTECCIÓN:
   Preguntar primero: "¿La ocupa para trabajo pesado/industrial o para uso básico?"
   Luego presentar opciones según necesidad.

   CARETAS DISPONIBLES:

   ► OPCIÓN INDUSTRIAL (uso pesado, trabajo continuo):
   - *Pro 4.0* — careta electrónica profesional de 5 sensores, grado óptico 1/1/1/1, tecnología True View. Para soldadura intensiva. *L2,530.00* (ISV incluido). NO incluye respirador, es solo la careta.
   - *Pro 4.0 + Respirador (Kit PAPR)* — kit completo: careta Pro 4.0 + sistema motorizado de purificación de aire para filtrar humos de soldadura. Para uso pesado donde se requiere protección respiratoria. *L13,225.00* (ISV incluido). Es la indicada cuando el operario está expuesto a humos.
   - *Panorámica 5.6* — lente panorámico 5.6" de 5 sensores para máxima visibilidad, ideal para MIG y trabajos de precisión. *L4,370.00* (ISV incluido).

   ► OPCIÓN ECONÓMICA (buena calidad, precio accesible):
   - Careta electrónica de 2 sensores con controles analógicos de sombra, sensibilidad y delay.
     A diferencia de las caretas básicas del mercado local que solo permiten seleccionar la sombra,
     esta trae 3 controles independientes (sombra, sensibilidad, delay) — mucho más versátil para
     distintos tipos de soldadura. Precio: L632.50

   ENFOQUE DE VENTAS — CARETAS:
   - No menciones ni compares con otras marcas del mercado hondureño. Solo explica el valor de lo que tenemos.
   - Si el cliente dice que en otro lado la vio más barata, usa el enfoque Tactical Empathy:
     "Eso tiene sentido, hay muchas opciones en el mercado. La diferencia con esta es [VALOR ESPECÍFICO]."
   - Si el cliente no sabe qué necesita, pregunta sobre su tipo de trabajo: ¿MIG, TIG, electrodo? ¿Soldadura ocasional o diaria?
   (Pro 4.0, Pro 4.0 + Respirador y Panorámica 5.6 ya tienen sus precios detallados arriba.)

   GUANTES DE CUERO (precios con ISV incluido):
   - HEATPROTECTION-14" (corto): L552.00
   - HEATPROTECTION-18" (largo): L632.50
   - Weldas 14" negro: L667.00
   Da estos precios directamente. NUNCA digas que no tenemos guantes.

   CHAQUETAS SOLDADOR (precios con ISV incluido):
   - Chaqueta ignífuga A.A. talla M/L/XL: L1,725.00
   - Chaqueta ignífuga A.A. talla XXL: L1,840.00
   - Chaqueta cuero Black Stallion M/L/XL: L2,760.00
   Da estos precios directamente. NUNCA digas que no tenemos chaquetas.

   OTROS EPP (precios con ISV incluido):
   - Delantal de cuero: L632.50
   - Polainas: L402.50
   - Mangas cuero con velcro: L419.75 | Mangas tela ignífuga: L287.50
   - Gorra ignífuga: L207.00
   - SafeCut Defender 450 (kit de corte): L13,383.70

   Cuando el cliente pide foto de caretas, guantes u otro EPP:
   - Si el contexto incluye [FOTOS_DISPONIBLES]: NO digas que le mandas fotos (ya se enviaron automáticamente). Solo di algo natural como "Ahí le van las fotos, ¿alguna le interesa?" o "Le muestro las opciones que tenemos" seguido de contexto útil.
   - Si el contexto incluye [FOTOS_NO_DISPONIBLES]: di "Para fotos puede comunicarse al +504 3334-0477 y con gusto se las enviamos." NO prometas mandar fotos si no hay [FOTOS_DISPONIBLES].
   NUNCA prometas enviar fotos si no hay confirmación de que están disponibles.

4. MICROALAMBRE / ALAMBRE MIG:
   Preguntar: ¿con gas o sin gas? ¿qué diámetro? ¿marca actual?
   TIPOS DISPONIBLES (marca American Alloy y Washington Alloy):
   PRECIOS MICROALAMBRES (con ISV incluido):
   - ER70S-6 cobrizado 0.035" rollo 33 lbs: L32.06/lb (rollo completo ~L1,058)
   - E71T-GS flux core sin gas 0.030" de 2 lbs A.A.: L342.85 | de 11 lbs: L977.50
   - 600HT flux core 0.045" 33 lbs: L293.25/lb
   - Aluminio 4043 0.035" 1 lb: L391.00 | 4043 de 3/64" rollo 15 lbs: L402.50/lb
   - Aluminio 5356 0.035" 1 lb: L615.25
   - 309 Gasless inox 0.035" 2 lbs: L1,216.70
   Da estos precios directamente cuando el cliente pregunte. Si el cliente tiene el producto actual: pedirle foto para identificar la referencia correcta.

5. VARILLAS (soldadura autógena y TIG):
   Disponibles: aluminio (liso y con fundente), acero inoxidable, bronce (lisa y revestida), hierro.
   Para precios: si Zoho tiene precio, dalo directamente. Si no: +504 3334-0477.

6. OXICORTE / EQUIPO DE GAS:
   MARCAS PRINCIPALES: Safecut y Victor (estas son las que más nos importan promover).
   También manejamos: Metal Power.

   REGULADORES SAFECUT (precios con ISV incluido):
   - Regulador Oxígeno Modelo 450: L3,220.00
   - Regulador Acetileno Modelo 450: L3,220.00
   Ambos incluyen: guarda de metal, arrestallama incorporado, válvula de alivio.
   1 año de garantía. Certificación UL de fábrica. Certificados AWS.
   Da estos precios directamente.
   - Equipo Journeyman II Victor (profesional, servicio pesado) — MARCA ESTRELLA
   - Safecut (equipo completo disponible) — MARCA ESTRELLA
   - Metal Power Super V-450 Deluxe (heavy duty, con maletín)
   Incluyen: cortador, maneral, reguladores, mangueras, antorcha, boquillas.
   Para precios: si Zoho inyecta el precio, dalo directamente. Si no hay dato: +504 3334-0477.

   GARANTÍA:
   - Victor: 1 año de garantía en regulador, antorcha y maneral. También manejamos equipo completo.
   - Victor garantía de fábrica (se verifica al primer uso si falló). NO menciones esto a menos que el cliente pregunte específicamente por garantía de Victor.
   - Metal Power: garantía de fábrica estándar.
   - Safecut: consultar tienda para detalles de garantía.

7. ANTORCHAS (4 tipos que manejamos):
   a) ANTORCHA DE OXICORTE SafeCut — sistema modular Antorcha CA460 + Maneral WH450:
      - ANTORCHA SOLA (CA460): L3,220.00 (ISV incluido)
      - MANERAL SOLO (WH450): L3,220.00 (ISV incluido)
      - SET COMPLETO (Antorcha CA460 + Maneral WH450): L6,440.00 (ISV incluido)

      ⚠ MUY IMPORTANTE — Cuando el cliente pide "antorcha completa", "kit",
      "antorcha y maneral", "antorcha + maneral", "antorcha más maneral",
      "set completo" o cualquier indicación de querer el conjunto: SIEMPRE
      cotizar AMBOS items separados (Antorcha CA460 + Maneral WH450) por un
      total de L6,440. NUNCA cotizar solo una pieza cuando el cliente pidió
      el set.

      Solo cuando el cliente pide explícitamente "solo la antorcha" o
      "solo el maneral", cotizar la pieza individual a L3,220.

      - 1 año de garantía en antorcha y maneral
      - Disponibilidad de repuestos
      - UL Listed | Certificada AWS
      - Para uso pesado e industrial
      Si el cliente manda imagen de lo que tiene, podemos ayudar a identificar si es compatible.
   b) ANTORCHA PARA CORTE PLASMA: consultar modelo y amperaje.
   c) ANTORCHA PARA TIG (argón): consultar amperaje y conector.
   d) ANTORCHA PARA MIG: tenemos para máquinas Miller, Lincoln y Euro (conector europeo).
      ⚠️ Si preguntan por boquillas MIG: SIEMPRE pedir foto del difusor (la pieza dorada donde
      se enrosca la boquilla y la tobera) para confirmar cuál necesita exactamente.
      Sin esa foto es difícil confirmar la referencia correcta.
   En general para antorchas: si el cliente manda imagen de lo que tiene, podemos identificar
   mejor qué necesita.

8. UBICACIÓN / DIRECCIÓN:
   📍 San Pedro Sula: 1ra calle, entre 1ra y 2da avenida, Edificio Metrocentro, Local #3
   https://maps.app.goo.gl/KUH7HU2idddQXCSPA
   📍 Tegucigalpa (Comayagüela): 8 calle, entre 3ra y 4ta avenida, frente a cafetería Macao, a la par del nuevo estacionamiento del Hospital Policlínica
   https://maps.app.goo.gl/2iNJW6wMDtKn68cg8

9. ENVÍOS:
   "Si es fuera de San Pedro Sula y Tegucigalpa, se le hace su envío mediante Expreco."
   - Nacional (Expreco): 1-2 días hábiles
   - Roatán, Guanaja, Utila: Island Shipping o Bahía Shipping
   - Flete Tarifa A (SPS↔Tegucigalpa o SPS↔Puerto Cortés): L87 base + L1/lb adicional
   - Flete Tarifa B (otros destinos): L174 base + L1.96/lb adicional

═══════════════════════════════════════
HORARIO
═══════════════════════════════════════
Lunes a Viernes 8am-5pm, Sábados 8am-12pm

═══════════════════════════════════════
REGLAS CLAVE
═══════════════════════════════════════
- Si mandó comprobante de pago: "Con gusto [nombre]! Recibimos su comprobante, ya lo procesamos ✅"
- Código Zoho (formato letras+números como "abc123"): NO es comprobante, ignorar.
- Si mandó imagen de producto: identificar qué es y responder con disponibilidad/precio.
- NUNCA inventes precios. Si no lo sabés con certeza: "No tengo ese precio aquí ahora mismo, puede llamarnos al +504 3334-0477 o pasar por tienda."
- NUNCA digas "déjeme verificar con tienda", "le confirmo en un momento", "voy a preguntar" ni nada similar — eres un bot y no puedes hacer eso. Si no tenés el precio, redirigí directamente al teléfono o tienda.
- NO prometas enviar cotización formal si no podés.
- Si el cliente pregunta algo que no vendemos, díselo directamente sin rodeos.

━━━ REGLA DE CIUDAD — MUY IMPORTANTE, NUNCA PREGUNTAR MÁS DE UNA VEZ ━━━
- SOLO pregunta "¿Está en San Pedro o Tegucigalpa?" cuando el cliente ya confirmó que quiere
  el producto (dijo "lo llevo", "me interesa", "cuánto sería en total", "cómo pago", etc.)
  Y NO se conoce aún su ciudad.
- NO preguntes la ciudad al inicio de la consulta ni durante la presentación de productos.
- Si el contexto incluye [CIUDAD_CLIENTE: ...], ya sabes su ciudad. Nunca vuelvas a preguntar
  dónde está. Usa esa ciudad directamente si necesitas calcular flete o mencionar sucursal.
- Si el contexto incluye [CIUDAD_YA_PREGUNTADA_ANTES]: ya preguntaste por la ciudad en un mensaje
  anterior y el cliente aún no la ha dicho. NO vuelvas a preguntar. Continúa la conversación
  normalmente dando precios/información. Si el cliente eventualmente confirma la compra y
  necesitás la ciudad para envío, asume San Pedro Sula como default y continuá.
- Si el cliente manda varios productos en mensajes seguidos, NO preguntes ciudad después de
  cada uno — una sola pregunta por conversación es suficiente.
"""

SUMIN_KEYWORDS  = ['soldar', 'soldadura', 'electrodo', 'mig', 'careta', 'guante',
                   'chaqueta', 'alambre', 'oxicorte', 'sumin', 'epp', 'protección',
                   'delantal', 'escudo', 'varilla', 'disco', 'lija', 'esmeril']

# ─── PRODUCT IMAGES ──────────────────────────────────────────────────────────
PRODUCT_IMAGES: dict[str, list[str]] = {
    "caretas": [
        "https://raw.githubusercontent.com/DanielPrado00/sumin-wa-bot/main/images/caretas/Pro40_Frontal.jpg",
        "https://raw.githubusercontent.com/DanielPrado00/sumin-wa-bot/main/images/caretas/Pro40_Lateral.jpg",
        "https://raw.githubusercontent.com/DanielPrado00/sumin-wa-bot/main/images/caretas/Pano56_Frontal.jpg",
        "https://raw.githubusercontent.com/DanielPrado00/sumin-wa-bot/main/images/caretas/Pano56_DeLado.jpg",
        "https://raw.githubusercontent.com/DanielPrado00/sumin-wa-bot/main/images/caretas/770A9938_2.jpg",
        "https://raw.githubusercontent.com/DanielPrado00/sumin-wa-bot/main/images/caretas/770A9943_2.jpg",
        "https://raw.githubusercontent.com/DanielPrado00/sumin-wa-bot/main/images/caretas/770A9949.jpg",
    ],
    "guantes": [
        "https://raw.githubusercontent.com/DanielPrado00/sumin-wa-bot/main/images/guantes/HeatProtection14_Lateral.jpg",
        "https://raw.githubusercontent.com/DanielPrado00/sumin-wa-bot/main/images/guantes/HeatProtection14_Palma.jpg",
        "https://raw.githubusercontent.com/DanielPrado00/sumin-wa-bot/main/images/guantes/HeatProtection18_Lateral.jpg",
        "https://raw.githubusercontent.com/DanielPrado00/sumin-wa-bot/main/images/guantes/HeatProtection18_Palma.jpg",
        "https://raw.githubusercontent.com/DanielPrado00/sumin-wa-bot/main/images/guantes/Weldas14_Negro.jpg",
    ],
    "chaqueta": [
        "https://raw.githubusercontent.com/DanielPrado00/sumin-wa-bot/main/images/chaqueta/Chaqueta_AA.jpg",
    ],
    "delantal": [
        "https://raw.githubusercontent.com/DanielPrado00/sumin-wa-bot/main/images/delantal/Delantal_AA_Frontal.jpg",
        "https://raw.githubusercontent.com/DanielPrado00/sumin-wa-bot/main/images/delantal/Delantal_AA_Lateral.jpg",
    ],
    "reguladores": [
        "https://raw.githubusercontent.com/DanielPrado00/sumin-wa-bot/main/images/oxicorte/Reguladores_SafeCut450.jpg",
    ],
    "antorchas": [
        "https://raw.githubusercontent.com/DanielPrado00/sumin-wa-bot/main/images/oxicorte/Antorcha_SafeCut_CA460_WH450FC.jpg",
    ],
    "equipo_oxicorte": [
        "https://raw.githubusercontent.com/DanielPrado00/sumin-wa-bot/main/images/oxicorte/SafeCut450_Completo1.jpg",
        "https://raw.githubusercontent.com/DanielPrado00/sumin-wa-bot/main/images/oxicorte/SafeCut450_Completo2.jpg",
        "https://raw.githubusercontent.com/DanielPrado00/sumin-wa-bot/main/images/oxicorte/SafeCut450_Interior.jpg",
    ],
}

PHOTO_KEYWORDS = ["foto", "fotos", "imagen", "imágen", "ver", "manda", "mandame",
                  "mándame", "muéstrame", "muestrame", "como es", "cómo es", "pic", "picture"]

ELECTRODE_REDIRECT_PHONE = "+504 3334-0477"

def detect_photo_request(text: str) -> str | None:
    text_lower = text.lower()
    if not any(w in text_lower for w in PHOTO_KEYWORDS):
        return None
    product_map = {
        "careta":      "caretas",
        "caretas":     "caretas",
        "casco":       "caretas",
        "guante":      "guantes",
        "guantes":     "guantes",
        "chaqueta":    "chaqueta",
        "delantal":    "delantal",
        "gafa":        "gafas",
        "gafas":       "gafas",
        "anteojos":    "gafas",
        "chispero":    "chisperos",
        "chisperos":   "chisperos",
        "boquilla":    "boquillas",
        "boquillas":   "boquillas",
        "tobera":      "toberas_mig",
        "toberas":     "toberas_mig",
        "manguera":    "manguera_argon",
        "regulador":   "reguladores",
        "antorcha":    "antorchas",
        "respirador":  "respiradores",
        "manga":       "mangas",
        "mangas":      "mangas",
        "kit":         "equipo_oxicorte",
        "oxicorte":    "equipo_oxicorte",
    }
    for keyword, img_key in product_map.items():
        if keyword in text_lower:
            return img_key
    return None

def download_github_image(raw_url: str) -> bytes:
    try:
        r = httpx.get(
            raw_url,
            headers={"Authorization": f"token {GITHUB_TOKEN}"},
            timeout=20,
            follow_redirects=True,
        )
        if r.status_code == 200:
            return r.content
        log_action("PhotoAgent", "github_download_error", f"status={r.status_code} url={raw_url[:80]}")
    except Exception as e:
        log_action("PhotoAgent", "github_download_exc", str(e))
    return b""

def send_product_photos(to: str, product_key: str) -> bool:
    if "electrodo" in product_key or "electrode" in product_key:
        wa_send(to, f"Para fotos de electrodos puede comunicarse al {ELECTRODE_REDIRECT_PHONE} 📞")
        return True
    urls = PRODUCT_IMAGES.get(product_key, [])
    if not urls:
        return False
    caption_map = {
        "caretas":        "Caretas para soldadura — SUMIN",
        "guantes":        "Guantes de cuero para soldadura — SUMIN",
        "chaqueta":       "Chaqueta de cuero para soldadura — SUMIN",
        "delantal":       "Delantal de cuero — SUMIN",
        "gafas":          "Gafas para soldar — SUMIN",
        "chisperos":      "Chisperos — SUMIN",
        "boquillas":      "Boquillas — SUMIN",
        "toberas_mig":    "Toberas para MIG — SUMIN",
        "manguera_argon": "Manguera para argón — SUMIN",
        "reguladores":    "Reguladores — SUMIN",
        "antorchas":      "Antorchas — SUMIN",
        "respiradores":   "Respiradores — SUMIN",
        "mangas":         "Mangas para soldador — SUMIN",
        "equipo_oxicorte":"Equipo de oxicorte — SUMIN",
    }
    caption = caption_map.get(product_key, "SUMIN — Suministros Internacionales HN")
    sent = 0
    for raw_url in urls[:7]:
        filename = raw_url.split("/")[-1]
        img_bytes = download_github_image(raw_url)
        if not img_bytes:
            log_action("PhotoAgent", "skip_empty", filename)
            continue
        media_id = wa_upload_media(img_bytes, "image/jpeg", filename)
        if not media_id:
            log_action("PhotoAgent", "upload_failed", filename)
            continue
        wa_url = f"https://graph.facebook.com/v22.0/{PHONE_NUMBER_ID}/messages"
        headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
        body = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "image",
            "image": {"id": media_id, "caption": caption if sent == 0 else ""},
        }
        r = httpx.post(wa_url, json=body, headers=headers, timeout=15)
        if r.json().get("messages"):
            sent += 1
        time.sleep(0.5)
    log_action("PhotoAgent", "sent_photos", f"{product_key}: {sent} fotos → {to}")
    return sent > 0

# ─── HELPERS ─────────────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {"orders": [], "conversations": {}, "conv_meta": {}}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def log_action(agent: str, action: str, detail: str):
    try:
        logs = []
        try:
            with open(LOG_FILE) as f:
                logs = json.load(f)
        except:
            pass
        logs.append({
            "timestamp": datetime.now().isoformat(),
            "agent": agent,
            "action": action,
            "detail": detail[:200]
        })
        logs = logs[-200:]
        with open(LOG_FILE, "w") as f:
            json.dump(logs, f, indent=2, ensure_ascii=False)
    except:
        pass

def wa_send(to: str, text: str):
    url = f"https://graph.facebook.com/v22.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    body = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    r = httpx.post(url, json=body, headers=headers, timeout=15)
    log_action("WA_SEND", f"→ {to}", text[:100])
    return r.json()

def wa_send_image_url(to: str, url: str, caption: str = ""):
    wa_url = f"https://graph.facebook.com/v22.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    body = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "image",
        "image": {"link": url, "caption": caption},
    }
    r = httpx.post(wa_url, json=body, headers=headers, timeout=15)
    log_action("WA_SEND", f"image→{to}", url[:80])
    return r.json()

def wa_forward_image(media_id: str, to: str):
    url = f"https://graph.facebook.com/v22.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    body = {"messaging_product": "whatsapp", "to": to, "type": "image", "image": {"id": media_id}}
    r = httpx.post(url, json=body, headers=headers, timeout=15)
    return r.json()

def wa_download_image(media_id: str) -> bytes:
    headers = {"Authorization": f"Bearer {WA_TOKEN}"}
    r = httpx.get(f"https://graph.facebook.com/v22.0/{media_id}", headers=headers, timeout=15)
    media_url = r.json().get("url", "")
    if not media_url:
        return b""
    r2 = httpx.get(media_url, headers=headers, timeout=30)
    return r2.content

def is_comprobante(image_bytes: bytes, mime_type: str = "image/jpeg") -> bool:
    b64 = base64.standard_b64encode(image_bytes).decode()
    msg = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}},
            {"type": "text", "text": "¿Esta imagen es un comprobante/recibo de transferencia bancaria o pago? Responde SOLO 'SI' o 'NO'."}
        ]}]
    )
    return msg.content[0].text.strip().upper() == "SI"

def identify_product(image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    b64 = base64.standard_b64encode(image_bytes).decode()
    msg = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system=SUMIN_SYSTEM,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}},
            {"type": "text", "text": "Identifica qué producto de soldadura/EPP/oxicorte es este. Dame nombre técnico, especificaciones visibles y si lo manejamos en SUMIN."}
        ]}]
    )
    return msg.content[0].text

def detect_city(text: str) -> str | None:
    t = text.lower()
    if any(k in t for k in ['san pedro', 'sps', 'sampedro', 'pedro sula']):
        return 'San Pedro Sula'
    if any(k in t for k in ['tegucigalpa', 'tegu', 'comayagüela', 'comayaguela', 'tegus']):
        return 'Tegucigalpa'
    return None

def bot_asked_city(response: str) -> bool:
    """Detect if the bot's response contains a question asking for the customer's city."""
    r = response.lower()
    # Typical phrasing includes both cities + a question mark
    has_both = ("san pedro" in r or "sps" in r) and ("tegucigalpa" in r or "tegu" in r)
    has_question = "?" in response or "¿" in response
    # Also catch standalone "dónde está" / "de dónde nos escribe"
    asks_location = any(p in r for p in [
        "¿dónde está", "de dónde nos escribe", "de dónde es",
        "¿está en san pedro", "está en san pedro o tegucigalpa"
    ])
    return (has_both and has_question) or asks_location

def claude_respond(system: str, conversation_history: list, new_message: str) -> str:
    messages = conversation_history[-10:] + [{"role": "user", "content": new_message}]
    msg = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        system=system,
        messages=messages
    )
    return msg.content[0].text

def get_conv_meta(state: dict, conv_key: str) -> dict:
    if 'conv_meta' not in state:
        state['conv_meta'] = {}
    if conv_key not in state['conv_meta']:
        state['conv_meta'][conv_key] = {}
    return state['conv_meta'][conv_key]

# ─── ZOHO BOOKS INTEGRATION ──────────────────────────────────────────────────
_zoho_token_cache: dict = {"token": None, "expires": 0.0}
_zoho_catalog_cache: dict = {"items": [], "expires": 0.0}

def get_zoho_access_token() -> str | None:
    global _zoho_token_cache
    if not ZOHO_REFRESH_TOKEN:
        return None
    now = time.time()
    if _zoho_token_cache["token"] and now < _zoho_token_cache["expires"] - 60:
        return _zoho_token_cache["token"]
    try:
        r = httpx.post(
            "https://accounts.zoho.com/oauth/v2/token",
            data={
                "refresh_token": ZOHO_REFRESH_TOKEN,
                "client_id":     ZOHO_CLIENT_ID,
                "client_secret": ZOHO_CLIENT_SECRET,
                "grant_type":    "refresh_token",
            },
            timeout=10,
        )
        data = r.json()
        token = data.get("access_token")
        if token:
            _zoho_token_cache = {"token": token, "expires": now + data.get("expires_in", 3600)}
            log_action("ZohoAPI", "token_refreshed", "OK")
            return token
        log_action("ZohoAPI", "token_error", str(data))
    except Exception as e:
        log_action("ZohoAPI", "token_error", str(e))
    return None

def zoho_check_item(query: str) -> dict | None:
    token = get_zoho_access_token()
    if not token or not ZOHO_ORG_ID:
        return None
    try:
        r = httpx.get(
            "https://www.zohoapis.com/books/v3/items",
            params={
                "organization_id": ZOHO_ORG_ID,
                "search_text":     query,
                "filter_by":       "Status.Active",
            },
            headers={"Authorization": f"Zoho-oauthtoken {token}"},
            timeout=8,
        )
        items = r.json().get("items", [])
        if items:
            names = [i.get("item_name", "") for i in items[:4]]
            rate  = items[0].get("rate", 0.0)
            unit  = items[0].get("unit", "")
            log_action("ZohoAPI", "item_found", f"'{query}' → {names} rate={rate}")
            return {"found": True, "names": names, "rate": rate, "unit": unit}
        log_action("ZohoAPI", "item_not_found", f"'{query}' → 0 results")
        return {"found": False}
    except Exception as e:
        log_action("ZohoAPI", "search_error", str(e))
        return None

def fetch_zoho_catalog() -> list:
    global _zoho_catalog_cache
    now = time.time()
    if _zoho_catalog_cache["items"] and now < _zoho_catalog_cache["expires"]:
        return _zoho_catalog_cache["items"]
    token = get_zoho_access_token()
    if not token or not ZOHO_ORG_ID:
        return _zoho_catalog_cache["items"]
    all_items = []
    page = 1
    try:
        while True:
            r = httpx.get(
                "https://www.zohoapis.com/books/v3/items",
                params={
                    "organization_id": ZOHO_ORG_ID,
                    "filter_by": "Status.Active",
                    "page": page,
                    "per_page": 200,
                },
                headers={"Authorization": f"Zoho-oauthtoken {token}"},
                timeout=15,
            )
            data = r.json()
            items = data.get("items", [])
            if not items:
                break
            all_items.extend(items)
            if not data.get("page_context", {}).get("has_more_page", False):
                break
            page += 1
        _zoho_catalog_cache = {"items": all_items, "expires": now + 3600}
        log_action("ZohoAPI", "catalog_fetched", f"{len(all_items)} items, {page} pages")
        return all_items
    except Exception as e:
        log_action("ZohoAPI", "catalog_error", str(e))
        return _zoho_catalog_cache["items"]

def _normalize_unit(u: str) -> str:
    """Normalize unit strings so 'lb', 'lbs', 'LIBRA' all map to 'LB', etc."""
    if not u:
        return ""
    s = u.strip().upper().rstrip(".")
    if s in {"LB", "LBS", "LIBRA", "LIBRAS", "POUND", "POUNDS"}:
        return "LB"
    if s in {"UND", "UNID", "UNIDAD", "UNIDADES", "PZA", "PIEZA", "U"}:
        return "UND"
    if s in {"CAJA", "CJ", "BOX"}:
        return "CAJA"
    if s in {"ROLLO", "ROLL"}:
        return "ROLLO"
    if s in {"KG", "KILO", "KILOS", "KILOGRAMO"}:
        return "KG"
    return s


_QUERY_STOPWORDS = {
    "el", "la", "los", "las", "un", "una", "unos", "unas",
    "de", "del", "al", "a", "en", "con", "sin", "para", "por",
    "es", "son", "está", "esta", "están", "estan",
    "me", "le", "lo", "se", "te", "mi", "tu",
    "que", "qué", "cual", "cuál", "como", "cómo",
    "y", "o", "u", "pero", "si", "no",
    "cuanto", "cuánto", "cuesta", "vale", "tienen", "hay",
    "manejan", "venden", "stock", "disponible",
    "precio", "busco", "necesito", "quiero", "puedes", "porfa", "porfavor",
    "favor", "buen", "buenos", "buena", "buenas", "dia", "día", "dias", "días",
    "tarde", "noche", "noches",
    "hola", "ok", "gracias", "saludos",
}


def _query_tokens(text: str) -> list[str]:
    """Extract significant tokens for catalog pre-filtering."""
    if not text:
        return []
    s = re.sub(r"[¿?¡!.,;:()\"]", " ", text.lower())
    raw = s.split()
    out: list[str] = []
    for tok in raw:
        tok = tok.strip("-")
        if not tok:
            continue
        if tok in _QUERY_STOPWORDS:
            continue
        if len(tok) < 2:
            continue
        out.append(tok)
    return out


def _prefilter_catalog(query: str, catalog: list, top_n: int = 200) -> list:
    """Score each item by query-token overlap. Falls back to first `top_n` if no matches."""
    tokens = _query_tokens(query)
    if not tokens or not catalog:
        return catalog[:top_n]
    scored: list[tuple[int, float, dict]] = []
    for item in catalog:
        name = (item.get("item_name") or "").lower()
        sku  = (item.get("sku") or "").lower()
        score = 0
        for t in tokens:
            if t in name:
                score += 2
            if t in sku:
                score += 1
        if score > 0:
            stock = float(item.get("stock_on_hand") or 0)
            scored.append((score, stock, item))
    if not scored:
        return catalog[:top_n]
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [it for _, _, it in scored[:top_n]]


def match_product_to_catalog(client_query: str, catalog: list,
                             requested_unit: str = "") -> dict | None:
    """Pick the best-matching Zoho item for a free-text product description.

    Pipeline:
      1) Optional unit narrowing (LB/UND/KG).
      2) Token-based pre-filter so the LLM never has to scan 940 SKUs.
      3) Haiku 4.5 picks the SKU from the focused list.
      4) Resolve SKU → item, with fallbacks.
    """
    if not catalog:
        return None

    req_unit = _normalize_unit(requested_unit)
    filtered = catalog
    if req_unit:
        narrowed = [it for it in catalog
                    if _normalize_unit(it.get("unit", "")) == req_unit]
        if narrowed:
            filtered = narrowed

    focused = _prefilter_catalog(client_query, filtered, top_n=200)

    try:
        log_action("ZohoAPI", "prefilter",
                   f"query='{client_query[:60]}' "
                   f"in:{len(filtered)} out:{len(focused)} "
                   f"top3={[it.get('item_name','')[:40] for it in focused[:3]]}")
    except Exception:
        pass

    catalog_lines = []
    for item in focused:
        name = item.get("item_name", "")
        sku  = item.get("sku", "")
        rate = item.get("rate", 0)
        unit = item.get("unit", "")
        if name:
            catalog_lines.append(f"SKU:{sku} | {name} | unit:{unit} | L.{rate}")
    catalog_text = "\n".join(catalog_lines)

    unit_hint = ""
    if req_unit:
        unit_hint = (
            f"\n\nIMPORTANTE: el cliente pidió la cantidad en {req_unit}. "
            f"Prefiere SIEMPRE un ítem cuya columna unit sea {req_unit}. "
            f"NO elijas un ítem con otra unidad (por ejemplo, nunca UND si el cliente pidió LB)."
        )

    try:
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=60,
            messages=[{"role": "user", "content":
                f"El cliente pregunta por: \"{client_query}\"\n\n"
                f"Catálogo de productos disponibles (una línea por SKU):\n{catalog_text}"
                f"{unit_hint}\n\n"
                f"¿Cuál es el SKU del producto que mejor coincide con lo que pide el cliente?\n"
                f"Responde SOLO el SKU exacto del producto, sin explicaciones. "
                f"Si no hay match claro, responde NINGUNO."}]
        ).content[0].text.strip()
        if not response or response.upper() == "NINGUNO":
            return None
        sku_clean = response.strip()

        for item in focused:
            if item.get("sku", "").strip() == sku_clean:
                return item
        for item in filtered:
            if item.get("sku", "").strip() == sku_clean:
                return item
        for item in catalog:
            if item.get("sku", "").strip() == sku_clean:
                return item
        for item in focused:
            sku = item.get("sku", "")
            if sku and (sku_clean in sku or sku in sku_clean):
                return item
        for item in catalog:
            sku = item.get("sku", "")
            if sku and (sku_clean in sku or sku in sku_clean):
                return item
        return None
    except Exception as e:
        try:
            log_action("ZohoAPI", "match_error", str(e))
        except Exception:
            pass
        return None

_INQUIRY_WORDS = [
    "tienen", "hay", "disponible", "stock", "venden", "manejan",
    "precio", "cuánto", "cuanto", "cuesta", "vale", "busco", "necesito",
    "electrodo", "alambre", "careta", "guante", "chaqueta", "delantal",
    "6011", "6013", "6010", "7018", "7024", "mig", "tig", "oxicorte",
    "disco", "lija", "esmeril", "varilla", "aluminio", "inoxidable",
    "ni-99", "ni99", "ni-55", "ni55", "308", "309", "316",
    "antorcha", "regulador", "boquilla", "tobera", "difusor",
    "kit", "equipo", "victor", "safecut", "respirador",
    # Revestimientos duros y electrodos especiales
    "hard plus", "hardplus", "american hard", "american sugar",
    "chrome carb", "chromecarb", "everwear", "ever wear",
    "e-300", "e300", "e-700", "e700",
    "8018", "9018", "11018", "12018",
    "tungsteno",
]

# Patterns that indicate the customer is giving a detail (diameter, quantity)
# that only makes sense in context of a previous product mention.
_CONTEXTUAL_FOLLOWUP_PATTERNS = [
    r"^\s*\d+\s*/\s*\d+\s*$",                          # "1/8", "5/32", "3 / 32"
    r"^\s*\d+\s*(lbs?|libras?|libra|kg|kilos?)\s*$",   # "10 lbs", "5 libras"
    r"^\s*\d+\s*$",                                    # "10", "100" (a veces cantidad)
    r"^\s*(de\s+|el\s+|la\s+)?\d+\s*/\s*\d+\b.*",     # "de 5/32", "el 1/8 porfa"
]


def _looks_like_contextual_followup(text: str) -> bool:
    """Heuristic: short message that only makes sense in context of a prior
    product mention (e.g. customer replying '5/32' after bot asked diameter)."""
    t = (text or "").strip().lower()
    if len(t) > 40 or not t:
        return False
    for pat in _CONTEXTUAL_FOLLOWUP_PATTERNS:
        if re.match(pat, t):
            return True
    return False


def _last_product_hint_from_history(history: list) -> str:
    """Scan the last few messages (both client and bot) for product keywords.
    Returns a snippet around the product mention, or '' if none found."""
    if not history:
        return ""
    # Look at the last 6 messages, newest first
    recent = list(history[-6:])
    recent.reverse()
    for msg in recent:
        content = (msg.get("content") or "").lower()
        if not content:
            continue
        for kw in _INQUIRY_WORDS:
            if kw in content:
                # Return a bounded snippet (max 120 chars) for the matcher
                return content[:120]
    return ""


def zoho_inventory_context(text: str, history: list | None = None) -> str:
    """Build a [INVENTARIO ZOHO] snippet to inject into the system prompt.

    Priority:
      1. If `text` itself mentions a product/inquiry keyword → search Zoho for `text`.
      2. If `text` is a short contextual follow-up (e.g. just '5/32' or '10 lbs')
         AND the recent history mentions a product → combine history hint + text
         and search Zoho for that combined query. This fixes the case where the
         bot asked 'qué diámetro?' and the customer replied only '5/32', losing
         the product context.
      3. Otherwise → return '' (no Zoho context needed).
    """
    history = history or []
    text_lower = (text or "").lower()
    has_inquiry = any(w in text_lower for w in _INQUIRY_WORDS)

    query = ""
    if has_inquiry:
        query = text
    elif _looks_like_contextual_followup(text):
        hint = _last_product_hint_from_history(history)
        if hint:
            query = f"{hint} {text}".strip()
            log_action("ZohoAPI", "contextual_lookup",
                       f"followup='{text}' hint='{hint[:60]}'")

    if not query:
        return ""

    try:
        catalog = fetch_zoho_catalog()
        if not catalog:
            return ""
        matched = match_product_to_catalog(query, catalog)
        if matched:
            item_name = matched.get("item_name", "")
            sku       = matched.get("sku", "")
            rate      = matched.get("rate", 0.0)
            unit      = matched.get("unit", "")
            price_ctx = ""
            if rate and rate > 0:
                rate_with_isv = round(rate * 1.15, 2)
                price_ctx = (f" Precio base Zoho: L{rate}/{unit} + ISV 15% = *L{rate_with_isv}/{unit}*. "
                             f"Da este precio directamente al cliente (ya incluye ISV).")
            return (f"\n\n[INVENTARIO ZOHO — DATO REAL]: Producto encontrado: '{item_name}' "
                    f"(SKU: {sku}).{price_ctx} "
                    f"Confirma disponibilidad y da precio con ISV incluido.")
        else:
            return (f"\n\n[INVENTARIO ZOHO — DATO REAL]: No se encontró un producto claro en el "
                    f"catálogo de Zoho para la consulta del cliente. IMPORTANTE: esto NO significa que "
                    f"no lo tengamos — puede estar catalogado diferente o con otro nombre. "
                    f"Usa tu conocimiento del catálogo (system prompt) para responder. "
                    f"Solo di que no lo manejamos si el system prompt tampoco lo menciona.")
    except Exception as e:
        log_action("ZohoAPI", "context_error", str(e))
        return ""

# ════════════════════════════════════════════════════════════════════════════════
# ─── SUMIN AGENTS ────────────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════════

def _build_city_context(meta: dict) -> str:
    """Build [CIUDAD_CLIENTE] or [CIUDAD_YA_PREGUNTADA_ANTES] context string."""
    if meta.get("ciudad"):
        return f"\n\n[CIUDAD_CLIENTE: {meta['ciudad']}]"
    if meta.get("city_asked"):
        return ("\n\n[CIUDAD_YA_PREGUNTADA_ANTES: Ya preguntaste la ciudad en un mensaje "
                "anterior. NO vuelvas a preguntar. Si necesitás ciudad para flete/envío, "
                "asume San Pedro Sula por defecto y continuá.]")
    return ""

def _update_city_from_text(meta: dict, text: str, history: list):
    """Detect city from new message or recent history; persist to meta."""
    detected = detect_city(text)
    if detected:
        meta["ciudad"] = detected
        return
    if not meta.get("ciudad"):
        for msg in (history[-6:] if len(history) >= 6 else history):
            c = detect_city(msg.get("content", ""))
            if c:
                meta["ciudad"] = c
                return

def sales_agent(from_number: str, from_name: str, text: str, state: dict):
    """SUMIN: Handle sales inquiries with real-time Zoho inventory check."""
    log_action("SalesAgent", "processing", f"{from_name}: {text}")
    if from_number not in state["conversations"]:
        state["conversations"][from_number] = []
    meta = get_conv_meta(state, from_number)
    if from_name and from_name != from_number:
        meta["name"] = from_name
    meta["last_active"] = datetime.now().isoformat()
    meta["last_msg"] = text[:80]
    history = state["conversations"][from_number]

    _update_city_from_text(meta, text, history)
    city_ctx = _build_city_context(meta)
    zoho_ctx = zoho_inventory_context(text, history=history)
    system = SUMIN_SYSTEM + city_ctx + zoho_ctx
    response = claude_respond(system, history, text)

    # If Claude asked about city in this response, mark it as asked so we don't
    # ask again in future messages of this same conversation.
    if not meta.get("ciudad") and bot_asked_city(response):
        meta["city_asked"] = True
        log_action("SalesAgent", "city_asked_flag", from_number)

    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": response})
    state["conversations"][from_number] = history[-20:]
    wa_send(from_number, response)
    log_action("SalesAgent", "sent_response", response[:100])
    save_state(state)

def vision_agent(from_number: str, from_name: str, media_id: str, mime_type: str, state: dict):
    log_action("VisionAgent", "processing_image", f"{from_name} sent image")
    image_bytes = wa_download_image(media_id)
    if not image_bytes:
        return
    if is_comprobante(image_bytes, mime_type):
        payment_agent(from_number, from_name, media_id, image_bytes, state)
    else:
        product_info = identify_product(image_bytes, mime_type)
        response = f"Identificamos el producto:\n\n{product_info}\n\n¿Cuántas unidades necesita?"
        wa_send(from_number, response)

def payment_agent(from_number: str, from_name: str, media_id: str, image_bytes: bytes, state: dict):
    log_action("PaymentAgent", "processing", f"Comprobante from {from_name}")
    client_name = from_name.split()[0] if from_name else "estimado cliente"
    wa_send(from_number, f"Con gusto {client_name}! Recibimos su comprobante, ya lo procesamos ✅")
    CONFIRMACION_GROUP = os.environ.get("WA_CONFIRMACION_GROUP", "")
    OFICINA_SPS_NUMBER = os.environ.get("WA_OFICINA_SPS", "")
    if CONFIRMACION_GROUP:
        wa_forward_image(media_id, CONFIRMACION_GROUP)
    order = next((o for o in state.get("orders", [])
                  if o.get("client") == from_number and o.get("status") in ["quote_sent", "pending"]), None)
    if OFICINA_SPS_NUMBER:
        wa_forward_image(media_id, OFICINA_SPS_NUMBER)
        info = (f"📋 Pago recibido de {from_name} ({from_number})\n"
                f"Cotización: {order.get('quote','N/A') if order else 'N/A'}\n"
                "Favor procesar y enviar factura + guía de envío.")
        wa_send(OFICINA_SPS_NUMBER, info)
    if order:
        order["status"] = "payment_received"
        order["payment_date"] = datetime.now().isoformat()
    else:
        state["orders"].append({"client": from_number, "name": from_name,
                                "status": "payment_received", "payment_date": datetime.now().isoformat()})
    save_state(state)

def fulfillment_agent(message_data: dict, state: dict) -> bool:
    OFICINA_SPS_NUMBER = os.environ.get("WA_OFICINA_SPS", "")
    if message_data.get("from", "") != OFICINA_SPS_NUMBER:
        return False
    log_action("FulfillmentAgent", "checking_message", "Message from Oficina SPS")
    msg_type = message_data.get("type", "")
    text = message_data.get("text", {}).get("body", "") if msg_type == "text" else ""
    keywords = ["factura", "guía", "guia", "envío", "envio", "tracking", "número de guía"]
    if not (any(k in text.lower() for k in keywords) or msg_type == "document"):
        return False
    pending = sorted([o for o in state.get("orders", []) if o.get("status") == "payment_received"],
                     key=lambda x: x.get("payment_date", ""))
    if not pending:
        return True
    order = pending[0]
    client = order.get("client")
    if msg_type == "document":
        mid = message_data.get("document", {}).get("id")
        if mid: wa_forward_image(mid, client)
    elif msg_type == "image":
        mid = message_data.get("image", {}).get("id")
        if mid: wa_forward_image(mid, client)
    if text:
        wa_send(client, f"📦 Su pedido está en camino!\n{text}")
    order["status"] = "shipped"
    order["shipped_date"] = datetime.now().isoformat()
    save_state(state)
    return True

# ════════════════════════════════════════════════════════════════════════════════
# ─── ZOHO ESTIMATES / COTIZACIONES ───────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════════

QUOTE_TRIGGERS = [
    "cotización", "cotizacion", "cotizame", "cotizar", "presupuesto",
    "estimación", "estimacion", "precio formal", "factura proforma",
    "me pueden cotizar", "me das una coti", "necesito una coti",
    "quiero una coti", "me hacen una coti", "pueden cotizar",
    "hágame cotización", "hagame cotizacion", "hágame una cotización",
]

def detect_quote_request(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in QUOTE_TRIGGERS)


def extract_items_for_quote(text: str, history: list) -> tuple[list[dict], str]:
    """Extract products, quantities, unit of measure, and customer/company name.

    Each item returned has shape: {"product": str, "quantity": float, "unit": str}
    where unit ∈ {"LB","UND","CAJA","ROLLO","KG",""} ("" = cliente no lo especificó).
    """
    recent_ctx = ""
    for m in history[-8:]:
        role = "Cliente" if m["role"] == "user" else "Bot"
        recent_ctx += f"{role}: {m['content'][:300]}\n"

    prompt = (
        "Analiza la conversación y extrae:\n"
        "1. Los PRODUCTOS que el cliente quiere cotizar (busca en TODO el historial, no solo el último mensaje)\n"
        "2. Las CANTIDADES de cada producto\n"
        "3. La UNIDAD DE MEDIDA que pide el cliente para cada producto\n"
        "4. El NOMBRE o EMPRESA para la cotización (si el cliente dijo 'a nombre de X' o 'para la empresa X')\n\n"
        "REGLAS PARA LA UNIDAD (campo `unit`):\n"
        "- 'libra', 'libras', 'lb', 'lbs', 'pound' → \"LB\"\n"
        "- 'unidad', 'unidades', 'und', 'suelto', 'por electrodo', 'pieza', 'pza' → \"UND\"\n"
        "- 'caja', 'cajas' → \"CAJA\"\n"
        "- 'rollo', 'rollos' → \"ROLLO\"\n"
        "- 'kilo', 'kilos', 'kg' → \"KG\"\n"
        "- Si el cliente NO especificó la unidad, devuelve \"\" (string vacío).\n"
        "- NUNCA inventes la unidad — si hay duda, devuelve \"\".\n"
        "- Los ELECTRODOS de soldadura (6010, 6011, 6013, 7018, 7024, E308, E309, E316, Everwear, NI-55, NI-99, etc.) se venden por LIBRA por defecto. Solo los electrodos de TUNGSTENO (TIG) se venden por UND.\n\n"
        "OTRAS REGLAS:\n"
        "- Un nombre de empresa (como 'Proenco', 'ACSA', etc.) NO es un producto — es el destinatario.\n"
        "- Si el cliente dijo 'de 10' o 'quiero 10', es la cantidad del producto mencionado antes.\n"
        "- Busca productos en TODO el historial, no solo en el último mensaje.\n\n"
        f"Conversación reciente:\n{recent_ctx}\n"
        f"Mensaje actual: {text}\n\n"
        "Responde SOLO con JSON válido en este formato:\n"
        '{"items": [{"product": "electrodo 6010 1/8", "quantity": 10, "unit": "LB"}], "customer_name": "Proenco"}\n'
        'Si no hay productos claros: {"items": [], "customer_name": ""}'
    )
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        content = resp.content[0].text.strip()
        match = re.search(r'\{.*\}', content, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            items = parsed.get("items", [])
            # Backfill: if Haiku forgot the unit but the product looks like an
            # electrode, default to LB (welding electrodes are always LB unless
            # tungsten). Tungsten → UND.
            for it in items:
                if not it.get("unit"):
                    pname = (it.get("product") or "").lower()
                    if "tungsteno" in pname or "tig" in pname:
                        it["unit"] = "UND"
                    elif any(k in pname for k in (
                        "electrodo", "6010", "6011", "6013", "7018", "7024",
                        "everwear", "ni-55", "ni55", "ni-99", "ni99",
                        "e308", "e309", "e316", "e310", "e312",
                    )):
                        it["unit"] = "LB"
            customer_name = parsed.get("customer_name", "")
            return items, customer_name
    except Exception as e:
        log_action("QuoteAgent", "extract_error", str(e))
    return [], ""


def zoho_search_item_for_quote(product_name: str,
                               requested_unit: str = "") -> dict | None:
    """Match a natural-language product description against the full Zoho catalog using AI.

    `requested_unit` (e.g. "LB", "UND") is passed down to the matcher so that
    items with a different unit of measure are excluded. This is the core fix
    for the lbs-vs-unidades bug.

    Falls back to raw search_text + unit filter if the AI matcher returns nothing.
    """
    req_unit = _normalize_unit(requested_unit)
    catalog = fetch_zoho_catalog()
    if catalog:
        matched = match_product_to_catalog(product_name, catalog,
                                           requested_unit=req_unit)
        if matched:
            return {
                "item_id": matched.get("item_id", ""),
                "name":    matched.get("item_name", ""),
                "rate":    matched.get("rate", 0.0),
                "unit":    matched.get("unit", ""),
            }
    # Fallback: direct Zoho search, then filter client-side by unit if given
    token = get_zoho_access_token()
    if not token or not ZOHO_ORG_ID:
        return None
    try:
        r = httpx.get(
            "https://www.zohoapis.com/books/v3/items",
            params={"organization_id": ZOHO_ORG_ID, "search_text": product_name, "filter_by": "Status.Active"},
            headers={"Authorization": f"Zoho-oauthtoken {token}"},
            timeout=8,
        )
        items = r.json().get("items", [])
        if req_unit:
            preferred = [i for i in items
                         if _normalize_unit(i.get("unit", "")) == req_unit]
            if preferred:
                items = preferred
        if items:
            i = items[0]
            return {"item_id": i.get("item_id", ""), "name": i.get("item_name", ""),
                    "rate": i.get("rate", 0.0), "unit": i.get("unit", "")}
    except Exception as e:
        log_action("ZohoAPI", "quote_search_error", str(e))
    return None


def zoho_get_or_create_customer(name: str, phone: str) -> str | None:
    """Return contact_id for the given customer name.
    1) Search existing contacts by name (case-insensitive contains).
    2) If none found, create a new contact with San Pedro Sula as billing city.
    Returns None if Zoho is unreachable or both attempts fail."""
    token = get_zoho_access_token()
    if not token or not ZOHO_ORG_ID:
        return None
    clean_name = (name or "").strip()
    if not clean_name:
        return None
    # 1) Search existing
    try:
        r = httpx.get(
            "https://www.zohoapis.com/books/v3/contacts",
            params={"organization_id": ZOHO_ORG_ID, "contact_name_contains": clean_name},
            headers={"Authorization": f"Zoho-oauthtoken {token}"},
            timeout=10,
        )
        contacts = r.json().get("contacts", [])
        # Prefer exact (case-insensitive) match
        for c in contacts:
            if c.get("contact_name", "").strip().lower() == clean_name.lower():
                cid = c.get("contact_id")
                log_action("ZohoAPI", "customer_match_exact", f"{clean_name} → {cid}")
                return cid
        # Otherwise take first result
        if contacts:
            cid = contacts[0].get("contact_id")
            log_action("ZohoAPI", "customer_match_partial",
                       f"{clean_name} → {contacts[0].get('contact_name')} ({cid})")
            return cid
    except Exception as e:
        log_action("ZohoAPI", "customer_search_error", str(e))
    # 2) Create new
    try:
        payload = {
            "contact_name": clean_name,
            "contact_type": "customer",
            "billing_address": {"city": "San Pedro Sula", "country": "Honduras"},
            "shipping_address": {"city": "San Pedro Sula", "country": "Honduras"},
        }
        if phone:
            payload["contact_persons"] = [{
                "first_name": clean_name[:100],
                "phone": phone,
                "mobile": phone,
                "is_primary_contact": True,
            }]
        r = httpx.post(
            "https://www.zohoapis.com/books/v3/contacts",
            params={"organization_id": ZOHO_ORG_ID},
            headers={"Authorization": f"Zoho-oauthtoken {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
        if r.status_code in (200, 201):
            cid = r.json().get("contact", {}).get("contact_id")
            if cid:
                log_action("ZohoAPI", "customer_created", f"{clean_name} → {cid}")
                return cid
        log_action("ZohoAPI", "customer_create_error",
                   f"status={r.status_code} body={r.text[:250]}")
    except Exception as e:
        log_action("ZohoAPI", "customer_create_exc", str(e))
    return None


def zoho_create_estimate(customer_name: str, customer_phone: str, line_items: list[dict]) -> dict | None:
    """Create a Zoho Books estimate. Requires customer_id, which we look up
    (or create) via zoho_get_or_create_customer."""
    token = get_zoho_access_token()
    if not token or not ZOHO_ORG_ID:
        return None
    customer_id = zoho_get_or_create_customer(customer_name, customer_phone)
    if not customer_id:
        log_action("ZohoAPI", "estimate_no_customer", customer_name)
        return None
    formatted = [
        {"item_id": li["item_id"], "name": li["name"], "quantity": li["quantity"],
         "rate": li["rate"], "unit": li.get("unit", "")}
        for li in line_items
    ]
    payload = {
        "customer_id": customer_id,
        "line_items":  formatted,
        "notes":       f"Cotización vía WhatsApp — {customer_phone}",
        "terms":       "Precios incluyen ISV (15%). Válida por 15 días.",
    }
    try:
        r = httpx.post(
            "https://www.zohoapis.com/books/v3/estimates",
            params={"organization_id": ZOHO_ORG_ID},
            headers={"Authorization": f"Zoho-oauthtoken {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
        if r.status_code in (200, 201):
            est = r.json().get("estimate", {})
            log_action("ZohoAPI", "estimate_created",
                       f"#{est.get('estimate_number')} for {customer_name} (cust_id={customer_id})")
            return est
        log_action("ZohoAPI", "estimate_error", f"status={r.status_code} body={r.text[:300]}")
    except Exception as e:
        log_action("ZohoAPI", "estimate_exception", str(e))
    return None


def zoho_get_estimate_pdf(estimate_id: str) -> bytes | None:
    token = get_zoho_access_token()
    if not token or not ZOHO_ORG_ID:
        return None
    try:
        r = httpx.get(
            f"https://www.zohoapis.com/books/v3/estimates/{estimate_id}",
            params={"organization_id": ZOHO_ORG_ID, "accept": "pdf"},
            headers={"Authorization": f"Zoho-oauthtoken {token}"},
            timeout=20,
        )
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/pdf"):
            return r.content
    except Exception as e:
        log_action("ZohoAPI", "pdf_error", str(e))
    return None


def wa_upload_media(file_bytes: bytes, mime_type: str, filename: str) -> str | None:
    url = f"https://graph.facebook.com/v22.0/{PHONE_NUMBER_ID}/media"
    headers = {"Authorization": f"Bearer {WA_TOKEN}"}
    files = {
        "file": (filename, file_bytes, mime_type),
        "type": (None, mime_type),
        "messaging_product": (None, "whatsapp"),
    }
    try:
        r = httpx.post(url, headers=headers, files=files, timeout=30)
        media_id = r.json().get("id")
        log_action("WA_UPLOAD", "media", f"id={media_id} file={filename}")
        return media_id
    except Exception as e:
        log_action("WA_UPLOAD", "error", str(e))
    return None


def wa_send_document(to: str, file_bytes: bytes, filename: str, caption: str = ""):
    media_id = wa_upload_media(file_bytes, "application/pdf", filename)
    if not media_id:
        log_action("WA_SEND", "doc_upload_failed", filename)
        return None
    url = f"https://graph.facebook.com/v22.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    body = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "document",
        "document": {"id": media_id, "filename": filename, "caption": caption},
    }
    r = httpx.post(url, json=body, headers=headers, timeout=15)
    log_action("WA_SEND", f"doc→{to}", filename)
    return r.json()


_QUOTE_AFFIRM_WORDS = {
    "si", "sí", "yes", "ok", "okay", "dale", "correcto", "perfecto", "vale",
    "claro", "asi", "así", "asi mismo", "así mismo", "esta bien", "está bien",
    "confirmado", "afirmativo", "exacto", "sip", "sale", "listo",
}
_QUOTE_NONAME_WORDS = {
    "no", "sin nombre", "no importa", "consumidor final", "ninguno",
    "no tengo", "ninguna", "anonimo", "anónimo", "no aplica",
}


def _parse_quote_name_response(text: str, suggested: str) -> str:
    """Parse the customer's response to '¿A nombre de X o otra empresa?'.
    Returns resolved name. Defaults to 'Consumidor Final' on opt-out.
    """
    t = (text or "").lower().strip().rstrip(".!?¡¿")
    if not t:
        return suggested

    if t in _QUOTE_AFFIRM_WORDS:
        return suggested

    suggested_lower = (suggested or "").lower()
    if suggested_lower and t in (
        f"a nombre de {suggested_lower}",
        f"a nombre del {suggested_lower}",
        f"para {suggested_lower}",
        f"facturar a {suggested_lower}",
    ):
        return suggested

    if t in _QUOTE_NONAME_WORDS or t.startswith("consumidor"):
        return "Consumidor Final"

    for prefix in (
        "a nombre de ", "a nombre del ", "para ", "facturar a ",
        "a la empresa ", "cotizar a ", "facturar para ",
    ):
        if t.startswith(prefix):
            extracted = text[len(prefix):].strip().rstrip(".!?¡¿")
            return extracted or suggested

    if len(text.strip()) >= 2:
        return text.strip().rstrip(".!?¡¿")

    return suggested


def quote_agent(from_number: str, from_name: str, text: str, state: dict):
    """Generate a Zoho estimate + PDF and send via WhatsApp.

    Two-phase flow when customer hasn't named the company in their message:
      Phase 1 — extract products, match Zoho, ASK "¿a nombre de X?".
      Phase 2 — when customer responds with name (or 'sí' / 'sin nombre'),
                orchestrator routes back here, we use stored items.
    """
    log_action("QuoteAgent", "start", f"{from_name}: {text[:80]}")
    history = state["conversations"].get(from_number, [])
    meta = get_conv_meta(state, from_number)
    pending = meta.get("pending_quote") or {}

    if pending.get("items"):
        suggested = pending.get("suggested", "Consumidor Final")
        customer_name = _parse_quote_name_response(text, suggested)
        line_items = pending["items"]
        not_found = pending.get("not_found", [])
        unit_mismatches = pending.get("unit_mismatches", [])
        meta.pop("pending_quote", None)
        log_action("QuoteAgent", "resumed_pending",
                   f"name='{customer_name}' items={len(line_items)}")
    else:
        items_requested, company_name_override = extract_items_for_quote(text, history)

        if not items_requested:
            wa_send(
                from_number,
                "¡Con gusto le preparo la cotización! 📋\n\n"
                "Por favor indíqueme qué productos necesita y en qué cantidades. "
                "Ejemplo: *10 cajas de electrodo 6010 1/8* o *5 caretas básicas*.",
            )
            return

        line_items = []
        not_found = []
        unit_mismatches = []
        for req in items_requested:
            req_unit = _normalize_unit(req.get("unit", ""))
            zoho_item = zoho_search_item_for_quote(req["product"], requested_unit=req_unit)
            if zoho_item and zoho_item.get("item_id"):
                matched_unit = _normalize_unit(zoho_item.get("unit", ""))
                if req_unit and matched_unit and req_unit != matched_unit:
                    unit_mismatches.append(
                        f"{req['product']} (pidió {req_unit}, en catálogo solo hay {matched_unit})"
                    )
                    continue
                line_items.append({**zoho_item,
                                   "quantity": max(1, int(req.get("quantity", 1)))})
            else:
                not_found.append(req["product"])

        if unit_mismatches and not line_items:
            wa_send(
                from_number,
                "Necesito confirmar la unidad de medida antes de cotizar:\n\n"
                f"• {chr(10).join(unit_mismatches)}\n\n"
                "¿Me confirma si es por libra, por unidad o por caja? "
                f"O si prefiere, comuníquese al {ELECTRODE_REDIRECT_PHONE}.",
            )
            return

        if not line_items:
            product_list = ", ".join(r["product"] for r in items_requested)
            wa_send(
                from_number,
                f"Identifiqué que necesita: *{product_list}*.\n\n"
                "Sin embargo, no pude ubicar ese(os) producto(s) en nuestro sistema para generar la cotización automática. "
                f"Por favor comuníquese al {ELECTRODE_REDIRECT_PHONE} para que le preparemos la cotización manualmente. "
                "Podemos enviarla por WhatsApp o correo electrónico. 📋",
            )
            return

        if company_name_override and len(company_name_override) > 1:
            customer_name = company_name_override
        else:
            suggested = (
                from_name if (from_name and from_name != from_number)
                else (meta.get("name") or "Consumidor Final")
            )
            meta["pending_quote"] = {
                "items": line_items,
                "not_found": not_found,
                "unit_mismatches": unit_mismatches,
                "suggested": suggested,
                "asked_at": datetime.now().isoformat(),
            }
            save_state(state)
            items_summary = ", ".join((li.get("name", "") or "")[:35] for li in line_items)
            wa_send(
                from_number,
                f"¡Con gusto le preparo la cotización! 📋\n\n"
                f"Identifiqué: *{items_summary}*\n\n"
                f"¿Le genero la cotización a nombre de *{suggested}* o a nombre de otra empresa?\n"
                f"(Responda 'sí' para usar {suggested}, escriba el nombre de la empresa, "
                f"o 'sin nombre' para Consumidor Final)"
            )
            log_action("QuoteAgent", "asked_customer_name", f"suggested={suggested}")
            return

    estimate = zoho_create_estimate(customer_name, from_number, line_items)
    if not estimate:
        wa_send(
            from_number,
            "Hubo un problema generando la cotización en nuestro sistema. "
            f"Por favor contáctenos al {ELECTRODE_REDIRECT_PHONE}.",
        )
        return

    est_number = estimate.get("estimate_number", "—")
    est_id     = estimate.get("estimate_id", "")
    total      = estimate.get("total", 0.0)

    lines_txt = "\n".join(
        f"  • {li['quantity']} x {li['name']} — L{li['rate'] * li['quantity'] * 1.15:,.2f} c/ISV"
        for li in line_items
    )
    summary = (
        f"✅ *Cotización #{est_number}*\n"
        f"A nombre de: {customer_name}\n\n"
        f"{lines_txt}\n\n"
        f"*Total (ISV incluido):* L{total:,.2f}\n\n"
        f"📄 Enviando el PDF ahora..."
    )
    if not_found:
        summary += f"\n\n⚠️ No encontramos en catálogo: {', '.join(not_found)}. Contáctenos para agregarlos."
    if unit_mismatches:
        summary += ("\n\n⚠️ No cotizamos por diferencia de unidad: "
                    f"{', '.join(unit_mismatches)}. "
                    f"Confírmenos la unidad correcta o comuníquese al {ELECTRODE_REDIRECT_PHONE}.")
    wa_send(from_number, summary)

    pdf_bytes = zoho_get_estimate_pdf(est_id)
    if pdf_bytes:
        wa_send_document(from_number, pdf_bytes,
                         f"Cotizacion_SUMIN_{est_number}.pdf",
                         f"Cotización #{est_number} — SUMIN")
    else:
        wa_send(from_number,
                f"(No se pudo generar el PDF automáticamente. "
                f"Su cotización #{est_number} está registrada en nuestro sistema.)")

    if from_name and from_name != from_number:
        meta["name"] = from_name
    meta["last_active"] = datetime.now().isoformat()
    meta["last_msg"] = text[:80]
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant",
                    "content": f"[Cotización #{est_number} generada para {customer_name}. Total: L{total:,.2f}]"})
    state["conversations"][from_number] = history[-20:]
    save_state(state)
    log_action("QuoteAgent", "done",
               f"Estimate #{est_number} → {customer_name} total=L{total:,.2f}")


# ════════════════════════════════════════════════════════════════════════════════
# ─── ORCHESTRATOR ────────────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════════

def orchestrate(message_data: dict):
    time.sleep(10)

    state     = load_state()
    from_number = message_data.get("from", "")
    from_name   = message_data.get("from_name", from_number)
    msg_type    = message_data.get("type", "text")

    log_action("Orchestrator", "received", f"from={from_name} type={msg_type}")

    if fulfillment_agent(message_data, state):
        return

    if msg_type == "image":
        media_id  = message_data.get("image", {}).get("id", "")
        mime_type = message_data.get("image", {}).get("mime_type", "image/jpeg")
        vision_agent(from_number, from_name, media_id, mime_type, state)
        return

    if msg_type == "text":
        text = message_data.get("text", {}).get("body", "")

        if re.fullmatch(r"[a-zA-Z]{2,5}\d{4,8}", text.strip()):
            log_action("Orchestrator", "skipped_zoho_code", text)
            return

        # If there's a pending quote awaiting customer-name confirmation,
        # route this message to quote_agent regardless of detect_quote_request.
        _meta_check = get_conv_meta(state, from_number)
        if _meta_check.get("pending_quote"):
            log_action("Orchestrator", "routed_pending_quote", from_number)
            quote_agent(from_number, from_name, text, state)
            return

        if detect_quote_request(text):
            quote_agent(from_number, from_name, text, state)
            return

        photo_key = detect_photo_request(text)
        if photo_key:
            if "electrodo" in text.lower() or any(e in text.lower() for e in ["6010","6011","6013","7018","7024","tungsteno","inox"]):
                wa_send(from_number, f"Para fotos de electrodos puede comunicarse al {ELECTRODE_REDIRECT_PHONE} 📞")
                return
            photos_sent = send_product_photos(from_number, photo_key)
            photo_ctx = "\n\n[FOTOS_DISPONIBLES]" if photos_sent else "\n\n[FOTOS_NO_DISPONIBLES]"
            if from_number not in state["conversations"]:
                state["conversations"][from_number] = []
            meta = get_conv_meta(state, from_number)
            if from_name and from_name != from_number:
                meta["name"] = from_name
            meta["last_active"] = datetime.now().isoformat()
            meta["last_msg"] = text[:80]
            history = state["conversations"][from_number]
            _update_city_from_text(meta, text, history)
            city_ctx = _build_city_context(meta)
            zoho_ctx = zoho_inventory_context(text, history=history)
            system_with_ctx = SUMIN_SYSTEM + city_ctx + zoho_ctx + photo_ctx
            response = claude_respond(system_with_ctx, history, text)
            if not meta.get("ciudad") and bot_asked_city(response):
                meta["city_asked"] = True
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": response})
            state["conversations"][from_number] = history[-20:]
            wa_send(from_number, response)
            log_action("SalesAgent", "photo_response", response[:100])
            save_state(state)
            return

        sales_agent(from_number, from_name, text, state)
        return

    elif msg_type == "document":
        doc      = message_data.get("document", {})
        filename = doc.get("filename", "")
        sales_agent(from_number, from_name, f"[Documento adjunto: {filename}]", state)
        return

    else:
        log_action("Orchestrator", "skipped", f"unsupported type: {msg_type}")

# ─── WEBHOOK ENDPOINTS ───────────────────────────────────────────────────────
@app.get("/webhook")
async def verify_webhook(request: Request):
    params    = dict(request.query_params)
    mode      = params.get("hub.mode")
    token     = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        log_action("Webhook", "verified", "OK")
        return PlainTextResponse(challenge)
    return Response(status_code=403)

@app.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()
    try:
        entry    = body["entry"][0]
        changes  = entry["changes"][0]["value"]
        messages = changes.get("messages", [])
        contacts = changes.get("contacts", [])
        name_map = {c["wa_id"]: c["profile"]["name"] for c in contacts}
        for msg in messages:
            msg["from_name"] = name_map.get(msg.get("from", ""), msg.get("from", ""))
            background_tasks.add_task(orchestrate, msg)
    except (KeyError, IndexError):
        pass
    return {"status": "ok"}

# ─── DASHBOARD ───────────────────────────────────────────────────────────────
def _fmt_dashboard_time(iso: str) -> str:
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso)
        now = datetime.now()
        if (now - d).days == 0:
            return d.strftime("%H:%M")
        return d.strftime("%d/%m")
    except:
        return iso[11:16]

@app.get("/dashboard")
async def dashboard():
    try:
        with open(LOG_FILE) as f: logs = json.load(f)
    except:
        logs = []
    try:
        with open(STATE_FILE) as f: state = json.load(f)
    except:
        state = {"orders": [], "conversations": {}, "conv_meta": {}}

    conversations = state.get("conversations", {})
    conv_meta     = state.get("conv_meta", {})

    sorted_phones = sorted(
        conversations.keys(),
        key=lambda p: conv_meta.get(p, {}).get("last_active", ""),
        reverse=True
    )

    contacts_html = ""
    for phone in sorted_phones:
        msgs  = conversations.get(phone, [])
        meta  = conv_meta.get(phone, {})
        name  = html_lib.escape(meta.get("name", phone))
        last  = _fmt_dashboard_time(meta.get("last_active", ""))
        preview_raw = ""
        if msgs:
            last_msg = msgs[-1]
            prefix = "🤖 " if last_msg["role"] == "assistant" else "👤 "
            preview_raw = prefix + last_msg["content"][:55]
        preview = html_lib.escape(preview_raw)
        raw_name = meta.get("name", phone)
        parts = raw_name.split()
        initials = (parts[0][0] + (parts[1][0] if len(parts) > 1 else "")).upper()
        phone_id = phone.replace("+","").replace(" ","")
        contacts_html += f"""<div class='ci' id='c{phone_id}' onclick='show("{phone}")'>
  <div class='av'>{initials}</div>
  <div class='ci-info'>
    <div class='ci-name'>{name}</div>
    <div class='ci-prev'>{preview}</div>
  </div>
  <div class='ci-time'>{last}</div>
</div>"""

    log_colors = {"SalesAgent":"#25d366","VisionAgent":"#2196F3","PaymentAgent":"#FF9800",
                  "FulfillmentAgent":"#9C27B0","Orchestrator":"#607D8B","WA_SEND":"#00BCD4",
                  "ZohoAPI":"#ff7043","PhotoAgent":"#ab47bc","Webhook":"#795548","QuoteAgent":"#ffca28"}
    logs_html = ""
    for entry in reversed(logs[-80:]):
        color = log_colors.get(entry["agent"], "#999")
        logs_html += (f"<div class='lr'>"
                      f"<span class='lt'>{entry['timestamp'][11:19]}</span>"
                      f"<span class='lb' style='background:{color}'>{html_lib.escape(entry['agent'])}</span>"
                      f"<span class='la'>{html_lib.escape(entry['action'])}</span>"
                      f"<span class='ld'>{html_lib.escape(entry['detail'][:90])}</span>"
                      f"</div>")

    conv_json = json.dumps(conversations, ensure_ascii=False)
    meta_json = json.dumps(conv_meta,     ensure_ascii=False)
    n_convs   = len(sorted_phones)
    n_orders  = len(state.get("orders", []))
    ts        = datetime.now().strftime("%H:%M:%S")

    return Response(media_type="text/html", content=f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'><title>SUMIN Bot</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,Segoe UI,sans-serif;background:#111b21;color:#e9edef;height:100vh;display:flex;flex-direction:column;overflow:hidden}}
#hdr{{background:#202c33;padding:10px 20px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #2a3942;flex-shrink:0}}
#hdr h1{{font-size:17px;color:#00a884;font-weight:700}}
.hstats{{display:flex;gap:18px;font-size:13px;color:#8696a0}}
#main{{display:flex;flex:1;overflow:hidden}}
#sidebar{{width:360px;min-width:260px;background:#111b21;border-right:1px solid #2a3942;display:flex;flex-direction:column;overflow:hidden}}
#sb-hdr{{background:#202c33;padding:12px 16px;font-size:12px;color:#8696a0;font-weight:600;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid #2a3942}}
#contact-list{{flex:1;overflow-y:auto}}
.ci{{display:flex;align-items:center;padding:11px 16px;cursor:pointer;border-bottom:1px solid #1f2c34;transition:background .12s}}
.ci:hover,.ci.active{{background:#2a3942}}
.av{{width:46px;height:46px;border-radius:50%;background:#00a884;display:flex;align-items:center;justify-content:center;font-size:16px;font-weight:700;color:#fff;flex-shrink:0;margin-right:12px}}
.ci-info{{flex:1;min-width:0}}
.ci-name{{font-size:15px;font-weight:500;color:#e9edef;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.ci-prev{{font-size:13px;color:#8696a0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px}}
.ci-time{{font-size:11px;color:#8696a0;flex-shrink:0;margin-left:8px}}
#chat{{flex:1;display:flex;flex-direction:column;background:#0b141a;overflow:hidden}}
#chat-hdr{{background:#202c33;padding:11px 18px;display:flex;align-items:center;border-bottom:1px solid #2a3942;flex-shrink:0;min-height:62px}}
#chat-hdr .av2{{width:40px;height:40px;border-radius:50%;background:#00a884;display:flex;align-items:center;justify-content:center;font-size:15px;font-weight:700;color:#fff;margin-right:12px;flex-shrink:0}}
#ch-name{{font-size:15px;font-weight:600;color:#e9edef}}
#ch-phone{{font-size:12px;color:#8696a0}}
#msgs{{flex:1;overflow-y:auto;padding:14px 18px;display:flex;flex-direction:column;gap:3px}}
.msg{{max-width:68%;padding:7px 11px 7px 11px;border-radius:8px;font-size:14px;line-height:1.5;word-wrap:break-word;white-space:pre-wrap}}
.msg.u{{background:#005c4b;align-self:flex-end;border-radius:8px 8px 2px 8px;color:#e9edef}}
.msg.b{{background:#202c33;align-self:flex-start;border-radius:8px 8px 8px 2px;color:#e9edef}}
.msg .rl{{font-size:10px;margin-bottom:3px;opacity:.65}}
.msg.u .rl{{text-align:right;color:#a8d5c2}}
.msg.b .rl{{color:#8696a0}}
#empty{{display:flex;align-items:center;justify-content:center;flex:1;flex-direction:column;gap:12px;color:#8696a0;font-size:14px}}
#log-bar{{background:#202c33;padding:8px 16px;cursor:pointer;color:#8696a0;font-size:12px;text-align:center;border-top:1px solid #2a3942;flex-shrink:0}}
#log-bar:hover{{color:#e9edef}}
#log-panel{{background:#111b21;overflow:hidden;max-height:0;transition:max-height .3s;flex-shrink:0}}
#log-panel.open{{max-height:180px;overflow-y:auto}}
.lr{{display:flex;gap:8px;padding:4px 14px;border-bottom:1px solid #1a2530;font-size:12px;align-items:center}}
.lt{{color:#8696a0;flex-shrink:0;width:56px}}
.lb{{padding:1px 6px;border-radius:8px;font-size:11px;color:#fff;flex-shrink:0}}
.la{{color:#e9edef;flex-shrink:0;min-width:90px}}
.ld{{color:#8696a0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}}
::-webkit-scrollbar{{width:5px}}::-webkit-scrollbar-thumb{{background:#374045;border-radius:3px}}
</style></head>
<body>
<div id='hdr'>
  <h1>⚡ SUMIN Bot</h1>
  <div class='hstats'>
    <span>💬 {n_convs} chats</span>
    <span>📦 {n_orders} órdenes</span>
    <span style='opacity:.5'>{ts}</span>
  </div>
</div>
<div id='main'>
  <aside id='sidebar'>
    <div id='sb-hdr'>Conversaciones — {n_convs}</div>
    <div id='contact-list'>{"" if contacts_html else "<div style='padding:24px;color:#8696a0;font-size:14px'>Sin conversaciones aún</div>"}{contacts_html}</div>
  </aside>
  <section id='chat'>
    <div id='chat-hdr'>
      <div id='ch-av' class='av2' style='display:none'></div>
      <div><div id='ch-name' style='color:#8696a0;font-size:14px'>Selecciona una conversación →</div><div id='ch-phone'></div></div>
    </div>
    <div id='msgs'><div id='empty'><span style='font-size:48px'>💬</span><span>Selecciona un contacto para ver la conversación</span></div></div>
  </section>
</div>
<div id='log-bar' onclick="document.getElementById('log-panel').classList.toggle('open')">📋 Log de sistema (clic para expandir)</div>
<div id='log-panel'>{logs_html or "<div style='padding:12px;color:#8696a0'>Sin actividad</div>"}</div>
<script>
const C={{conversations_json}};
const M={{meta_json}};
let cur=null;
function ini(s){{let p=s.split(' ');return(p[0][0]+(p[1]?p[1][0]:'')).toUpperCase()}}
function show(phone){{
  cur=phone;
  const msgs=C[phone]||[];
  const meta=M[phone]||{{}};
  const name=meta.name||phone;
  document.getElementById('ch-av').style.display='flex';
  document.getElementById('ch-av').textContent=ini(name);
  document.getElementById('ch-name').textContent=name;
  document.getElementById('ch-name').style.color='#e9edef';
  document.getElementById('ch-phone').textContent=phone;
  const box=document.getElementById('msgs');
  box.innerHTML='';
  for(const m of msgs){{
    const d=document.createElement('div');
    d.className='msg '+(m.role==='user'?'u':'b');
    const rl=document.createElement('div');rl.className='rl';
    rl.textContent=m.role==='user'?'Cliente':'SUMIN Bot';
    d.appendChild(rl);
    const t=document.createElement('div');t.textContent=m.content;
    d.appendChild(t);box.appendChild(d);
  }}
  box.scrollTop=box.scrollHeight;
  document.querySelectorAll('.ci').forEach(e=>e.classList.remove('active'));
  const pid=phone.replace(/\\+/g,'').replace(/ /g,'');
  document.getElementById('c'+pid)?.classList.add('active');
}}
const phones=Object.keys(C);
if(phones.length>0)setTimeout(()=>show(phones[0]),50);
setTimeout(()=>{{const u=new URL(location.href);if(cur)u.searchParams.set('sel',cur);location.href=u;}},30000);
const sel=new URLSearchParams(location.search).get('sel');
if(sel&&C[sel])setTimeout(()=>show(sel),60);
</script>
</body></html>""".replace("{conversations_json}", conv_json).replace("{meta_json}", meta_json))

@app.get("/zoho-auth")
async def zoho_auth():
    """Redirect to Zoho OAuth authorizing items + contacts + estimates."""
    scope = "ZohoBooks.items.READ,ZohoBooks.contacts.CREATE,ZohoBooks.contacts.READ,ZohoBooks.estimates.CREATE,ZohoBooks.estimates.READ"
    url = (
        f"https://accounts.zoho.com/oauth/v2/auth"
        f"?scope={scope}"
        f"&client_id={ZOHO_CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={ZOHO_REDIRECT_URI}"
        f"&access_type=offline"
        f"&prompt=consent"
    )
    return Response(
        content=f'<html><body><h2>Autorizar Zoho Books</h2>'
                f'<p><a href="{url}" style="font-size:20px">👉 Haz clic aquí para autorizar</a></p>'
                f'<p>Se pedirán permisos para: items (leer), contactos (leer/crear), cotizaciones (leer/crear).</p></body></html>',
        media_type="text/html"
    )

@app.get("/zoho-callback")
async def zoho_callback(request: Request):
    code = dict(request.query_params).get("code", "")
    if not code:
        return Response("<html><body><h2>❌ No se recibió código de autorización.</h2></body></html>",
                        media_type="text/html", status_code=400)
    try:
        r = httpx.post(
            "https://accounts.zoho.com/oauth/v2/token",
            data={
                "code":          code,
                "client_id":     ZOHO_CLIENT_ID,
                "client_secret": ZOHO_CLIENT_SECRET,
                "redirect_uri":  ZOHO_REDIRECT_URI,
                "grant_type":    "authorization_code",
            },
            timeout=15,
        )
        data = r.json()
        refresh = data.get("refresh_token", "")
        if refresh:
            log_action("ZohoAPI", "oauth_success", "Refresh token obtained")
            return Response(
                content=f"""<html><body style='font-family:sans-serif;padding:30px'>
                <h2>✅ ¡Autorización exitosa!</h2>
                <p><b>Refresh Token:</b></p>
                <textarea rows="3" cols="90" style="font-size:13px">{refresh}</textarea>
                <br><br>
                <p>📋 Agrega este valor en Render como variable de entorno:</p>
                <code style="background:#eee;padding:5px">ZOHO_REFRESH_TOKEN = {refresh}</code>
                <br><br><p style="color:green">El bot ahora puede consultar inventario, crear contactos y generar cotizaciones en Zoho Books.</p>
                </body></html>""",
                media_type="text/html"
            )
        log_action("ZohoAPI", "oauth_error", str(data))
        return Response(f"<html><body><h2>❌ Error: {data}</h2></body></html>",
                        media_type="text/html", status_code=400)
    except Exception as e:
        return Response(f"<html><body><h2>❌ Error: {e}</h2></body></html>",
                        media_type="text/html", status_code=500)

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}

@app.get("/privacy")
async def privacy():
    return Response(content="""<!DOCTYPE html>
<html><head><meta charset='utf-8'><title>Politica de Privacidad</title>
<style>body{font-family:sans-serif;max-width:800px;margin:40px auto;padding:0 20px;color:#333;line-height:1.6}h1{color:#1a1a2e}</style>
</head><body>
<h1>Politica de Privacidad</h1>
<p><strong>Suministros Internacionales HN (SUMIN)</strong> - Abril 2026</p>
<p>Recopilamos el contenido de mensajes y número de teléfono únicamente para atender su solicitud comercial. No compartimos su información con terceros.</p>
<p>Contacto: <a href="mailto:danielprado@suminhn.com">danielprado@suminhn.com</a></p>
</body></html>""", media_type="text/html")
