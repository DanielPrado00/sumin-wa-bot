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

# ─── CONSOLE BRIDGE ──────────────────────────────────────────────────────────
CONSOLE_API_URL = os.environ.get("CONSOLE_API_URL", "")
INTERNAL_API_TOKEN = os.environ.get("INTERNAL_API_TOKEN", "")

def forward_to_console(direction: str, phone: str, name: str, body: str, msg_type: str = "text"):
    """Fire-and-forget POST to sumin-console-backend so the bandeja mirrors WA."""
    if not CONSOLE_API_URL or not INTERNAL_API_TOKEN:
        return
    try:
        httpx.post(
            f"{CONSOLE_API_URL}/internal/messages",
            json={
                "direction": direction,
                "phone": phone,
                "name": name or "",
                "body": body or "",
                "msg_type": msg_type,
            },
            headers={"X-Internal-Token": INTERNAL_API_TOKEN},
            timeout=5,
        )
    except Exception as e:
        try:
            log_action("CONSOLE_BRIDGE", "error", str(e)[:120])
        except Exception:
            pass


def is_conversation_paused(phone: str) -> bool:
    """Ask the console whether a human took over this conversation.

    Returns True ONLY when the console confirms the bot is paused. On any
    error (timeout, network, console down) we default to False so the bot
    keeps working — better to send an extra message than to go silent.
    """
    if not CONSOLE_API_URL or not INTERNAL_API_TOKEN:
        return False
    try:
        r = httpx.get(
            f"{CONSOLE_API_URL}/internal/conversations/{phone}/state",
            headers={"X-Internal-Token": INTERNAL_API_TOKEN},
            timeout=4,
        )
        if r.status_code == 200:
            return bool(r.json().get("paused", False))
    except Exception as e:
        try:
            log_action("CONSOLE_BRIDGE", "paused_check_error", str(e)[:120])
        except Exception:
            pass
    return False


# When True, the bot does NOT send quotes directly to the customer — it submits
# them to the console as `pending_approval`, sends a holding message, and waits
# for a human to click Approve in /approvals (which sends the formal quote via
# WhatsApp from the console).
QUOTE_APPROVAL_MODE = os.environ.get("QUOTE_APPROVAL_MODE", "on").lower() in {"on", "1", "true", "yes"}

# Internal SUMIN numbers that bypass the approval gate. When one of these
# numbers asks the bot for a cotización, we go straight to the legacy
# direct-send flow (estimate + PDF in one shot), because the recipient is
# an employee/owner who just wants the PDF in hand to forward to the end
# customer — they're already trusted, no review needed.
TRUSTED_NUMBERS: set[str] = {
    "50497041381",   # Daniel Prado (founder)
    "50431742116",   # Sumin SPS — tablet de mostrador
    "50431740168",   # Sumin Tegucigalpa — tablet de mostrador
    "50431742019",   # Eva Pinzón
    "50431848009",   # Eduardo Prado
}


def is_trusted_number(phone: str) -> bool:
    """True if `phone` belongs to an internal SUMIN user (skip approval gate)."""
    digits = re.sub(r"\D", "", phone or "")
    return digits in TRUSTED_NUMBERS


# Founder's WhatsApp — receives forwarded MIG-consumable photos for manual
# advisory. Kept separate from TRUSTED_NUMBERS for clarity.
DANIEL_PHONE = "50497041381"

# Phone we tell customers to call when we can't fully resolve via bot.
ELECTRODE_REDIRECT_PHONE_HUMAN = "+504 3334-0477"


def submit_pending_quote_to_console(
    phone: str,
    customer_name: str | None,
    line_items: list[dict],
    zoho_estimate_id: str | None,
    estimate_number: str | None,
    notes: str | None = None,
) -> bool:
    """POST a pending-approval quote to the console. Returns True on success."""
    if not CONSOLE_API_URL or not INTERNAL_API_TOKEN:
        return False
    payload = {
        "phone": phone,
        "customer_name": customer_name or "",
        "zoho_estimate_id": zoho_estimate_id or "",
        "estimate_number": estimate_number or "",
        "notes": notes or "",
        "approval_reason": "Cotización generada por bot, requiere aprobación del vendedor",
        "lines": [
            {
                "name": li.get("name", ""),
                "description": li.get("description") or li.get("name", ""),
                "quantity": float(li.get("quantity", 0) or 0),
                "rate": float(li.get("rate", 0) or 0),
                "quoted_unit": li.get("unit", "UND") or "UND",
                "requested_unit": li.get("requested_unit"),
                "sku": li.get("sku"),
                "zoho_item_id": li.get("item_id"),
            }
            for li in line_items
        ],
    }
    try:
        r = httpx.post(
            f"{CONSOLE_API_URL}/internal/quotes",
            json=payload,
            headers={"X-Internal-Token": INTERNAL_API_TOKEN},
            timeout=8,
        )
        if r.status_code == 200:
            log_action("CONSOLE_BRIDGE", "quote_pending_submitted",
                       f"phone={phone} est={estimate_number} status=ok")
            return True
        log_action("CONSOLE_BRIDGE", "quote_submit_error",
                   f"status={r.status_code} body={r.text[:150]}")
    except Exception as e:
        try:
            log_action("CONSOLE_BRIDGE", "quote_submit_exception", str(e)[:120])
        except Exception:
            pass
    return False


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

9. ENVÍOS / RETIRO EN TIENDA:

   ━━━ REGLA POR MONTO DEL PEDIDO (MUY IMPORTANTE) ━━━
   • Si el pedido es MENOR a L.10,000 (orden chica/promedio):
     - NO ofrezcas envío proactivamente.
     - Pregunta primero: "¿Desde qué parte del país nos escribe?
       ¿Pasa por nuestra tienda en San Pedro Sula o en Tegucigalpa?"
     - Solo si el cliente pide explícitamente envío, ofrécelo (ver opciones abajo).

   • Si el pedido es MAYOR O IGUAL a L.10,000 (orden grande):
     - Pregunta directo: "¿Necesita que se lo enviemos o pasa a recoger por
       nuestra tienda?" (luego confirma sucursal SPS/Tegucigalpa o destino).

   ━━━ OPCIONES DE ENVÍO (cuando aplique) ━━━
   - Fuera de San Pedro Sula y Tegucigalpa (resto del país terrestre):
     Expreco — 1-2 días hábiles.
   - Islas de la Bahía (Utila, Roatán, Guanaja):
     Island Shipping o Bahía Shipping (no usamos Expreco para islas).
   - Flete Tarifa A (SPS↔Tegucigalpa o SPS↔Puerto Cortés):
     L.87 base + L.1/lb adicional.
   - Flete Tarifa B (otros destinos):
     L.174 base + L.1.96/lb adicional.

   ━━━ ALIAS DE CIUDADES (trata como equivalentes) ━━━
   - "sps" = "san pedro" = "san pedro sula" = SPS.
   - "tegus" = "tgu" = "tegucigalpa" = "comayagüela" = TGU.
   - "ceiba" = "la ceiba"; "puerto" / "pto cortés" = Puerto Cortés.
   - "utila" / "roatán" / "roatan" / "guanaja" = Islas de la Bahía
     (siempre Island/Bahía Shipping, nunca Expreco).

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

═══════════════════════════════════════
BOQUILLAS — FLUJO ANTES DE COTIZAR (MUY IMPORTANTE)
═══════════════════════════════════════
La palabra "boquilla" SOLA es ambigua. NUNCA cotices "boquilla" sin clasificar primero.
Hay 3 categorías totalmente distintas que NO debés confundir:

A) BOQUILLA PARA SOLDAR (oxígeno-acetileno, soldadura autógena, NO corta)
   - SKU SafeCut: BOQ-SOL-x (donde x = 0, 1, 2, 3, 5...)
   - SKU Victor: 4010012, 4010002, etc. (BOQUILLA PARA SOLDAR # X VICTOR ORIGINAL)
   - Sirve para SOLDAR metales, no cortar ni calentar.
   - Si el cliente dice "boquilla para soldar" o "boquilla de soldadura" → es esto.

B) BOQUILLA DE CORTE (corta metales con oxicorte o plasma)
   - Subdivide en B1 oxicorte (acetileno o GPN/propano) y B2 plasma — preguntar SIEMPRE.

   B1) Oxicorte con ACETILENO:
       - Estilo 1-101 (SafeCut/Victor) → SKU SafeCut BOQ-1-101-x-SC, SKU Victor distinto.
       - Estilo Harris → SKU empieza en 62...
       Si el cliente dice "boquilla de corte" sin más detalle: preguntar
       "¿Es para oxicorte (acetileno o gas LPG) o para plasma?" Si dice oxicorte:
       "¿Estilo 1-101 (SafeCut/Victor) o estilo Harris?"

   B2) Oxicorte con GAS LPG / PROPANO (GPN):
       - SKU SafeCut: BOQ-GPN-x-SC (donde x = 1, 2, 3, 4, 5)
       - Si el cliente dice que usa GPN, propano o LPG → ese es el camino.

   B3) Plasma:
       - Pedir modelo de la máquina (Hypertherm, Lincoln, Hugong, Texas, Miller).
       - O pedir foto del cuerpo de la antorcha (ambos lados) y de los consumibles.
       - Sin esa info NO se puede cotizar plasma.

