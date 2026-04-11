""" SUMIN / Proenco WhatsApp Business Bot
Multi-agent system: SalesAgent, ProencoAgent, VisionAgent, PaymentAgent, FulfillmentAgent
Structure: SUMIN and Proenco share one WhatsApp number. Routing happens on first contact.
To split into two separate bots later: each business section is clearly delimited.
"""
import os, json, re, httpx, base64, time
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

# ─── BUSINESS NUMBERS ────────────────────────────────────────────────────────
ALDO_NUMBER      = "50497096965"   # Ing. Aldo Villafranca - Proenco

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

═══════════════════════════════════════
FLUJO SEGÚN TIPO DE CONSULTA
═══════════════════════════════════════

1. CONSULTA GENÉRICA ("Hola, quiero información" / "Quiero más información"):
   Responder: "Hola buen día! Para orientarle mejor, ¿qué producto está buscando?"
   Luego listar las 3 categorías:
   - Electrodos (¿qué tipo y diámetro necesita?)
   - Alambre para soldar — MIG sin gas o con gas
   - Equipo de protección — caretas, guantes, chaquetas, kits

2. ELECTRODOS:
   Preguntar: diámetro y tamaño de caja (10 lbs o 50 lbs).
   Precios electrodo 6011:
     - Caja 10 lbs: L517.50
     - Caja 50 lbs: L2,587.50
   Diámetros disponibles SOLO: 3/32", 1/8", 5/32". NO hay 1/16" ni 3/16".
   Para otros electrodos (7018, etc.) sin precio conocido: "No tengo ese precio aquí, puede llamarnos o pasar por tienda."

3. CARETAS / EQUIPO DE PROTECCIÓN:
   Preguntar primero: "¿La ocupa para uso pesado o uso básico?"
   Luego presentar opciones según necesidad:

   CARETAS DISPONIBLES:
   - Careta básica con respirador: L632.50
   - Careta Pro 4.0 (para humos de soldadura, uso intensivo): L2,530.00
   - Careta Panorámica (visión amplia + respirador): L4,300.00
   - Careta PAPR (sistema motorizado, máxima protección): L13,225.00

   OTROS EPP:
   - Delantal de cuero: L632.50
   - SafeCut Defender 450 (chaqueta/kit de corte): L13,383.70
   - Guantes, chaquetas de cuero: "Puede pasar por tienda o llamarnos para ver existencias y precios."

   Ofrecer siempre: "Si quiere le mando foto o video del producto."

4. MICROALAMBRE / ALAMBRE MIG:
   Preguntar: ¿con gas o sin gas? ¿qué diámetro? ¿marca actual?
   Si el cliente tiene el producto actual: pedirle foto para identificar la referencia correcta.
   Sin precio conocido: dar precio en tienda o pedir que llame.

5. OXICORTE / EQUIPO DE GAS:
   Kits disponibles — ofrecer enviar foto + descripción + precio mensual.
   "Le mando foto del kit para que lo vea."

6. UBICACIÓN / DIRECCIÓN:
   📍 San Pedro Sula: 1ra calle, entre 1ra y 2da avenida, Edificio Metrocentro, Local #3
   https://maps.app.goo.gl/KUH7HU2idddQXCSPA
   📍 Tegucigalpa (Comayagüela): 8 calle, entre 3ra y 4ta avenida, frente a cafetería Macao, a la par del nuevo estacionamiento del Hospital Policlínica
   https://maps.app.goo.gl/2iNJW6wMDtKn68cg8
   Preguntar: "¿En cuál ciudad le gustaría visitarnos?"

7. ENVÍOS:
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
- NUNCA inventes precios. Si no lo sabés: "No tengo ese precio aquí ahora mismo, puede llamarnos o pasar por tienda."
- NO prometas enviar cotización formal si no podés.
- Si el cliente pregunta algo que no vendemos, díselo directamente sin rodeos.
"""

# ════════════════════════════════════════════════════════════════════════════════
# ─── PROENCO — SYSTEM PROMPT ─────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════════
PROENCO_SYSTEM = """Eres el asistente virtual de Proenco, empresa del Ing. Aldo Villafranca (Ingeniero Civil especialista en techos) en Honduras.