C) BOQUILLA / ANTORCHA MULTIFLAMA (CALENTAR, NO cortar)
   - SKU SafeCut: MFA-x-SC (donde x = 8, 10, 12, 15)
   - Sirve para CALENTAR metales (precalentamiento de piezas), NO cortar.
   - ⚠️ A veces el cliente dice "boquilla para calentar" o "para cortar" pero quiere
     multiflama (uso real: calentar). Si dudás, preguntá: "¿Es para calentar piezas
     o para cortar metales?" Si dice "calentar" → multiflama. Si dice "cortar" → ir
     al flujo B (boquilla de corte).
   - NUNCA digas que la multiflama es "para cortar". Es PARA CALENTAR.

═══════════════════════════════════════
REGLA UNIVERSAL DE BOQUILLAS
═══════════════════════════════════════
Si el cliente dice solo "boquilla" o "boquilla de corte" sin detalle:
1. PRIMERO preguntar: "¿Para soldar, para cortar (oxicorte/plasma), o multiflama (calentar)?"
2. Si dice cortar → preguntar "¿oxicorte (acetileno o gas LPG) o plasma?"
3. Si oxicorte → preguntar "¿estilo 1-101 (SafeCut/Victor) o estilo Harris?"
4. Si plasma → preguntar modelo de máquina (Hypertherm, Lincoln, Hugong, Texas, Miller) o pedir foto
5. Si tiene número (#1, #3, etc.) ya sabés el size — confirmá tipo igual.

NUNCA cotices boquilla GPN-1 / 1-101-3 / etc. sin pasar por este flujo, aunque [INVENTARIO ZOHO]
sugiera un match. El SKU exacto depende del tipo confirmado por el cliente.

═══════════════════════════════════════
TABLA DE BOQUILLAS DE CORTE — GROSOR → NÚMERO
═══════════════════════════════════════
Cuando el cliente diga el grosor de la lámina/material que va a cortar (en pulgadas o
milímetros), respondele con el número de boquilla que corresponde. NO pidas el número —
deducílo del grosor. Si el cliente pide "para cortar 1/4 de pulgada", la respuesta es
"para 1/4\" le corresponde la boquilla #0 (acetileno) o #0 (GPN)".

⚠️ SUMIN NO STOCKEA los tamaños "000" ni "00". Si el grosor del cliente cae en esos rangos,
mencionarle que ese tamaño no lo manejamos y redirigir al teléfono o tienda. Solo cotizamos
de #0 hacia arriba.

━━━ ACETILENO — Estilo 1-101 (SafeCut/Victor) ━━━
Aplica para sopletes Victor/SafeCut con gas acetileno (oxiacetilénico):
  • Grosor hasta 1/2"  (≤12 mm)  → boquilla #0
  • Grosor hasta 3/4"  (≤19 mm)  → boquilla #1
  • Grosor hasta 1"    (≤25 mm)  → boquilla #2
  • Grosor hasta 2"    (≤50 mm)  → boquilla #3
  • Grosor hasta 3"    (≤76 mm)  → boquilla #4
  • Grosor hasta 4"    (≤102 mm) → boquilla #5
  • Grosor hasta 6"    (≤152 mm) → boquilla #6

━━━ PROPANO / GAS LPG / GAS NATURAL — Estilo GPN (SafeCut/Victor) ━━━
Aplica para sopletes Victor/SafeCut con propano, LPG o gas natural:
  • Grosor hasta 1/2"  (≤12 mm)  → boquilla GPN #0
  • Grosor hasta 3/4"  (≤19 mm)  → boquilla GPN #1
  • Grosor hasta 1"    (≤25 mm)  → boquilla GPN #2
  • Grosor hasta 2"    (≤50 mm)  → boquilla GPN #3
  • Grosor hasta 3"    (≤76 mm)  → boquilla GPN #4
  • Grosor hasta 4"    (≤102 mm) → boquilla GPN #5

━━━ PLASMA — Estilo P80 (SafeCut, antorchas Panasonic / P80) ━━━
Solo aplica si el cliente confirmó que su antorcha es Panasonic o P80:
  • Grosor 1/32" – 1/4"   (1–6 mm)   → boquilla P80-40A
  • Grosor 1/4"  – 1/2"   (6–12 mm)  → boquilla P80-60A
  • Grosor 1/2"  – 3/4"   (12–20 mm) → boquilla P80-80A

━━━ PLASMA — Otras antorchas (Hypertherm 1000s, Lincoln Tomahawk, Hugong, Texas, Miller) ━━━
NO TENEMOS TABLA DE EQUIVALENCIAS. Pedirle al cliente:
  "¿Me puede pasar el número de parte impreso en la boquilla actual, o foto de la
   boquilla y de la antorcha? Sin eso no podemos confirmar la referencia compatible."

NO inventes referencias para estas marcas. Si el cliente no tiene número de parte ni foto,
redirigir a +504 3334-0477.

━━━ CONVERSIÓN MM ↔ PULGADAS (referencia rápida) ━━━
  3 mm ≈ 1/8"   |  6 mm ≈ 1/4"   |  10 mm ≈ 3/8"   |  12 mm ≈ 1/2"
  19 mm ≈ 3/4"  |  25 mm ≈ 1"    |  50 mm ≈ 2"     |  76 mm ≈ 3"
  102 mm ≈ 4"   |  152 mm ≈ 6"

═══════════════════════════════════════
CONSUMIBLES MIG — REGLA DE FOTO DEL DIFUSOR
═══════════════════════════════════════
Para CUALQUIER consumible MIG (boquilla, tobera, difusor) SIEMPRE pedir foto del difusor:
"Para confirmar la referencia exacta necesito que me mande foto del DIFUSOR (la pieza dorada
donde se enrosca la boquilla y la tobera). Sin esa foto es difícil confirmar cuál es compatible."

Si el cliente manda solo foto de boquilla/tobera sin difusor: pedirle también foto del difusor.
Solo después de tener foto del difusor podemos identificar el sistema (Lincoln Magnum, Miller,
Tweco, etc.) y la referencia correcta. Si el cliente no puede mandar foto del difusor:
redirigir a +504 3334-0477.

═══════════════════════════════════════
SISTEMAS MIG QUE MANEJAMOS — REFERENCIA INTERNA
═══════════════════════════════════════
SUMIN tiene en stock 6 sistemas distintos de consumibles MIG. Cuando un asesor humano
identifica el sistema correcto a partir de la foto del difusor, le manda al cliente
una lista con descripciones GENÉRICAS (sin SKU, sin marca específica) y el total.

⚠️ POLÍTICA DE PRIVACIDAD COMERCIAL: nunca le digas al cliente "es del sistema Magnum 200"
o "es estilo Tweco" o "el SKU es XXX". Solo le decimos genéricamente "boquilla", "difusor",
"tobera/capuchón" + precio. Esto es para que el cliente no pueda buscar la pieza por
referencia exacta en Amazon/Temu y termine comprando con SUMIN. Mantener vague.

Sistema 1 — MILLER serie 169 (modelo anterior 252, descontinuado):    4 piezas
  • Boquilla 0.035" (000-068) o 0.045" (000-069) — L74.75 c/ISV
  • Difusor parte interna (169-728) — L319.30 c/ISV
  • Retenedor (169-729) — L345.00 c/ISV
  • Tobera/capuchón (169-724) — L569.25 c/ISV

Sistema 2 — M25 / Magnum 200:                                           3 piezas
  • Boquilla (11-35) — L51.75 c/ISV
  • Difusor (FP 1510-1140) — L207.00 c/ISV
  • Tobera/capuchón (21-50 WPW) — L339.25 c/ISV

Sistema 3 — M250 / Magnum 250:                                          3 piezas
  • Boquilla (14-35) — L51.75 c/ISV
  • Difusor (52FN WPW) — L287.50 c/ISV
  • Tobera/capuchón (23-50 WPW) — L402.50 c/ISV

Sistema 4 — MDX (Miller nuevo MDX-250):                                 3 piezas
  • Boquilla 0.035 / 0.045 / 0.055 (TM Acculock) — L46.00 c/u c/ISV
  • Difusor (DM-250 A.A.) — L298.01 c/ISV
  • Tobera/capuchón (N-M1200C A.A.) — L356.50 c/ISV

Sistema 5 — Tipo BINZEL:                                                3 piezas
  • Boquilla (11-35, igual que M25) — L51.75 c/ISV
  • Difusor para antorcha BINZEL A.A. — L226.32 c/ISV
  • Tobera plateada BINZEL A.A. (145-0075) — L336.95 c/ISV

Sistema 6 — Serie HD:                                                   3 piezas
  • Boquilla HD — ~L92 c/ISV (asesor confirma referencia exacta)
  • Difusor Gas HD (Miller HD54-16) — L483.00 c/ISV
  • Tobera/capuchón (HD24-62 WPW) — L603.75 c/ISV

═══════════════════════════════════════
REGLA "NUNCA DECIR NO HAY" PARA CONSUMIBLES MIG
═══════════════════════════════════════
Si el cliente pide un consumible MIG y no estás 100% seguro de que el sistema cae en
los 6 que manejamos arriba: NO le digas "no manejamos eso" ni "no tenemos". En su
lugar, redirigílo:

   "Para confirmarle disponibilidad y precio exacto de ese consumible, por favor
    comuníquese con uno de nuestros asesores al +504 3334-0477."

La razón: el inventario cambia, hay equivalencias entre sistemas, y un asesor humano
puede revisar stock real y proponer alternativas. Decir "no hay" cierra la venta;
redirigir mantiene la oportunidad abierta.
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
    forward_to_console("outbound", to, "", text)
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


# Audio download is functionally identical to image — just an alias for clarity.
wa_download_audio = wa_download_image


def wa_send_interactive_list(
    to: str,
    body_text: str,
    button_label: str,
    sections: list[dict],
    header_text: str | None = None,
    footer_text: str | None = None,
) -> dict:
    """Send a WhatsApp interactive list message.

    `sections` is a list of {"title": str, "rows": [{"id": str, "title": str, "description": str?}]}.
    WhatsApp limits: button title ≤ 20 chars, row title ≤ 24 chars, row description ≤ 72 chars,
    up to 10 rows total across all sections.
    """
    url = f"https://graph.facebook.com/v22.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    interactive = {
        "type": "list",
        "body": {"text": body_text[:1024]},
        "action": {"button": button_label[:20], "sections": sections},
    }
    if header_text:
        interactive["header"] = {"type": "text", "text": header_text[:60]}
    if footer_text:
        interactive["footer"] = {"text": footer_text[:60]}
    body = {"messaging_product": "whatsapp", "to": to, "type": "interactive", "interactive": interactive}
    r = httpx.post(url, json=body, headers=headers, timeout=15)
    log_action("WA_SEND", f"interactive_list→{to}", button_label)
    forward_to_console("outbound", to, "", body_text)
    return r.json()


def transcribe_audio_whisper(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """Transcribe a WhatsApp voice note via OpenAI Whisper. Returns "" on failure.

    WhatsApp delivers voice notes as audio/ogg (Opus codec). Whisper handles this
    format natively, no preprocessing required. Cost: ~$0.006/minute.
    Requires OPENAI_API_KEY env var (set in Render).
    """
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        log_action("Whisper", "missing_api_key", "OPENAI_API_KEY not set")
        return ""
    if not audio_bytes:
        return ""
    # Pick a sensible filename suffix matching the mime type so Whisper auto-detects
    suffix = ".ogg"
    if "mp4" in mime_type or "m4a" in mime_type:
        suffix = ".m4a"
    elif "mpeg" in mime_type or "mp3" in mime_type:
        suffix = ".mp3"
    elif "wav" in mime_type:
        suffix = ".wav"
    elif "webm" in mime_type:
        suffix = ".webm"
    try:
        files = {"file": (f"audio{suffix}", audio_bytes, mime_type)}
        data  = {"model": "whisper-1", "language": "es"}
        r = httpx.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            files=files,
            data=data,
            timeout=60,
        )
        if r.status_code == 200:
            text = (r.json().get("text") or "").strip()
            log_action("Whisper", "transcribed", text[:120])
            return text
        log_action("Whisper", "error", f"status={r.status_code} body={r.text[:200]}")
    except Exception as e:
        log_action("Whisper", "exception", str(e)[:200])
    return ""

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


def try_extract_order_from_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict | None:
    """Inspect an image and try to extract a structured order list.

    Returns a dict with shape:
      {
        "is_order_list": bool,
        "items":    [{"quantity": float, "unit": str, "product": str}, ...],
        "excluded": [{"quantity": float, "unit": str, "product": str, "reason": str}, ...]
      }
    Returns None if the image clearly isn't an order list, or on parse error.

    "Order list" means: a structured list of multiple welding products with quantities —
    handwritten purchase orders, screenshots of order lists, typed lists, invoices, etc.
    A photo of a single loose product (one electrode, one disc) is NOT an order list.

    Exclusion detection: lines that are crossed out, marked "no comprar" / "X" /
    "S/STOCK" / "fuera de stock", or have a check ✓ next to them (already ordered).
    """
    b64 = base64.standard_b64encode(image_bytes).decode()
    extraction_prompt = (
        "Eres un extractor de items de pedidos para SUMIN (ferretería de soldadura, "
        "Honduras). Mirá la imagen y respondé EN JSON puro, sin texto antes ni después, "
        "sin bloques de código markdown.\n\n"
        "Schema:\n"
        "{\n"
        '  "is_order_list": true|false,\n'
        '  "items":    [{"quantity": <num>, "unit": "lb"|"und"|"caja"|"rollo"|"kg"|"", '
        '"product": "<descripción>"}],\n'
        '  "excluded": [{"quantity": <num>, "unit": <str>, "product": <str>, '
        '"reason": "tachado"|"no comprar"|"sin stock"|"ya pedido"}]\n'
        "}\n\n"
        "is_order_list es TRUE solo si la imagen es una lista estructurada con varias "
        "filas de productos + cantidades (solicitud de compra, lista a mano, screenshot "
        "de pedido, factura, requisición, etc). Para foto de un producto suelto: "
        "is_order_list = false, items = [], excluded = [].\n\n"
        "EXCLUSIONES — poné el item en `excluded` si:\n"
        "- la línea está tachada (línea horizontal sobre el texto),\n"
        "- al lado dice 'no comprar', 'no', 'X', 'cancelar', 'omitir',\n"
        "- al lado dice 'S/STOCK', 'sin stock', 'fuera de stock', 'no hay',\n"
        "- tiene un check ✓ que indica 'ya pedido / ya entregado'.\n\n"
        "PRODUCTOS típicos: electrodos (6011, 6013, 7018, 309-16, 7018-1, 7024, "
        "INCONEL/E NiCrFe-3, electrodo 800, tungsteno TIG), alambre MIG/TIG, varillas, "
        "discos abrasivos, caretas, guantes, gas. Si una línea es ambigua, igual ponela "
        "en items con el texto que mejor leas + agregale ' [verificar]' al final del product.\n\n"
        "━━━ FRACCIONES DE ELECTRODO (CRÍTICO) ━━━\n"
        "En SUMIN las fracciones de electrodo son SIEMPRE una de este set cerrado:\n"
        "  1/16, 5/64, 3/32, 1/8, 5/32, 3/16, 7/32, 1/4, 5/16, 3/8\n"
        "En manuscrita la letra 'G' o el dígito '8' se confunden con la fracción '1/8'. "
        "El '32' a veces se lee como '52' o '5z'. Si ves algo como '7/G', '8/G', '7/8', "
        "'1/G' al lado de un electrodo, en el 95% de los casos es '1/8'. Si ves '5/52' o "
        "'3/52' es probablemente '5/32' o '3/32'. NUNCA devuelvas una fracción que no "
        "esté en el set cerrado de arriba — si dudás, elegí la más común (1/8 para "
        "6011/7018/309-16, 3/32 para tungsteno) y agregá ' [verificar]' al final del product.\n\n"
        "Si quantity no se ve, usá 1. Si la unidad no se ve, dejala como cadena vacía."
    )
    try:
        msg = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system="Sos un extractor estructurado. Devolvés exclusivamente JSON puro.",
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}},
                {"type": "text", "text": extraction_prompt},
            ]}],
        )
        raw = msg.content[0].text.strip()
        # Tolerate ```json ... ``` wrappers in case the model adds them despite instructions
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)
    except Exception as e:
        log_action("VisionAgent", "extract_order_parse_error", str(e)[:200])
        return None

    if not isinstance(data, dict) or not data.get("is_order_list"):
        return None
    items = [it for it in (data.get("items") or []) if it.get("product")]
    excluded = data.get("excluded") or []
    if not items:
        return None
    return {"is_order_list": True, "items": items, "excluded": excluded}

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

# Hints en la respuesta del modelo de visión que indican que el cliente mandó un
# consumible MIG (difusor, portacontacto, tobera, antorcha). En esos casos hay tantas
# variables de compatibilidad (Lincoln Magnum / Miller / Tweco / Euro / etc.) que
# es mejor que un asesor humano lo atienda directo, en lugar de que el bot dé un
# análisis técnico largo y termine pidiendo más fotos.
_MIG_CONSUMABLE_HINTS = (
    "difusor", "diffuser", "gas diffuser",
    "portacontacto", "porta contacto", "punta de contacto", "contact tip",
    "tobera mig", "boquilla mig", "consumible mig", "consumibles mig",
    "antorcha mig", "magnum 100", "magnum 200", "magnum 300",
    "tweco", "euro mig", "lincoln magnum", "miller mig",
)