SOBRE PROENCO:
- Nos especializamos en cambio completo de techos con sistema Standing Seam (techo de junta alzada)
- NO vendemos lámina suelta ni somos una ferretería — hacemos el proyecto completo de principio a fin
- Somos los ÚNICOS en Honduras que ofrecen garantía escrita contra goteras
- Tenemos nuestras propias máquinas troqueladoras (formamos los paneles en sitio)
- Contamos con grúa especializada para levantamiento de techos grandes
- Personal capacitado con técnica brasileña de alto nivel
- Servicio premium: más costoso que la competencia, pero la calidad y garantía no tienen comparación
- Atendemos casas, edificios, locales comerciales y naves industriales

EL SISTEMA STANDING SEAM:
- Paneles de metal con costuras alzadas que se interconectan — sin tornillos expuestos al exterior
- Sin tornillos expuestos = sin perforaciones = elimina la causa número 1 de goteras
- Clips flotantes permiten la expansión/contracción térmica del metal sin dañar el techo
- Vida útil de 50-70 años con mantenimiento básico
- Radicalmente diferente y superior a la lámina acanalada tradicional

SI CONFUNDEN CON VENTA DE LÁMINA (como Alutech u otras):
"No vendemos lámina por separado. Nosotros hacemos el cambio completo del techo — desde la fabricación de nuestros propios paneles hasta la instalación y la garantía. Es un servicio completo."

PROCESO CON EL CLIENTE:
1. Visita de levantamiento gratuita: Ing. Aldo evalúa el techo actual y las condiciones estructurales
2. Cotización personalizada (no hay precio fijo — depende del área, acceso, tipo de estructura)
3. Ejecución con equipo y maquinaria especializada propia
4. Garantía escrita contra goteras

PRECIOS:
No existe precio base — cada proyecto es único. La visita de levantamiento es gratuita y sin compromiso.
Nunca inventes precios. Si preguntan: "Para darle un precio exacto necesitamos hacer la visita de levantamiento — es sin costo y sin compromiso."

ESTILO DE RESPUESTA:
- Saluda con "Hola buen día" o "buen día"
- Tono profesional, cálido y directo
- Máximo 3-4 líneas por mensaje
- Sin emojis excesivos
- Una pregunta a la vez

CALIFICACIÓN DEL CLIENTE — PREGUNTAR UNO A LA VEZ:
Cuando llegue un cliente interesado, recoge esta información de forma natural en la conversación:
1. Tipo de propiedad: ¿casa, edificio/local comercial, nave industrial, otro?
2. Zona o ciudad donde está la propiedad
3. Motivo del cambio: ¿goteras frecuentes, techo muy viejo, renovación, construcción nueva?
4. ¿Cuándo fue el último cambio o instalación del techo actual?
5. ¿Le han dado mantenimiento al techo? ¿Qué tipo?
6. Nombre completo del contacto

SOBRE LAS GOTERAS:
Si el cliente menciona goteras, enfatizá: el Standing Seam elimina las goteras de raíz porque no tiene tornillos expuestos. Es la solución definitiva, no un parche.

CUANDO TENGAS TODA LA INFO:
Confirmá la información y decí: "Perfecto, le voy a pasar sus datos al Ing. Aldo para que le contacte y agende la visita de levantamiento. Es sin costo y sin compromiso."
"""

# ─── ROUTING ─────────────────────────────────────────────────────────────────
ROUTING_QUESTION = """Hola buen día! Tenemos dos líneas de servicio:

1️⃣ *SUMIN* — Productos de soldadura y equipo de protección personal (EPP)
2️⃣ *Proenco* — Cambio completo de techo (Standing Seam, con garantía)