def _looks_like_mig_consumable(product_info: str) -> bool:
    p = (product_info or "").lower()
    return any(k in p for k in _MIG_CONSUMABLE_HINTS)


# Hints en la respuesta del modelo de visión que indican electrodo de TUNGSTENO TIG.
# Se vende por unidad. Cualquier color/dopaje cuenta:
#   Torio 2% (rojo), Cerio 2% (gris), Lantano (azul, dorado), Puro (verde),
#   Rare Metals (morado).
_TUNGSTEN_HINTS = (
    "tungsteno", "tungsten", "tig",
    "torio", "cerio", "lantano", "rare metals", "puro verde",
    "wt-20", "wc-20", "wl-15", "wl-20", "wp",  # designaciones AWS
)

# Hints que indican electrodo común de revestido (se vende por libra):
# 6011, 6013, 7018, 7018-1, 7024, 309-16, 308-16, 316-16, 800/INCONEL, etc.
_LB_ELECTRODE_HINTS = (
    "electrodo 6011", "electrodo 6013", "electrodo 7018", "electrodo 7024",
    "electrodo 309", "electrodo 308", "electrodo 316",
    "e6011", "e6013", "e7018", "e7024", "e309", "e308", "e316",
    "inconel", "nicrfe", "electrodo 800", "e800",
    "electrodo revestido", "revestido", "stick electrode",
)


def _quantity_question_for_product(product_info: str) -> str:
    """Decide whether to ask the customer for quantity in LBS or UND.

    Tungsten TIG electrodes are sold by unit; common stick electrodes
    (6011/7018/309-16/etc.) are sold by pound. Anything else defaults to UND.
    """
    p = (product_info or "").lower()
    # Tungsten check is FIRST because tungsten lines may also contain "electrodo"
    if any(k in p for k in _TUNGSTEN_HINTS):
        return "¿Cuántas unidades necesita?"
    if any(k in p for k in _LB_ELECTRODE_HINTS):
        return "¿Cuántas libras necesita?"
    return "¿Cuántas unidades necesita?"


# ════════════════════════════════════════════════════════════════════════════════
# ─── POST-COTIZACIÓN: CONFIRMACIÓN / CORRECCIÓN ──────────────────────────────
# ════════════════════════════════════════════════════════════════════════════════
# Después de un direct-send (bypass para trusted users) el bot pregunta al
# usuario si los items están bien. Si el usuario corrige cantidades o productos,
# el bot actualiza el estimate en Zoho y manda PDF nuevo.

CONFIRMATION_TTL_MIN = 30   # minutos vigentes después de mandar PDF
CONFIRM_KEYWORDS = (
    "✅", "ok", "okay", "todo bien", "todo correcto", "correcto",
    "confirmo", "confirmado", "está bien", "esta bien", "perfecto",
    "sí", "si", "yes", "👍",
)


def _build_confirmation_message(line_items: list[dict], total: float, est_number: str) -> str:
    """Plain-text fallback (used inside the interactive list body, and as a backup
    if the interactive send fails)."""
    lines_txt = "\n".join(
        f"  {i+1}. {li['quantity']:g} {li.get('unit','UND')} · {li['name']} · "
        f"L{li['rate'] * li['quantity'] * 1.15:,.2f}"
        for i, li in enumerate(line_items)
    )
    return (
        f"📋 *Confirme los items — cotización #{est_number}*\n"
        f"{lines_txt}\n"
        f"                                ───────\n"
        f"                       *Total:* L{total:,.2f}"
    )


def _send_confirmation_prompt(to: str, line_items: list[dict], total: float, est_number: str) -> None:
    """Send the post-PDF confirmation prompt as a WhatsApp interactive list.

    User taps:
      • "✅ Todo correcto"            → confirms
      • "📝 Corregir item N"          → bot asks what to change for item N
      • "❌ Cancelar / dejar pendiente" → drops the confirmation state

    For multiple corrections in one go, the user can type/audio anything during
    the list-reply window and confirmation_agent's text path handles it.
    """
    body = _build_confirmation_message(line_items, total, est_number)
    rows = [{"id": "confirm_ok", "title": "✅ Todo correcto"}]
    for i, li in enumerate(line_items[:8]):  # WA limit: 10 rows total; reserve 2 for actions
        idx = i + 1
        title = f"📝 Corregir item {idx}"[:24]
        descr = f"{li['quantity']:g} {li.get('unit','UND')} · {li['name']}"[:72]
        rows.append({"id": f"correct_{idx}", "title": title, "description": descr})
    rows.append({"id": "cancel", "title": "❌ Cancelar"})
    sections = [{"title": "Acciones", "rows": rows}]
    try:
        wa_send_interactive_list(
            to=to,
            body_text=body,
            button_label="Revisar items",
            sections=sections,
            header_text=f"Cotización #{est_number}",
            footer_text="Tapeá una opción o respondé por texto/audio",
        )
    except Exception as e:
        log_action("ConfirmAgent", "interactive_send_error", str(e)[:200])
        # Fallback to plain text + "Respondé: ..." instructions
        wa_send(to, body + "\n\nRespondé:\n  • *✅* si está todo correcto\n  • Número 1-N para corregir")


def _save_pending_confirmation(meta: dict, *, estimate_id: str, estimate_number: str,
                                customer_id: str, customer_name: str,
                                line_items: list[dict], total: float) -> None:
    """Snapshot the just-sent quote in conv_meta so a subsequent message can
    correct it. Expires after CONFIRMATION_TTL_MIN minutes."""
    from datetime import timedelta
    meta["pending_confirmation"] = {
        "estimate_id":     estimate_id,
        "estimate_number": estimate_number,
        "customer_id":     customer_id,
        "customer_name":   customer_name,
        "line_items":      [dict(li) for li in line_items],
        "total":           total,
        "expires_at":      (datetime.now() + timedelta(minutes=CONFIRMATION_TTL_MIN)).isoformat(),
        "awaiting_field_for_item": None,  # set to N when user replied "N" alone
    }


def _confirmation_expired(pc: dict) -> bool:
    try:
        return datetime.fromisoformat(pc["expires_at"]) < datetime.now()
    except Exception:
        return True


def _parse_confirmation_response(text: str, items: list[dict]) -> dict:
    """Parse the user's reply into an action structure that supports multiple
    corrections in one message ("el 6011 eran 800 y el 800 eran 200" → 2 corrections).

    Returns one of:
      {"action": "confirm"}
      {"action": "cancel"}
      {"action": "ask_what_to_change", "item_index": N}
      {"action": "corrections", "corrections": [{"item_index": N, "field": "quantity"|"product", "new_value": ...}, ...]}
      {"action": "ambiguous"}

    `items` is the current list of line_items (used so the LLM can match products
    by name like "el 6011" → item index whose name contains "6011").
    """
    t = (text or "").strip().lower()
    items_count = len(items)
    # Fast path 1: explicit confirm keywords
    if any(k in t for k in CONFIRM_KEYWORDS):
        if "no" not in t.split()[:3]:
            return {"action": "confirm"}
    # Fast path 2: bare digit "1" / "el 2" / "item 3"
    m = re.fullmatch(r"\s*(?:item\s+|el\s+)?(\d+)\s*", t)
    if m:
        idx = int(m.group(1))
        if 1 <= idx <= items_count:
            return {"action": "ask_what_to_change", "item_index": idx}
    # Fast path 3: "cancel" / "cancelar" / "dejar pendiente"
    if any(k in t for k in ("cancelar", "cancel", "dejar pendiente", "después", "luego")):
        return {"action": "cancel"}
    # LLM fallback — supports multi-correction in one message
    items_brief = "\n".join(
        f"  {i+1}. {li.get('quantity', '?'):g} {li.get('unit') or 'UND'} · {li.get('name', '')}"
        for i, li in enumerate(items)
    )
    prompt = (
        f"El usuario está respondiendo a una confirmación de cotización. Items actuales:\n"
        f"{items_brief}\n\n"
        f"Mensaje del usuario: \"{text}\"\n\n"
        f"Devolvé JSON puro (sin markdown). UNA de estas formas:\n"
        f'  {{"action":"confirm"}} — usuario aprueba (ok/sí/todo bien/perfecto/✅)\n'
        f'  {{"action":"cancel"}} — usuario quiere cancelar/dejar pendiente\n'
        f'  {{"action":"ask_what_to_change","item_index":N}} — menciona un número sin decir qué\n'
        f'  {{"action":"corrections","corrections":[{{...}},{{...}}]}} — '
        f"una o más correcciones, donde cada una es:\n"
        f'    {{"item_index":N, "field":"quantity"|"product", "new_value":<num o str>}}\n'
        f'  {{"action":"ambiguous"}} — no podés interpretar\n\n'
        f"Reglas:\n"
        f"- Si el usuario menciona un producto por nombre (\"el 6011\", \"el 309\"), buscalo en\n"
        f"  los items y devolvé el item_index correcto (1-{items_count}).\n"
        f"- Múltiples correcciones en un solo mensaje: devolvé TODAS en \"corrections\".\n"
        f"- Si el usuario corrige cantidad: field=\"quantity\", new_value es número.\n"
        f"- Si corrige producto/diámetro: field=\"product\", new_value es string.\n\n"
        f"Ejemplos:\n"
        f'  "el 6011 eran 800 y el 800 eran 200" →\n'
        f'    {{"action":"corrections","corrections":[\n'
        f'       {{"item_index":<idx_6011>, "field":"quantity", "new_value":800}},\n'
        f'       {{"item_index":<idx_800>, "field":"quantity", "new_value":200}}\n'
        f"     ]}}\n"
        f'  "1: cantidad 800" → corrections con un solo item\n'
        f'  "todo bien" → confirm\n'
        f'  "cancela / dejar pendiente" → cancel'
    )
    try:
        msg = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system="Sos un parser estructurado de respuestas. Devolvés solo JSON puro.",
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)
        if isinstance(data, dict) and "action" in data:
            return data
    except Exception as e:
        log_action("ConfirmAgent", "parse_error", str(e)[:200])
    return {"action": "ambiguous"}


def _apply_one_correction(pc: dict, correction: dict) -> tuple[bool, str]:
    """Apply a single correction (item_index + field + new_value) to pc['line_items'].
    Returns (ok, error_msg). Mutates pc in place."""
    try:
        idx = int(correction.get("item_index", 0))
    except Exception:
        return False, "item_index inválido"
    field = correction.get("field", "")
    new_value = correction.get("new_value")
    if not (1 <= idx <= len(pc["line_items"])) or field not in {"quantity", "product"}:
        return False, f"item_index {idx} fuera de rango o field inválido"
    li = pc["line_items"][idx - 1]
    if field == "quantity":
        try:
            qty = float(str(new_value).replace(",", ""))
        except Exception:
            return False, f"cantidad inválida para item {idx}: {new_value!r}"
        li["quantity"] = qty
    else:
        new_items, _ = extract_items_for_quote(f"cotice 1 de {new_value}", [])
        if not new_items:
            return False, f"no encontré '{new_value}' en el catálogo"
        ni = new_items[0]
        li["item_id"] = ni.get("item_id", li.get("item_id"))
        li["name"]    = ni.get("name", li.get("name"))
        li["rate"]    = ni.get("rate", li.get("rate"))
        li["unit"]    = ni.get("unit", li.get("unit"))
    return True, ""


def _push_corrections_to_zoho_and_resend(
    from_number: str, pc: dict, meta: dict, state: dict,
) -> None:
    """Common tail for after one or more corrections were applied to pc['line_items']:
    push to Zoho, refresh expiry, resend PDF, resend confirmation prompt."""
    upd = zoho_update_estimate(pc["estimate_id"], pc["customer_id"], pc["line_items"])
    if not upd:
        wa_send(from_number,
                f"No pude actualizar la cotización automáticamente. Por favor llame a "
                f"{ELECTRODE_REDIRECT_PHONE_HUMAN}.")
        meta.pop("pending_confirmation", None)
        save_state(state)
        return
    new_total = float(upd.get("total", 0))
    pc["total"] = new_total
    pc["awaiting_field_for_item"] = None
    from datetime import timedelta
    pc["expires_at"] = (datetime.now() + timedelta(minutes=CONFIRMATION_TTL_MIN)).isoformat()
    pdf_bytes = zoho_get_estimate_pdf(pc["estimate_id"])
    if pdf_bytes:
        wa_send_document(
            from_number, pdf_bytes,
            f"Cotizacion_SUMIN_{pc['estimate_number']}_corregida.pdf",
            f"Cotización #{pc['estimate_number']} (corregida) — SUMIN",
        )
    _send_confirmation_prompt(from_number, pc["line_items"], new_total, pc["estimate_number"])
    save_state(state)


def confirmation_agent(
    from_number: str, from_name: str, text: str, state: dict,
    *, list_reply_id: str | None = None,
) -> None:
    """Handle the user's reply to a post-PDF confirmation prompt.

    Two entry-points:
      1. List reply (user tapped a row in the interactive list) — `list_reply_id`
         is set, `text` contains the row title (we ignore it and use the id).
      2. Plain text or transcribed audio — `list_reply_id` is None, `text` has
         the user's message; the parser extracts confirm/cancel/correct(s).
    """
    meta = get_conv_meta(state, from_number)
    pc = meta.get("pending_confirmation")
    if not pc:
        return
    if _confirmation_expired(pc):
        meta.pop("pending_confirmation", None)
        save_state(state)
        return

    items_count = len(pc["line_items"])

    # ─── List-reply path: structured ID dispatch ──────────────────────────
    if list_reply_id:
        log_action("ConfirmAgent", f"list_reply={list_reply_id}", "")
        if list_reply_id == "confirm_ok":
            wa_send(from_number, "Perfecto, cotización confirmada ✅ Gracias!")
            meta.pop("pending_confirmation", None)
            save_state(state)
            return
        if list_reply_id == "cancel":
            wa_send(from_number, "Listo, cotización dejada en pendiente. Si querés retomar, escribime.")
            meta.pop("pending_confirmation", None)
            save_state(state)
            return
        m = re.fullmatch(r"correct_(\d+)", list_reply_id)
        if m:
            idx = int(m.group(1))
            if not 1 <= idx <= items_count:
                wa_send(from_number, f"Solo hay items del 1 al {items_count}.")
                return
            li = pc["line_items"][idx - 1]
            pc["awaiting_field_for_item"] = idx
            save_state(state)
            wa_send(
                from_number,
                f"*Item {idx}*: {li['quantity']:g} {li.get('unit','UND')} · {li['name']}\n\n"
                f"¿Qué corregís? Podés escribir o mandarme nota de voz. "
                f"Ej: \"cantidad 800\", \"producto 6013 1/8\".",
            )
            return
        # Unknown id — fall through to text path
        text = list_reply_id

    # ─── Text/audio path ──────────────────────────────────────────────────
    awaiting_idx = pc.get("awaiting_field_for_item")
    if awaiting_idx:
        synth = f"el {awaiting_idx}: {text}"
        action = _parse_confirmation_response(synth, pc["line_items"])
        # Force item_index for any single correction returned
        if action.get("action") == "corrections":
            for c in action.get("corrections", []):
                c["item_index"] = awaiting_idx
    else:
        action = _parse_confirmation_response(text, pc["line_items"])

    act = action.get("action", "ambiguous")
    log_action("ConfirmAgent", f"action={act}", text[:80])

    if act == "confirm":
        wa_send(from_number, "Perfecto, cotización confirmada ✅ Gracias!")
        meta.pop("pending_confirmation", None)
        save_state(state)
        return

    if act == "cancel":
        wa_send(from_number, "Listo, cotización dejada en pendiente. Si querés retomar, escribime.")
        meta.pop("pending_confirmation", None)
        save_state(state)
        return

    if act == "ask_what_to_change":
        idx = int(action["item_index"])
        if not 1 <= idx <= items_count:
            wa_send(from_number, f"Solo hay items del 1 al {items_count}. ¿Cuál querés corregir?")
            return
        li = pc["line_items"][idx - 1]
        pc["awaiting_field_for_item"] = idx
        save_state(state)
        wa_send(
            from_number,
            f"*Item {idx}*: {li['quantity']:g} {li.get('unit','UND')} · {li['name']}\n\n"
            f"¿Qué corregís? Podés escribir o mandar nota de voz. "
            f"Ej: \"cantidad 800\", \"producto 6013 1/8\".",
        )
        return

    if act == "corrections":
        corrections = action.get("corrections") or []
        if not corrections:
            wa_send(from_number, "No detecté correcciones en tu mensaje. ¿Podés indicarme qué item y qué cambio?")
            return
        applied = 0
        errors  = []
        for c in corrections:
            ok, err = _apply_one_correction(pc, c)
            if ok:
                applied += 1
            else:
                errors.append(err)
        if applied == 0:
            wa_send(
                from_number,
                "No pude aplicar las correcciones: " + "; ".join(errors[:3]) +
                f". Llame al {ELECTRODE_REDIRECT_PHONE_HUMAN} si esto sigue fallando.",
            )
            return
        msg = f"Aplicando {applied} correcci" + ("ones" if applied > 1 else "ón") + " y regenerando PDF..."
        if errors:
            msg += f"\n(Algunas no se pudieron aplicar: {'; '.join(errors[:2])})"
        wa_send(from_number, msg)
        _push_corrections_to_zoho_and_resend(from_number, pc, meta, state)
        return

    # Ambiguous
    wa_send(
        from_number,
        "No entendí. Respondé con *✅* si todo está bien, o con un número (1-"
        f"{len(pc['line_items'])}) seguido del cambio (ej: \"1: cantidad 800\").",
    )