¿En cuál de los dos le podemos ayudar?"""

SUMIN_KEYWORDS  = ['soldar', 'soldadura', 'electrodo', 'mig', 'careta', 'guante',
                   'chaqueta', 'alambre', 'oxicorte', 'sumin', 'epp', 'protección',
                   'delantal', 'escudo', 'varilla', 'disco', 'lija', 'esmeril']
PROENCO_KEYWORDS = ['techo', 'lámina', 'gotera', 'proenco', 'standing', 'nave',
                    'bodega', 'cubierta', 'zinc', 'impermeabilizar', 'aislamiento',
                    'lámina de techo', 'cambio de techo', 'goteras', 'lluvia']

def classify_business(text: str):
    """Returns 'sumin', 'proenco', or None if ambiguous."""
    t = text.lower().strip()
    is_sumin   = any(k in t for k in SUMIN_KEYWORDS)  or t in ['1', '1️⃣']
    is_proenco = any(k in t for k in PROENCO_KEYWORDS) or t in ['2', '2️⃣']
    if is_sumin and not is_proenco:
        return 'sumin'
    if is_proenco and not is_sumin:
        return 'proenco'
    return None

def get_conv_meta(state: dict, conv_key: str) -> dict:
    """Get or initialize per-conversation metadata."""
    if 'conv_meta' not in state:
        state['conv_meta'] = {}
    if conv_key not in state['conv_meta']:
        state['conv_meta'][conv_key] = {'business': None, 'lead_sent': False}
    return state['conv_meta'][conv_key]

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

def claude_respond(system: str, conversation_history: list, new_message: str) -> str:
    messages = conversation_history[-10:] + [{"role": "user", "content": new_message}]
    msg = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        system=system,
        messages=messages
    )
    return msg.content[0].text

# ════════════════════════════════════════════════════════════════════════════════
# ─── SUMIN AGENTS ────────────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════════

def sales_agent(from_number: str, from_name: str, text: str, state: dict):
    """SUMIN: Handle sales inquiries."""
    log_action("SalesAgent", "processing", f"{from_name}: {text}")
    if from_number not in state["conversations"]:
        state["conversations"][from_number] = []
    history = state["conversations"][from_number]
    response = claude_respond(SUMIN_SYSTEM, history, text)
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": response})
    state["conversations"][from_number] = history[-20:]
    wa_send(from_number, response)
    log_action("SalesAgent", "sent_response", response[:100])
    save_state(state)

def vision_agent(from_number: str, from_name: str, media_id: str, mime_type: str, state: dict):
    """SUMIN: Handle image messages."""
    log_action("VisionAgent", "processing_image", f"{from_name} sent image")
    image_bytes = wa_download_image(media_id)
    if not image_bytes:
        return
    if is_comprobante(image_bytes, mime_type):
        payment_agent(from_number, from_name, media_id, image_bytes, state)
    else:
        product_info = identify_product(image_bytes, mime_type)
        response = f"Identificamos el producto:\n\n{product_info}\n\n¿Cuántas unidades necesita y para qué ciudad es el envío?"
        wa_send(from_number, response)

def payment_agent(from_number: str, from_name: str, media_id: str, image_bytes: bytes, state: dict):
    """SUMIN: Handle payment comprobante."""
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
    """SUMIN: Monitor messages from Oficina SPS."""
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
# ─── PROENCO AGENTS ──────────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════════

def extract_proenco_lead(history: list) -> dict | None:
    """Use Haiku to check if enough lead info has been collected."""
    if len(history) < 6:
        return None
    conv_text = "\n".join(
        f"{'Cliente' if m['role']=='user' else 'Bot'}: {m['content']}"
        for m in history[-16:]
    )
    msg = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": f"""De esta conversación de WhatsApp sobre cambio de techo, extrae la información del lead.

Necesito al menos: nombre del contacto, zona/ciudad, tipo de propiedad, y motivo del cambio.
Si falta alguno de esos 4 datos clave, responde exactamente: NO_READY

Si tienes suficiente información, responde SOLO el JSON sin nada más:
{{"nombre": "...", "zona": "...", "propiedad": "...", "motivo": "...", "ultimo_cambio": "...", "mantenimiento": "..."}}