def vision_agent(from_number: str, from_name: str, media_id: str, mime_type: str, state: dict):
    log_action("VisionAgent", "processing_image", f"{from_name} sent image")
    image_bytes = wa_download_image(media_id)
    if not image_bytes:
        return
    if is_comprobante(image_bytes, mime_type):
        payment_agent(from_number, from_name, media_id, image_bytes, state)
        return

    product_info = identify_product(image_bytes, mime_type)

    # Consumible MIG → handoff a asesor humano:
    #   1. cliente recibe acuse breve (no engage técnico),
    #   2. Daniel (+50497041381) recibe la foto + ficha de la conversación
    #      para que tome la decisión de cuál sistema cotizar y le responda
    #      al cliente directo desde su WhatsApp (o desde el console).
    # Foto que es una LISTA / SOLICITUD DE COMPRA con varios items + cantidades →
    # extraer items estructurados y enrutar a quote_agent (mismo flujo que si el
    # cliente hubiera escrito "cotice 800 lbs de 6011, 800 lbs de 7018, ...").
    # Para usuarios trusted (Eduardo, Daniel, tablets) el bypass del v8 lleva a
    # estimate + PDF directo. Para clientes externos cae en /approvals.
    order = try_extract_order_from_image(image_bytes, mime_type)
    if order:
        items = order["items"]
        excluded = order.get("excluded", [])

        def _qty_num(it: dict) -> float:
            """Coerce quantity to float — the LLM occasionally returns strings."""
            try:
                return float(it.get("quantity", 1) or 1)
            except (TypeError, ValueError):
                return 1.0

        items_text = "\n".join(
            f"  • {_qty_num(it):g} {it.get('unit') or ''} {it['product']}".replace("  ", " ").rstrip()
            for it in items
        )
        ack = f"Recibimos su lista 📋 Encontramos:\n{items_text}"
        if excluded:
            excluded_text = "\n".join(
                f"  • {e.get('product','?')} ({e.get('reason') or 'excluido'})"
                for e in excluded
            )
            ack += f"\n\nExcluidos (no se cotizan):\n{excluded_text}"
        ack += "\n\nGenerando cotización..."
        wa_send(from_number, ack)
        # Construir texto sintético que quote_agent puede parsear con su lógica
        # actual (cotice X de Y, Z de W, ...).
        synth_parts = []
        for it in items:
            qty = _qty_num(it)
            unit = it.get("unit") or ""
            prod = it["product"]
            piece = f"{qty:g}"
            if unit:
                piece += f" {unit}"
            piece += f" de {prod}"
            synth_parts.append(piece)
        synthetic_text = "cotice " + ", ".join(synth_parts)
        log_action(
            "VisionAgent",
            "order_extracted",
            f"items={len(items)} excluded={len(excluded)} from={from_number}",
        )
        quote_agent(from_number, from_name, synthetic_text, state)
        return

    if _looks_like_mig_consumable(product_info):
        log_action("VisionAgent", "mig_consumable_handoff", from_number)
        wa_send(
            from_number,
            "Recibimos su foto 👍 En breve uno de nuestros asesores le confirma la "
            "referencia exacta de ese consumible MIG.",
        )
        # Reenvío a Daniel para resolución manual.
        try:
            wa_forward_image(media_id, DANIEL_PHONE)
            handoff_note = (
                "📷 *Foto de consumible MIG recibida*\n"
                f"Cliente: {from_name or '(sin nombre)'} ({from_number})\n"
                f"Identificación AI: {product_info[:300]}\n\n"
                "Sistemas en stock (referencia interna):\n"
                "1️⃣ Miller serie 169 (4 piezas)\n"
                "2️⃣ M25 / Magnum 200 (3 piezas)\n"
                "3️⃣ M250 / Magnum 250 (3 piezas)\n"
                "4️⃣ MDX Miller nuevo (3 piezas)\n"
                "5️⃣ Tipo Binzel (3 piezas)\n"
                "6️⃣ Serie HD (3 piezas)\n\n"
                "Respondele al cliente directo. (En v11 vas a poder responder "
                "el número aquí y el bot le manda lista vague + total al cliente.)"
            )
            wa_send(DANIEL_PHONE, handoff_note)
            log_action("VisionAgent", "mig_handoff_forwarded_to_daniel", from_number)
        except Exception as e:
            log_action("VisionAgent", "mig_handoff_forward_error", str(e)[:200])
        return

    # Para electrodos comunes (6011/7018/309-16/etc.) preguntamos en LIBRAS — se venden
    # por peso. Solo el electrodo de TUNGSTENO TIG se vende por unidad. El resto
    # (caretas, discos, alambre, etc.) sigue en unidades como default.
    qty_question = _quantity_question_for_product(product_info)
    response = f"Identificamos el producto:\n\n{product_info}\n\n{qty_question}"
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
    "cotización", "cotizacion", "cotiza",   # "cotiza" cubre "cotiza el 6011", "cotizame", etc.
    "cotizar", "presupuesto",
    "estimación", "estimacion", "precio formal", "factura proforma",
    "me pueden cotizar", "me das una coti", "necesito una coti",
    "quiero una coti", "me hacen una coti", "pueden cotizar",
    "hágame cotización", "hagame cotizacion", "hágame una cotización",
    # Variantes informales muy comunes en Honduras (cotice/cotíceme/cotizeme):
    "cotice", "cotíce", "cotíceme", "coticeme", "cotizeme", "cotízeme",
    "me cotice", "me cotíce",
    # Forma corta "una coti" / "la coti":
    "una coti", "la coti",
]

# Regex para detectar patrones cantidad + unidad + producto típicos de SUMIN.
# Ej: "100 lbs de 6011", "5 cajas de 7018", "20 und de discos", "2 rollos de mig"
_QTY_PRODUCT_RE = re.compile(
    r"\b(\d{1,5})\s*(lb|lbs|libra|libras|kg|kilo|kilos|caja|cajas|"
    r"rollo|rollos|unidad|unidades|und|pza|pzas|piezas|metro|metros|"
    r"galon|galón|galones|disco|discos|par|pares)\b",
    re.IGNORECASE,
)


def detect_quote_request(text: str) -> bool:
    """Decide si el mensaje del cliente debe enrutarse al agente de cotización.

    Activa cotización si:
      (a) usa una palabra-gatillo explícita (cotíceme, cotización, etc.), o
      (b) menciona una cantidad + unidad ("100 lbs de 6011", "5 cajas de 7018"),
          ya que en Honduras los clientes piden cotización así sin la palabra.
    """
    t = text.lower()
    if any(kw in t for kw in QUOTE_TRIGGERS):
        return True
    # Patrón cantidad+unidad: implica intención de comprar/cotizar varios productos
    if _QTY_PRODUCT_RE.search(t):
        return True
    return False


def _explicit_new_quote_request(text: str) -> bool:
    """Stricter than `detect_quote_request` — TRUE only for explicit trigger
    phrases ("cotice", "cotización", "presupuesto", etc.), NOT for quantity+unit
    patterns alone. Used during a `pending_confirmation` window to decide
    whether the user is abandoning the prior cotización to start a new one,
    versus just correcting an item with a phrase like "el 6011 eran 800 lbs".
    """
    return any(kw in (text or "").lower() for kw in QUOTE_TRIGGERS)


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


_LEGAL_FORM_TOKENS = {
    "sa", "s.a.", "s.a", "cv", "c.v.", "c.v", "srl", "s.r.l.", "s.r.l",
    "ltda", "ltd", "inc", "co", "sas", "spa",
}
_STOP_TOKENS = {
    "de", "del", "la", "el", "los", "las", "y", "e", "por", "con", "para",
    "the", "of", "and",
}
_NORMALIZE_RE = re.compile(r"[^\w\s]+", re.UNICODE)


def _normalize_for_match(s: str) -> str:
    """Normalize a name for token-based comparison."""
    s = (s or "").lower()
    s = _NORMALIZE_RE.sub(" ", s)
    return " ".join(s.split())


def _significant_tokens(s: str) -> set[str]:
    """Tokens we use to compare names: lowercase, no punct, drop legal forms
    and Spanish stop-words. 'AZUCARERA DEL NORTE' → {'azucarera', 'norte'}."""
    norm = _normalize_for_match(s)
    return {
        t for t in norm.split()
        if len(t) >= 2 and t not in _LEGAL_FORM_TOKENS and t not in _STOP_TOKENS
    }


def _match_score(query_tokens: set[str], candidate: str) -> float:
    """Score a candidate name 0.0–1.0 by significant-token overlap."""
    if not query_tokens:
        return 0.0
    cand_tokens = _significant_tokens(candidate)
    if not cand_tokens:
        return 0.0
    overlap = query_tokens & cand_tokens
    if not overlap:
        return 0.0
    # Score: fraction of query tokens that appear in candidate
    return len(overlap) / len(query_tokens)