Conversación:
{conv_text}"""}]
    )
    result = msg.content[0].text.strip()
    if "NO_READY" in result:
        return None
    try:
        match = re.search(r'\{.*\}', result, re.DOTALL)
        if match:
            return json.loads(match.group())
    except:
        pass
    return None

def notify_aldo(lead: dict, client_number: str, client_name: str):
    """Send lead notification to Ing. Aldo via WhatsApp."""
    nombre    = lead.get('nombre', client_name) or client_name
    zona      = lead.get('zona', 'Por confirmar')
    propiedad = lead.get('propiedad', 'Por confirmar')
    motivo    = lead.get('motivo', 'Por confirmar')
    ultimo    = lead.get('ultimo_cambio', '-')
    mant      = lead.get('mantenimiento', '-')

    msg = (
        f"🏗️ *Nuevo lead — Proenco*\n\n"
        f"👤 *Cliente:* {nombre}\n"
        f"📞 *Número:* +{client_number}\n"
        f"📍 *Zona:* {zona}\n"
        f"🏠 *Propiedad:* {propiedad}\n"
        f"❓ *Motivo del cambio:* {motivo}\n"
        f"🕐 *Último cambio:* {ultimo}\n"
        f"🔧 *Mantenimiento previo:* {mant}\n\n"
        f"El cliente espera que le contacte para agendar la visita de levantamiento."
    )
    wa_send(ALDO_NUMBER, msg)
    log_action("ProencoAgent", "notified_aldo", f"Lead enviado: {nombre} | {zona}")

def proenco_agent(from_number: str, from_name: str, text: str, state: dict):
    """Proenco: Handle roofing inquiries and qualify leads."""
    log_action("ProencoAgent", "processing", f"{from_name}: {text}")
    meta = get_conv_meta(state, from_number)

    if from_number not in state["conversations"]:
        state["conversations"][from_number] = []
    history = state["conversations"][from_number]

    response = claude_respond(PROENCO_SYSTEM, history, text)
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": response})
    state["conversations"][from_number] = history[-20:]

    wa_send(from_number, response)
    log_action("ProencoAgent", "sent_response", response[:100])

    # Check if lead is ready to notify Aldo (only once per conversation)
    if not meta.get('lead_sent'):
        lead = extract_proenco_lead(history)
        if lead:
            notify_aldo(lead, from_number, from_name)
            meta['lead_sent'] = True
            log_action("ProencoAgent", "lead_sent", f"Notified Aldo for {from_name}")

    save_state(state)

# ════════════════════════════════════════════════════════════════════════════════
# ─── ORCHESTRATOR ────────────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════════

def orchestrate(message_data: dict):
    """Main dispatcher — routes to SUMIN or Proenco based on conversation context."""
    time.sleep(60)

    state     = load_state()
    from_number = message_data.get("from", "")
    from_name   = message_data.get("from_name", from_number)
    msg_type    = message_data.get("type", "text")

    log_action("Orchestrator", "received", f"from={from_name} type={msg_type}")

    # FulfillmentAgent always takes priority (Oficina SPS messages)
    if fulfillment_agent(message_data, state):
        return

    meta     = get_conv_meta(state, from_number)
    business = meta.get('business')

    # ── IMAGE HANDLING ────────────────────────────────────────────────────────
    if msg_type == "image":
        media_id  = message_data.get("image", {}).get("id", "")
        mime_type = message_data.get("image", {}).get("mime_type", "image/jpeg")
        if business == 'proenco':
            proenco_agent(from_number, from_name, "[El cliente envió una foto de su techo actual]", state)
        else:
            vision_agent(from_number, from_name, media_id, mime_type, state)
        return

    # ── TEXT HANDLING ─────────────────────────────────────────────────────────
    if msg_type == "text":
        text = message_data.get("text", {}).get("body", "")

        # Skip Zoho codes
        if re.fullmatch(r"[a-zA-Z]{2,5}\d{4,8}", text.strip()):
            log_action("Orchestrator", "skipped_zoho_code", text)
            return

        # ── ROUTING LOGIC ─────────────────────────────────────────────────────
        if business is None:
            detected = classify_business(text)
            if detected:
                meta['business'] = detected
                business = detected
                save_state(state)
            else:
                # First message is ambiguous — ask which business
                wa_send(from_number, ROUTING_QUESTION)
                meta['business'] = 'routing_asked'
                save_state(state)
                return

        elif business == 'routing_asked':
            # Interpret response to routing question
            detected = classify_business(text)
            if detected:
                meta['business'] = detected
                business = detected
            elif any(k in text.lower() for k in ['1', 'primero', 'sumin', 'soldadura', 'epp']):
                meta['business'] = 'sumin'
                business = 'sumin'
            elif any(k in text.lower() for k in ['2', 'segundo', 'techo', 'proenco', 'lámina', 'gotera']):
                meta['business'] = 'proenco'
                business = 'proenco'
            else:
                wa_send(from_number, "Por favor indíquenos: ¿soldadura/EPP (1) o cambio de techo (2)?")
                save_state(state)
                return
            save_state(state)

        # ── DISPATCH ─────────────────────────────────────────────────────────
        if business == 'sumin':
            sales_agent(from_number, from_name, text, state)
        elif business == 'proenco':
            proenco_agent(from_number, from_name, text, state)

    elif msg_type == "document":
        filename = message_data.get("document", {}).get("filename", "")
        if business == 'proenco':
            proenco_agent(from_number, from_name, f"[Documento adjunto: {filename}]", state)
        else:
            sales_agent(from_number, from_name, f"[Documento adjunto: {filename}]", state)

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

    logs_html = ""
    for entry in reversed(logs[-50:]):
        color = {
            "SalesAgent": "#4CAF50", "ProencoAgent": "#FF6B35",
            "VisionAgent": "#2196F3", "PaymentAgent": "#FF9800",
            "FulfillmentAgent": "#9C27B0", "Orchestrator": "#607D8B",
            "WA_SEND": "#00BCD4", "Webhook": "#795548"
        }.get(entry["agent"], "#999")
        logs_html += f"""<tr>
          <td style='color:#888;font-size:12px'>{entry['timestamp'][11:19]}</td>
          <td><span style='background:{color};color:white;padding:2px 8px;border-radius:10px;font-size:12px'>{entry['agent']}</span></td>
          <td style='font-size:13px'>{entry['action']}</td>
          <td style='font-size:12px;color:#555'>{entry['detail'][:80]}</td>
        </tr>"""

    # Conversations by business
    conv_meta = state.get("conv_meta", {})
    sumin_convs   = sum(1 for v in conv_meta.values() if v.get('business') == 'sumin')
    proenco_convs = sum(1 for v in conv_meta.values() if v.get('business') == 'proenco')
    leads_sent    = sum(1 for v in conv_meta.values() if v.get('lead_sent'))

    orders_html = ""
    status_icons = {"quote_sent": "📄", "payment_received": "💰", "shipped": "📦", "pending": "⏳"}
    for o in state.get("orders", []):
        icon = status_icons.get(o.get("status", ""), "❓")
        orders_html += f"<tr><td>{o.get('name','')}</td><td>{o.get('client','')}</td><td>{icon} {o.get('status','')}</td><td>{o.get('payment_date','')[:10]}</td></tr>"

    return Response(content=f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'><title>SUMIN / Proenco Bot</title>
<meta http-equiv='refresh' content='15'>
<style>
body{{font-family:sans-serif;background:#1a1a2e;color:#eee;margin:0;padding:20px}}
h1{{color:#4CAF50}}h2{{color:#aaa;font-size:16px}}
.stats{{display:flex;gap:16px;margin-bottom:20px}}
.stat{{background:#16213e;border-radius:8px;padding:14px 20px;flex:1;text-align:center}}
.stat .n{{font-size:28px;font-weight:bold;color:#4CAF50}}
.stat .l{{font-size:12px;color:#888}}
.proenco .n{{color:#FF6B35}}
table{{width:100%;border-collapse:collapse;background:#16213e;border-radius:8px;overflow:hidden;margin-bottom:20py}}
th{{background:#0f3460;padding:10px;text-align:left;font-size:13px}}
td{{padding:8px 10px;border-bottom:1px solid #0a2040}}
</style></head>
<body>
<h1>🤖 SUMIN / Proenco Bot Dashboard</h1>
<p style='color:#888'>Auto-refresh 15s | {datetime.now().strftime('%H:%M:%S')}</p>
<div class='stats'>
  <div class='stat'><div class='n'>{sumin_convs}</div><div class='l'>Chats SUMIN</div></div>
  <div class='stat proenco'><div class='n'>{proenco_convs}</div><div class='l'>Chats Proenco</div></div>
  <div class='stat proenco'><div class='n'>{leads_sent}</div><div class='l'>Leads enviados a Aldo</div></div>
  <div class='stat'><div class='n'>{len(state.get("orders",[]))}</div><div class='l'>Órdenes SUMIN</div></div>
</div>
<h2>📦 Órdenes SUMIN</h2>
<table><tr><th>Cliente</th><th>Número</th><th>Status</th><th>Fecha pago</th></tr>
{orders_html or "<tr><td colspan=4 style='color:#555'>Sin órdenes</td></tr>"}
</table>
<h2>📋 Log de agentes (últimas 50 acciones)</h2>
<table><tr><th>Hora</th><th>Agente</th><th>Acción</th><th>Detalle</th></tr>
{logs_html or "<tr><td colspan=4 style='color:#555'>Sin actividad</td></tr>"}
</table>
</body></html>""", media_type="text/html")

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
<p><strong>Suministros Internacionales HN (SUMIN) / Proenco</strong> - Marzo 2026</p>
<p>Recopilamos el contenido de mensajes y número de teléfono únicamente para atender su solicitud comercial. No compartimos su información con terceros ajenos a nuestras empresas.</p>
<p>Contacto: <a href="mailto:danielprado@suminhn.com">danielprado@suminhn.com</a></p>
</body></html>""", media_type="text/html")