def zoho_get_or_create_customer(name: str, phone: str) -> str | None:
    """Return contact_id for the given customer name.

    Matching strategy (in order):
      1) Exact case-insensitive match on contact_name.
      2) Fuzzy match: rank candidates by significant-token overlap against the
         input name. Stop-words ("de", "la", "y") and legal forms ("S.A.",
         "C.V.", "SRL") are stripped before comparison. Pick the best match if
         score ≥ 0.6 AND it's the unique top candidate.
      3) Otherwise create a new contact.

    The previous version used `contact_name_contains` (which doesn't always
    work in Zoho's API) and fell back to creating duplicates whenever the
    user's reply was a slightly different spelling of an existing customer
    (e.g. "Enercom" vs "ENERCOM S.A.").
    """
    token = get_zoho_access_token()
    if not token or not ZOHO_ORG_ID:
        return None
    clean_name = (name or "").strip()
    if not clean_name:
        return None
    query_tokens = _significant_tokens(clean_name)
    # If the name reduced to no significant tokens (e.g. only legal forms),
    # fall back to a less-stripped token set so search isn't empty.
    if not query_tokens:
        query_tokens = set(_normalize_for_match(clean_name).split())

    # 1) Search using `search_text` (more reliable than `contact_name_contains`).
    # Use the FIRST significant token as the primary search anchor; we'll rank
    # candidates ourselves below.
    primary_token = sorted(query_tokens, key=len, reverse=True)[0] if query_tokens else clean_name
    try:
        r = httpx.get(
            "https://www.zohoapis.com/books/v3/contacts",
            params={
                "organization_id": ZOHO_ORG_ID,
                "search_text": primary_token,
                "per_page": 50,
            },
            headers={"Authorization": f"Zoho-oauthtoken {token}"},
            timeout=10,
        )
        contacts = r.json().get("contacts", []) if r.status_code == 200 else []

        # 1a) Exact case-insensitive match on contact_name
        clean_lower = clean_name.lower()
        for c in contacts:
            if c.get("contact_name", "").strip().lower() == clean_lower:
                cid = c.get("contact_id")
                log_action("ZohoAPI", "customer_match_exact", f"{clean_name} → {cid}")
                return cid

        # 1b) Fuzzy ranked match on significant tokens
        scored = []
        for c in contacts:
            cname = c.get("contact_name", "")
            if not cname:
                continue
            score = _match_score(query_tokens, cname)
            if score > 0:
                scored.append((score, cname, c.get("contact_id")))
        scored.sort(reverse=True)

        if scored:
            top_score, top_name, top_cid = scored[0]
            # Require min score 0.6 (i.e. ≥60% of query tokens are in candidate).
            # AND a clear gap from the runner-up — even for "perfect" 1.0
            # matches, if there's a tie ("Azucarera" → AZUCARERA CHOLUTECA AND
            # AZUCARERA DEL NORTE both score 1.0), we should NOT auto-pick.
            second_score = scored[1][0] if len(scored) > 1 else 0.0
            gap = top_score - second_score
            confident = top_score >= 0.6 and gap >= 0.2
            # Tighter rule for very-high scores: still require gap ≥ 0.1
            if top_score >= 0.9 and gap >= 0.1:
                confident = True
            if confident:
                log_action(
                    "ZohoAPI", "customer_match_fuzzy",
                    f"'{clean_name}' → '{top_name}' (score={top_score:.2f}, runner_up={second_score:.2f})",
                )
                return top_cid
            else:
                log_action(
                    "ZohoAPI", "customer_match_ambiguous",
                    f"'{clean_name}': top='{top_name}' ({top_score:.2f}) vs runner_up ({second_score:.2f}) — creating new",
                )
        else:
            log_action("ZohoAPI", "customer_no_search_results", f"'{clean_name}' (token={primary_token})")
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


def zoho_update_estimate(estimate_id: str, customer_id: str, line_items: list[dict]) -> dict | None:
    """Update an existing Zoho estimate's line_items. Returns the updated estimate
    on success, None on failure. Used by the post-quote confirmation flow when a
    trusted user corrects the items before final delivery."""
    token = get_zoho_access_token()
    if not token or not ZOHO_ORG_ID or not estimate_id:
        return None
    formatted = [
        {"item_id": li["item_id"], "name": li["name"], "quantity": li["quantity"],
         "rate": li["rate"], "unit": li.get("unit", "")}
        for li in line_items
    ]
    payload = {
        "customer_id": customer_id,
        "line_items":  formatted,
    }
    try:
        r = httpx.put(
            f"https://www.zohoapis.com/books/v3/estimates/{estimate_id}",
            params={"organization_id": ZOHO_ORG_ID},
            headers={"Authorization": f"Zoho-oauthtoken {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
        if r.status_code == 200:
            est = r.json().get("estimate", {})
            log_action("ZohoAPI", "estimate_updated",
                       f"#{est.get('estimate_number')} items={len(formatted)}")
            return est
        log_action("ZohoAPI", "estimate_update_error",
                   f"status={r.status_code} body={r.text[:300]}")
    except Exception as e:
        log_action("ZohoAPI", "estimate_update_exception", str(e))
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


def _parse_quote_name_response_open(text: str) -> str:
    """Parse customer's reply when we asked '¿A nombre de quién?' WITHOUT
    suggesting any name. Returns the resolved customer name.

    Robust to filler / connector phrases:
      - "sin nombre" / "consumidor" / similar → Consumidor Final
      - "Azucarera del Norte"                 → Azucarera del Norte
      - "es me la genera a nombre de Azucarera del Norte" → Azucarera del Norte
      - "para Enercom porfa"                  → Enercom
      - "hazla a nombre de Constructora García S.A." → Constructora García S.A.

    Strategy: fast paths for trivial cases (empty / "sí" / "sin nombre"), then
    take the literal if it looks like a clean name, otherwise fall back to a
    Haiku LLM extraction.
    """
    t = (text or "").strip().rstrip(".!?¡¿")
    if not t:
        return "Consumidor Final"
    t_low = t.lower()

    # Fast path 1: explicit "yes" / generic affirm → no name given
    if t_low in _QUOTE_AFFIRM_WORDS or t_low in {"yes", "ok", "okay", "claro", "dale"}:
        return "Consumidor Final"

    # Fast path 2: explicit "sin nombre" / "consumidor"
    if (
        t_low in _QUOTE_NONAME_WORDS
        or t_low.startswith("consumidor")
        or t_low.startswith("sin nombre")
        or t_low == "sin"
        or t_low == "ninguno"
    ):
        return "Consumidor Final"

    # Fast path 3: clean short reply (≤6 tokens, no filler verbs) → take as-is
    tokens = t.split()
    has_filler = any(
        w in t_low for w in (
            "genera", "genere", "factur", "hago", "haga", "haz", "hace",
            "ponela", "ponele", "ponga", "se hace", "es para",
            "me la", "esta cot", "esa cot", "la cot",
        )
    )
    if len(tokens) <= 6 and not has_filler:
        # Strip trivial prefixes
        for prefix in (
            "a nombre de ", "a nombre del ", "para la empresa ", "para ",
            "facturar a ", "facturar para ", "a la empresa ",
            "cotizar a ", "cotizar para ", "es para ", "es de ",
        ):
            if t_low.startswith(prefix):
                t = t[len(prefix):].strip()
                break
        cleaned = t.strip(" \"\\\'.,;:")
        if cleaned:
            return cleaned[:80]

    # Slow path: Haiku LLM extraction for natural-language replies.
    try:
        msg = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            system=(
                "Extraés el nombre del cliente o empresa de la respuesta del usuario "
                "a la pregunta '¿A nombre de quién genero la cotización?'. Devolvés "
                "EXCLUSIVAMENTE el nombre, sin frases, sin prefijos, sin signos de "
                "puntuación al final. Si el usuario dice variantes de 'sin nombre' / "
                "'consumidor final' / 'no importa', devolvés literalmente: Consumidor Final"
            ),
            messages=[{"role": "user", "content": [
                {"type": "text", "text": (
                    "Respuesta del usuario: \"" + text + "\"\n\n"
                    "Ejemplos:\n"
                    "  \"Azucarera del Norte\" → Azucarera del Norte\n"
                    "  \"es me la genera a nombre de Azucarera del Norte\" → Azucarera del Norte\n"
                    "  \"para Enercom porfa\" → Enercom\n"
                    "  \"hazla a nombre de Constructora García S.A.\" → Constructora García S.A.\n"
                    "  \"esta la genera a nombre de aceites y derivados\" → Aceites y Derivados\n"
                    "  \"sin nombre\" → Consumidor Final\n\n"
                    "Devolvé SOLO el nombre extraído (sin comillas, sin prefijos):"
                )}
            ]}],
        )
        extracted = msg.content[0].text.strip().strip("\"'.,;:")
        if not extracted:
            return "Consumidor Final"
        log_action("QuoteAgent", "name_extracted_via_llm", f"'{text[:60]}' → '{extracted[:60]}'")
        return extracted[:80]
    except Exception as e:
        log_action("QuoteAgent", "name_extract_error", str(e)[:200])
        # Fallback: last-resort literal cleanup
        return t.strip(" \"\\\'.,;:")[:80] or "Consumidor Final"


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
        # Open name parsing — no "suggested" fallback.
        customer_name = _parse_quote_name_response_open(text)
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
            # IMPORTANT: do NOT suggest the WhatsApp profile name. Many customers
            # use nicknames, emojis or informal handles in their WhatsApp profile
            # ("DP", "el diablito", etc.) — that should NEVER end up on a formal
            # quote. We ask openly and default to Consumidor Final on opt-out.
            meta["pending_quote"] = {
                "items": line_items,
                "not_found": not_found,
                "unit_mismatches": unit_mismatches,
                "asked_at": datetime.now().isoformat(),
            }
            save_state(state)
            items_summary = ", ".join((li.get("name", "") or "")[:35] for li in line_items)
            wa_send(
                from_number,
                f"¡Con gusto le preparo la cotización! 📋\n\n"
                f"Identifiqué: *{items_summary}*\n\n"
                f"¿A nombre de quién o de qué empresa le genero la cotización? "
                f"(Si prefiere, escriba 'sin nombre' y la hago a nombre de Consumidor Final)"
            )
            log_action("QuoteAgent", "asked_customer_name_open", "")
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

    # ─── APPROVAL MODE ─────────────────────────────────────────────────
    # If approval mode is on, register the quote in the console (status =
    # pending_approval) and send the customer a holding message. The vendor
    # approves from /approvals, which triggers the formal quote send.
    #
    # Exception: trusted internal SUMIN numbers (founders, store tablets, key
    # employees) skip the gate and fall through to direct-send below.
    if QUOTE_APPROVAL_MODE and not is_trusted_number(from_number):
        notes_for_console = f"Cotización Zoho EST {est_number} — a nombre de {customer_name}"
        if not_found:
            notes_for_console += f". Items no encontrados: {', '.join(not_found)}"
        if unit_mismatches:
            notes_for_console += f". Mismatches de unidad: {', '.join(unit_mismatches)}"
        ok = submit_pending_quote_to_console(
            phone=from_number,
            customer_name=customer_name,
            line_items=line_items,
            zoho_estimate_id=est_id,
            estimate_number=est_number,
            notes=notes_for_console,
        )
        items_summary = ", ".join(li.get("name", "")[:30] for li in line_items[:3])
        more = f" (+{len(line_items) - 3} más)" if len(line_items) > 3 else ""
        if ok:
            wa_send(
                from_number,
                f"Buen día! Estamos preparando su cotización formal "
                f"({items_summary}{more}). En breve un miembro de nuestro "
                f"equipo se la enviará por este mismo medio. Gracias por su "
                f"paciencia 🙏",
            )
            log_action("QuoteAgent", "submitted_for_approval",
                       f"est={est_number} total=L{total:,.2f} → {customer_name}")
        else:
            # Console down — fall back to direct send so the customer is not stuck.
            log_action("QuoteAgent", "approval_submit_failed_fallback_direct", est_number)
            lines_txt = "\n".join(
                f"  • {li['quantity']} x {li['name']} — L{li['rate'] * li['quantity'] * 1.15:,.2f} c/ISV"
                for li in line_items
            )
            wa_send(
                from_number,
                f"✅ *Cotización #{est_number}*\n"
                f"A nombre de: {customer_name}\n\n"
                f"{lines_txt}\n\n"
                f"*Total (ISV incluido):* L{total:,.2f}",
            )
        # Skip PDF send + history append below — done.
        meta["last_active"] = datetime.now().isoformat()
        meta["last_msg"] = text[:80]
        history.append({"role": "user", "content": text})
        history.append({"role": "assistant",
                        "content": f"[Cotización #{est_number} pendiente de aprobación humana, total: L{total:,.2f}]"})
        state["conversations"][from_number] = history[-20:]
        save_state(state)
        return

    # ─── DIRECT MODE (legacy, only when QUOTE_APPROVAL_MODE=off) ────────
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

    # Post-confirmación: snapshot del quote y prompt al usuario para validar/corregir
    # antes de cerrar. Si algo está mal (cantidades mal leídas en OCR, producto
    # incorrecto), corrige acá y el bot actualiza Zoho + manda PDF nuevo.
    zoho_customer_id = estimate.get("customer_id", "") if isinstance(estimate, dict) else ""
    if zoho_customer_id:
        _save_pending_confirmation(
            meta,
            estimate_id=est_id,
            estimate_number=est_number,
            customer_id=zoho_customer_id,
            customer_name=customer_name,
            line_items=line_items,
            total=total,
        )
        _send_confirmation_prompt(from_number, line_items, total, est_number)

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

    # Mirror inbound to the console bandeja (fire-and-forget, never blocks bot).
    if msg_type == "text":
        _inbound_text = message_data.get("text", {}).get("body", "")
        forward_to_console("inbound", from_number, from_name, _inbound_text)
    elif msg_type == "image":
        forward_to_console("inbound", from_number, from_name, "[imagen]", "image")
    elif msg_type == "audio":
        forward_to_console("inbound", from_number, from_name, "[audio]", "audio")
    elif msg_type == "document":
        forward_to_console("inbound", from_number, from_name, "[documento]", "document")

    # If a human took over this conversation in the console, do NOT respond.
    if is_conversation_paused(from_number):
        log_action("Orchestrator", "skipped_bot_paused", from_number)
        return

    if fulfillment_agent(message_data, state):
        return

    if msg_type == "image":
        media_id  = message_data.get("image", {}).get("id", "")
        mime_type = message_data.get("image", {}).get("mime_type", "image/jpeg")
        vision_agent(from_number, from_name, media_id, mime_type, state)
        return

    # ─── INTERACTIVE (list-reply taps from confirmation prompt) ──────────────
    if msg_type == "interactive":
        interactive = message_data.get("interactive", {}) or {}
        if interactive.get("type") == "list_reply":
            list_reply = interactive.get("list_reply", {}) or {}
            row_id = list_reply.get("id", "")
            row_title = list_reply.get("title", "")
            log_action("Orchestrator", "list_reply_received", f"id={row_id}")
            _meta_check = get_conv_meta(state, from_number)
            if _meta_check.get("pending_confirmation"):
                confirmation_agent(from_number, from_name, row_title, state, list_reply_id=row_id)
            return
        # Other interactive types (button_reply etc.) — fall through silently for now
        return

    # ─── AUDIO (voice notes — transcribe via Whisper, route as text) ─────────
    if msg_type == "audio":
        audio_obj = message_data.get("audio", {}) or {}
        media_id  = audio_obj.get("id", "")
        mime_type = audio_obj.get("mime_type", "audio/ogg")
        if not media_id:
            return
        log_action("Orchestrator", "audio_received", f"media={media_id} mime={mime_type}")
        audio_bytes = wa_download_audio(media_id)
        transcript = transcribe_audio_whisper(audio_bytes, mime_type) if audio_bytes else ""
        if not transcript:
            wa_send(
                from_number,
                "No pude entender la nota de voz 🎙️ ¿Podés escribirlo o reenviar el audio?",
            )
            return
        log_action("Orchestrator", "audio_transcript", transcript[:120])
        # Route the transcript as if it were a text message — re-enter the text branch
        # by mutating message_data and falling through.
        message_data["type"] = "text"
        message_data["text"] = {"body": transcript}
        msg_type = "text"
        # fall through to the text branch below

    if msg_type == "text":
        text = message_data.get("text", {}).get("body", "")

        if re.fullmatch(r"[a-zA-Z]{2,5}\d{4,8}", text.strip()):
            log_action("Orchestrator", "skipped_zoho_code", text)
            return

        # If a quote was just sent and is awaiting confirmation/correction by
        # the user, route there first — UNLESS the user is starting a brand new
        # cotización with an EXPLICIT trigger word (cotice / cotización / etc).
        # We do NOT abandon on quantity-only patterns ("el 6011 eran 800 lbs"),
        # because those are corrections to the existing cotización, not new quotes.
        _meta_check = get_conv_meta(state, from_number)
        _pc = _meta_check.get("pending_confirmation")
        if _pc and not _confirmation_expired(_pc) and not _explicit_new_quote_request(text):
            log_action("Orchestrator", "routed_pending_confirmation", from_number)
            confirmation_agent(from_number, from_name, text, state)
            return
        elif _pc and _explicit_new_quote_request(text):
            # User abandons the previous confirmation by starting a new quote
            _meta_check.pop("pending_confirmation", None)
            save_state(state)

        # If there's a pending quote awaiting customer-name confirmation,
        # route this message to quote_agent regardless of detect_quote_request.
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
