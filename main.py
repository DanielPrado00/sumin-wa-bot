""" SUMIN / Proenco / Takicardia WhatsApp Business Bot
Multi-agent system: SalesAgent, ProencoAgent, TakicardiaAgent, VisionAgent, PaymentAgent, FulfillmentAgent
Structure: SUMIN, Proenco and Takicardia share one WhatsApp number. Routing happens on first contact.
To split into separate bots later: each business section is clearly delimited.
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
ALDO_NUMBER              = "50497096965"   # Ing. Aldo Villafranca - Proenco
TAKICARDIA_CONFIRM_NUMBER = "50431447807"  # Takicardia owner – receives comprobantes
EDY_NUMBER                = "50431723021"  # Edy – motorista de delivery

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

# ════════════════════════════════════════════════════════════════════════════════
# ─── TAKICARDIA — SYSTEM PROMPT ──────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════════
TAKICARDIA_SYSTEM = """Eres el asistente virtual de Takicardia Taqueria, una taquería en San Pedro Sula, Honduras.
Estás ubicados en Jardines del Valle, Blvd frente al Superzito, detrás de Galerías del Valle.
Instagram: @takicardia_sps | WhatsApp: 8742-9043

═══════════════════════════════════════
ESTILO DE RESPUESTA
═══════════════════════════════════════
- Tono amigable, cálido y relajado — como el staff de una taquería cool.
- Usa emojis con moderación para dar energía, pero no exageres.
- Breve y claro. Una cosa a la vez.
- Saluda con "Hola! Bienvenido a Takicardia 🌮" en el primer mensaje.

═══════════════════════════════════════
MENÚ COMPLETO
═══════════════════════════════════════

🥩 TACOS x LIBRA (orden por libra de carne — incluye 20 tortillas, cebolla, cilantro, limones, salsa verde, encurtido y pico de gallo):
- Pollo: L340 por libra
- Chorizo Picante: L350 por libra
- Cerdo al Pastor: L350 por libra ⚠️ (contiene piña)
- Res: L360 por libra
- Mixto: L360 por libra
Ideal para grupos o familias. Cada libra rinde bastante.

🫓 QUESADILLAS / GRINGAS (orden de 3 piezas):
- Pollo: L190
- Chorizo: L190
- Res: L210
- Cerdo al Pastor: L200
- Mixtas: L230
- Quesabirrias: L230

⭐ ESPECIALIDAD (plato de 4 tacos, doble tortilla — excepto Birria):
- Pollo: L180
- Cerdo al Pastor: L200 ⚠️ (contiene piña)
- Chorizo Picante: L190
- Res: L200
- Chicharrón: L180
- Alambre: L210
- Birria: L225
- Mixtos: L220

🍟 ANTOJOS:
- Nacho Grande: L240 (incluye jalapeños y cebolla)
- Nacho Mediano: L170 (incluye jalapeños y cebolla)
- Esquites: L70
- Birria Noodles: L160
- Birria Burger: L200
- Burritos: L180

🥤 BEBIDAS:
- Refresco Personal: L35
- Refresco Natural L40 (Maracuyá, Limonada, Limonada Rosa, Té Frío, Jamaica)
- Agua: L20
- Cerveza Nacional: L50
- Cerveza Internacional: L60
- Sangría: L140
- Michelada Mix: L50

═══════════════════════════════════════
CÓMO INTERPRETAR LAS CANTIDADES DE TACOS
═══════════════════════════════════════
IMPORTANTE: Hay dos categorías distintas de tacos — debes aclarar cuál quiere el cliente si no es obvio.

📦 TACOS x LIBRA: Se pide por LIBRA de carne (no por taco individual).
  "1 de pollo" = 1 LIBRA de pollo = L340 (viene con 20 tortillas + complementos)
  "2 de pastor y 2 de res" = 2 libras de pastor + 2 libras de res
  → Esta categoría es para grupos o cuando quieren bastante comida.

⭐ ESPECIALIDAD: Plato de exactamente 4 tacos por orden.
  "1 especialidad de pollo" = 1 plato de 4 tacos de pollo = L180
  "2 de pastor y 2 de pollo" = puede ser 1 Especialidad Mixtos (L220) o 2 especialidades separadas
  → Esta es la opción individual/personal.

REGLAS DE INTERPRETACIÓN:
- Si el cliente dice solo "quiero tacos de X" sin especificar → pregunta: "¿Lo querés como Especialidad (plato de 4 tacos) o Tacos x Libra (para grupo, viene con 20 tortillas y todos los complementos)?"
- Si menciona "libras" o "lbs" → Tacos x Libra.
- Si pide "2 de pastor y 2 de pollo" → probablemente quiere Especialidad Mixtos (L220) o 2 especialidades. Confirma.
- Si pide varios sabores en poca cantidad → sugiere Especialidad Mixtos.
- Si pide la misma cantidad de un solo sabor (ej: "3 de pollo") → puede ser 3 libras (Tacos x LB) o 3 especialidades de pollo. Confirma.

═══════════════════════════════════════
PERSONALIZACIÓN DE PEDIDOS
═══════════════════════════════════════
- Si el cliente pide ingredientes por aparte (sin cebolla, con extra piña, sin cilantro, etc.) → anótalo en la comanda tal como lo pide.
- Siempre incluirlo en el resumen final del pedido.
- Ejemplo comanda: "1 Especialidad Pastor (sin piña) + 1 Nacho Grande (sin jalapeño)"

═══════════════════════════════════════
OPCIONES DE ENTREGA
═══════════════════════════════════════
1. Pedidos Ya — el cliente hace el pedido por la app de Pedidos Ya
2. Pickup en local — pasa a recoger en Jardines del Valle (Blvd frente al Superzito, detrás de Galerías del Valle)
3. Delivery — nuestro motorista lleva el pedido (+L80 de costo de envío). En ~15 minutos llega el motorista una vez confirmado el pago.

═══════════════════════════════════════
FLUJO DE PEDIDO
═══════════════════════════════════════
1. Saluda y muestra el menú si el cliente pide verlo.
2. Toma el pedido completo (productos, cantidades, personalizaciones).
3. Pregunta cómo quiere recibirlo: ¿Pedidos Ya, pickup o delivery?
   - Si es delivery: pedir dirección completa.
   - Si es pickup: confirmar que pase al local en Jardines del Valle.
4. Confirmar el pedido con resumen y total (incluyendo personalizaciones). Si es delivery, sumar L80 al total por costo de envío.
5. Preguntar forma de pago:
   "Para confirmar tu pedido, ¿cómo prefieres pagar?
   💳 *Tarjeta* — te enviamos un link de pago
   🏦 *Transferencia* — te damos el número de cuenta"
6. Según respuesta:
   - Tarjeta: "Perfecto, en un momento te enviamos el link de pago 🔗"
   - Transferencia: "Aquí los datos para transferencia:
     BAC — Cuenta: 749058971
     A nombre de: Inversiones Casla
     Monto exacto: L[TOTAL]
     Al hacer la transferencia, envíanos el comprobante aquí y procesamos tu pedido de inmediato ✅"

IMPORTANTE: Si el cliente quiere pagar con Tarjeta, solo confirma que le enviarás el link — no lo generes tú, el staff lo enviará manualmente.

═══════════════════════════════════════
CUANDO EL CLIENTE MANDA COMPROBANTE
═══════════════════════════════════════
Responde: "¡Listo [nombre]! Recibimos tu comprobante, tu pedido está siendo preparado 🌮🔥 En aproximadamente 15 minutos pasa el motorista."
(Solo di esto si es delivery. Si es pickup: "¡Listo! Recibimos tu comprobante, tu pedido está siendo preparado 🌮🔥 En breve te avisamos cuando esté listo para recoger.")
(El sistema enviará el comprobante + detalles del pedido al equipo de Takicardia automáticamente.)

═══════════════════════════════════════
REGLAS CLAVE
═══════════════════════════════════════
- No inventes precios ni items que no están en el menú.
- Si el cliente pregunta por algo que no está en el menú: "Por el momento no tenemos ese plato, pero tenemos [sugerencia similar]."
- Si menciona alergia a la piña: advertirle que el Pastor contiene piña.
- No hagas más de una pregunta a la vez.
- Anota SIEMPRE las personalizaciones (sin cebolla, extra piña, etc.) en el resumen del pedido.
- Sé eficiente — el cliente quiere su taco rápido 🌮
"""

# ─── ROUTING ─────────────────────────────────────────────────────────────────
ROUTING_QUESTION = """Hola buen día! Tenemos tres líneas de servicio:

1️⃣ *SUMIN* — Productos de soldadura y equipo de protección personal (EPP)
2️⃣ *Proenco* — Cambio completo de techo (Standing Seam, con garantía)
3️⃣ *Takicardia Taqueria* — Tacos, birria, gringas y más 🌮

¿En cuál de los tres le podemos ayudar?"""

SUMIN_KEYWORDS  = ['soldar', 'soldadura', 'electrodo', 'mig', 'careta', 'guante',
                   'chaqueta', 'alambre', 'oxicorte', 'sumin', 'epp', 'protección',
                   'delantal', 'escudo', 'varilla', 'disco', 'lija', 'esmeril']
PROENCO_KEYWORDS = ['techo', 'lámina', 'gotera', 'proenco', 'standing', 'nave',
                    'bodega', 'cubierta', 'zinc', 'impermeabilizar', 'aislamiento',
                    'lámina de techo', 'cambio de techo', 'goteras', 'lluvia']
TAKICARDIA_KEYWORDS = ['taco', 'tacos', 'birria', 'gringa', 'quesadilla', 'quesabirria',
                       'takicardia', 'pastor', 'chorizo', 'birria burger', 'nacho',
                       'esquites', 'burritos', 'michelada', 'sangria', 'antojo',
                       'pedido', 'orden', 'menu', 'menú', 'comida', 'taqueria',
                       'taquería', 'alambre', 'chicharrón', 'chicharron']

def classify_business(text: str):
    """Returns 'sumin', 'proenco', 'takicardia', or None if ambiguous."""
    t = text.lower().strip()
    is_sumin      = any(k in t for k in SUMIN_KEYWORDS)      or t in ['1', '1️⃣']
    is_proenco    = any(k in t for k in PROENCO_KEYWORDS)    or t in ['2', '2️⃣']
    is_takicardia = any(k in t for k in TAKICARDIA_KEYWORDS) or t in ['3', '3️⃣']
    matched = sum([is_sumin, is_proenco, is_takicardia])
    if matched == 1:
        if is_sumin:      return 'sumin'
        if is_proenco:    return 'proenco'
        if is_takicardia: return 'takicardia'
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
        return {"orders": [], "conversations": {}, "conv_meta": {}, "taki_orders": []}

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
# ─── TAKICARDIA AGENTS ───────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════════

def extract_taki_order_summary(history: list) -> dict:
    """Use Haiku to extract order summary and delivery info from conversation history."""
    if len(history) < 4:
        return {"summary": "Pedido en progreso", "delivery": False, "address": ""}
    conv_text = "\n".join(
        f"{'Cliente' if m['role']=='user' else 'Bot'}: {m['content']}"
        for m in history[-12:]
    )
    msg = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": f"""De esta conversación de WhatsApp de una taquería, extrae la información del pedido.
Responde SOLO el siguiente JSON sin nada más:
{{
  "summary": "resumen del pedido en 2-3 líneas (qué pidió, cantidades, personalizaciones, total si se mencionó)",
  "delivery": true o false (true si es delivery a domicilio, false si es pickup o Pedidos Ya),
  "address": "dirección de entrega si es delivery, sino vacío"
}}

Si no hay pedido claro, pon summary: "Consulta general", delivery: false, address: ""

Conversación:
{conv_text}"""}]
    )
    result = msg.content[0].text.strip()
    try:
        match = re.search(r'\{.*\}', result, re.DOTALL)
        if match:
            return json.loads(match.group())
    except:
        pass
    return {"summary": result, "delivery": False, "address": ""}

def taki_comprobante_agent(from_number: str, from_name: str, media_id: str, image_bytes: bytes, state: dict):
    """Takicardia: Handle payment comprobante — acknowledge client + forward to owner + notify Edy if delivery."""
    log_action("TakicardiaAgent", "comprobante_received", f"From {from_name} ({from_number})")

    # Get order info from conversation history
    history = state.get("conversations", {}).get(from_number, [])
    order_info = extract_taki_order_summary(history)
    order_summary = order_info.get("summary", "Pedido en progreso")
    is_delivery   = order_info.get("delivery", False)
    # Use location saved from WhatsApp location message if available
    saved_address = get_conv_meta(state, from_number).get("delivery_address", "")
    address       = saved_address or order_info.get("address", "")
    if address:
        is_delivery = True

    first_name = from_name.split()[0] if from_name else "amigo"

    # Acknowledge client
    if is_delivery:
        wa_send(from_number, f"¡Listo {first_name}! Recibimos tu comprobante, tu pedido está siendo preparado 🌮🔥 En aproximadamente 15 minutos pasa el motorista.")
    else:
        wa_send(from_number, f"¡Listo {first_name}! Recibimos tu comprobante, tu pedido está siendo preparado 🌮🔥")

    # Forward comprobante image to Takicardia owner
    wa_forward_image(media_id, TAKICARDIA_CONFIRM_NUMBER)

    # Send order details as text to owner
    delivery_line = f"🛵 *Entrega:* Delivery — {address}" if is_delivery else "🏠 *Entrega:* Pickup en local"
    owner_msg = (
        f"🌮 *Nuevo pedido confirmado — Takicardia*\n\n"
        f"👤 *Cliente:* {from_name}\n"
        f"📞 *Número:* +{from_number}\n"
        f"📋 *Pedido:*\n{order_summary}\n"
        f"{delivery_line}\n\n"
        f"💳 *Pago:* Comprobante de transferencia recibido ✅"
    )
    wa_send(TAKICARDIA_CONFIRM_NUMBER, owner_msg)

    # If delivery — notify Edy (motorista) with client location
    if is_delivery:
        edy_msg = (
            f"🛵 *Pedido para delivery — Takicardia*\n\n"
            f"👤 *Cliente:* {from_name}\n"
            f"📞 *Número:* +{from_number}\n"
            f"📍 *Dirección:* {address or 'Ver con el cliente'}\n"
            f"📋 *Pedido:* {order_summary}\n\n"
            f"Por favor coordina la entrega. El cliente ya fue notificado que en ~15 minutos llega el motorista. 🙏"
        )
        wa_send(EDY_NUMBER, edy_msg)
        log_action("TakicardiaAgent", "edy_notified", f"Delivery to {address} for {from_name}")

    # Save order record
    if 'taki_orders' not in state:
        state['taki_orders'] = []
    state['taki_orders'].append({
        "client": from_number,
        "name": from_name,
        "status": "payment_received",
        "summary": order_summary,
        "delivery": is_delivery,
        "address": address,
        "payment_date": datetime.now().isoformat()
    })
    save_state(state)
    log_action("TakicardiaAgent", "order_confirmed", f"{from_name}: {order_summary[:80]}")

def notify_taki_kitchen(from_number: str, from_name: str, order_summary: str, is_delivery: bool, address: str):
    """Send new order notification to Takicardia kitchen/owner."""
    delivery_line = f"🛵 *Entrega:* Delivery — {address}" if is_delivery else "🏠 *Entrega:* Pickup en local"
    msg = (
        f"🌮 *Nuevo pedido — Takicardia*\n\n"
        f"👤 *Cliente:* {from_name}\n"
        f"📞 *Número:* +{from_number}\n"
        f"📋 *Pedido:*\n{order_summary}\n"
        f"{delivery_line}\n\n"
        f"⏳ *Estado:* Pendiente de pago"
    )
    wa_send(TAKICARDIA_CONFIRM_NUMBER, msg)
    log_action("TakicardiaAgent", "kitchen_notified", f"{from_name}: {order_summary[:60]}")

def takicardia_agent(from_number: str, from_name: str, text: str, state: dict):
    """Takicardia: Handle food ordering conversation."""
    log_action("TakicardiaAgent", "processing", f"{from_name}: {text}")

    if from_number not in state["conversations"]:
        state["conversations"][from_number] = []
    history = state["conversations"][from_number]

    response = claude_respond(TAKICARDIA_SYSTEM, history, text)
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": response})
    state["conversations"][from_number] = history[-20:]

    wa_send(from_number, response)
    log_action("TakicardiaAgent", "sent_response", response[:100])

    # Detect order confirmation moment — when bot gives payment instructions
    is_order_confirmation = any(phrase in response.lower() for phrase in [
        "bac", "749058971", "link de pago", "transferencia", "confirmar tu pedido"
    ])
    meta = get_conv_meta(state, from_number)
    if is_order_confirmation and not meta.get('kitchen_notified'):
        order_info = extract_taki_order_summary(history)
        order_summary = order_info.get("summary", "Pedido en progreso")
        saved_address = meta.get("delivery_address", "")
        is_delivery = bool(saved_address) or order_info.get("delivery", False)
        address = saved_address or order_info.get("address", "")
        notify_taki_kitchen(from_number, from_name, order_summary, is_delivery, address)
        meta['kitchen_notified'] = True

    save_state(state)

# ════════════════════════════════════════════════════════════════════════════════
# ─── ORCHESTRATOR ────────────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════════

def orchestrate(message_data: dict):
    """Main dispatcher — routes to SUMIN, Proenco, or Takicardia based on conversation context."""
    time.sleep(10)

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

        if business == 'takicardia':
            # Download and check if it's a payment comprobante
            image_bytes = wa_download_image(media_id)
            if image_bytes and is_comprobante(image_bytes, mime_type):
                taki_comprobante_agent(from_number, from_name, media_id, image_bytes, state)
            else:
                # Non-comprobante image — let the agent handle it contextually
                takicardia_agent(from_number, from_name, "[El cliente envió una imagen]", state)
        elif business == 'proenco':
            proenco_agent(from_number, from_name, "[El cliente envió una foto de su techo actual]", state)
        else:
            vision_agent(from_number, from_name, media_id, mime_type, state)
        return

    # ── LOCATION HANDLING ────────────────────────────────────────────────────
    if msg_type == "location":
        loc       = message_data.get("location", {})
        lat       = loc.get("latitude", "")
        lng       = loc.get("longitude", "")
        loc_name  = loc.get("name", "")
        loc_addr  = loc.get("address", "")
        maps_link = f"https://maps.google.com/?q={lat},{lng}"
        # Build readable address text
        parts = [p for p in [loc_name, loc_addr] if p]
        address_text = (", ".join(parts) + "\n" if parts else "") + maps_link
        # Save to conv_meta so comprobante agent can use it
        meta['delivery_address'] = address_text
        save_state(state)
        log_action("Orchestrator", "location_received", f"{from_name}: {address_text[:80]}")
        if business == 'takicardia':
            takicardia_agent(from_number, from_name,
                             f"[El cliente compartió su ubicación para el delivery: {address_text}]", state)
        else:
            wa_send(from_number, "Gracias, recibimos tu ubicación 📍")
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
            elif any(k in text.lower() for k in ['3', 'tercero', 'taco', 'tacos', 'takicardia', 'comida', 'birria']):
                meta['business'] = 'takicardia'
                business = 'takicardia'
            else:
                wa_send(from_number, "Por favor indíquenos: ¿soldadura/EPP (1), cambio de techo (2) o Takicardia Taqueria (3)?")
                save_state(state)
                return
            save_state(state)

        # ── DISPATCH ─────────────────────────────────────────────────────────
        if business == 'sumin':
            sales_agent(from_number, from_name, text, state)
        elif business == 'proenco':
            proenco_agent(from_number, from_name, text, state)
        elif business == 'takicardia':
            takicardia_agent(from_number, from_name, text, state)

    elif msg_type == "document":
        doc      = message_data.get("document", {})
        filename = doc.get("filename", "")
        mime     = doc.get("mime_type", "")
        media_id = doc.get("id", "")
        # PDF comprobante for Takicardia
        if business == 'takicardia' and "pdf" in mime.lower():
            log_action("TakicardiaAgent", "pdf_comprobante", f"PDF from {from_name}")
            first_name = from_name.split()[0] if from_name else "amigo"
            saved_address = get_conv_meta(state, from_number).get("delivery_address", "")
            is_delivery = bool(saved_address)
            if is_delivery:
                wa_send(from_number, f"¡Listo {first_name}! Recibimos tu comprobante, tu pedido está siendo preparado 🌮🔥 En aproximadamente 15 minutos pasa el motorista.")
            else:
                wa_send(from_number, f"¡Listo {first_name}! Recibimos tu comprobante, tu pedido está siendo preparado 🌮🔥")
            # Forward PDF to owner
            wa_forward_image(media_id, TAKICARDIA_CONFIRM_NUMBER)
            history = state.get("conversations", {}).get(from_number, [])
            order_info = extract_taki_order_summary(history)
            order_summary = order_info.get("summary", "Pedido en progreso")
            address = saved_address or order_info.get("address", "")
            delivery_line = f"🛵 Delivery — {address}" if (is_delivery or order_info.get("delivery")) else "🏠 Pickup"
            wa_send(TAKICARDIA_CONFIRM_NUMBER,
                    f"🌮 *Pedido confirmado (PDF)*\n👤 {from_name} +{from_number}\n📋 {order_summary}\n{delivery_line}\n💳 Comprobante recibido ✅")
            if is_delivery and address:
                wa_send(EDY_NUMBER,
                        f"🛵 *Delivery — Takicardia*\n👤 {from_name} +{from_number}\n📍 {address}\n📋 {order_summary}\n~15 min al cliente 🙏")
            if 'taki_orders' not in state:
                state['taki_orders'] = []
            state['taki_orders'].append({"client": from_number, "name": from_name,
                                         "status": "payment_received", "summary": order_summary,
                                         "delivery": is_delivery, "address": address,
                                         "payment_date": datetime.now().isoformat()})
            save_state(state)
        elif business == 'proenco':
            proenco_agent(from_number, from_name, f"[Documento adjunto: {filename}]", state)
        elif business == 'takicardia':
            takicardia_agent(from_number, from_name, f"[Documento adjunto: {filename}]", state)
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
        state = {"orders": [], "conversations": {}, "conv_meta": {}, "taki_orders": []}

    logs_html = ""
    for entry in reversed(logs[-50:]):
        color = {
            "SalesAgent": "#4CAF50", "ProencoAgent": "#FF6B35",
            "TakicardiaAgent": "#E91E63", "VisionAgent": "#2196F3",
            "PaymentAgent": "#FF9800", "FulfillmentAgent": "#9C27B0",
            "Orchestrator": "#607D8B", "WA_SEND": "#00BCD4", "Webhook": "#795548"
        }.get(entry["agent"], "#999")
        logs_html += f"""<tr>
          <td style='color:#888;font-size:12px'>{entry['timestamp'][11:19]}</td>
          <td><span style='background:{color};color:white;padding:2px 8px;border-radius:10px;font-size:12px'>{entry['agent']}</span></td>
          <td style='font-size:13px'>{entry['action']}</td>
          <td style='font-size:12px;color:#555'>{entry['detail'][:80]}</td>
        </tr>"""

    # Conversations by business
    conv_meta = state.get("conv_meta", {})
    sumin_convs     = sum(1 for v in conv_meta.values() if v.get('business') == 'sumin')
    proenco_convs   = sum(1 for v in conv_meta.values() if v.get('business') == 'proenco')
    taki_convs      = sum(1 for v in conv_meta.values() if v.get('business') == 'takicardia')
    leads_sent      = sum(1 for v in conv_meta.values() if v.get('lead_sent'))
    taki_orders     = state.get("taki_orders", [])
    taki_paid       = sum(1 for o in taki_orders if o.get('status') == 'payment_received')

    orders_html = ""
    status_icons = {"quote_sent": "📄", "payment_received": "💰", "shipped": "📦", "pending": "⏳"}
    for o in state.get("orders", []):
        icon = status_icons.get(o.get("status", ""), "❓")
        orders_html += f"<tr><td>{o.get('name','')}</td><td>{o.get('client','')}</td><td>{icon} {o.get('status','')}</td><td>{o.get('payment_date','')[:10]}</td></tr>"

    taki_orders_html = ""
    for o in taki_orders:
        taki_orders_html += f"<tr><td>{o.get('name','')}</td><td>{o.get('client','')}</td><td>💰 {o.get('status','')}</td><td>{o.get('summary','')[:60]}</td><td>{o.get('payment_date','')[:10]}</td></tr>"

    return Response(content=f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'><title>SUMIN / Proenco / Takicardia Bot</title>
<meta http-equiv='refresh' content='15'>
<style>
body{{font-family:sans-serif;background:#1a1a2e;color:#eee;margin:0;padding:20px}}
h1{{color:#4CAF50}}h2{{color:#aaa;font-size:16px}}
.stats{{display:flex;gap:16px;margin-bottom:20px;flex-wrap:wrap}}
.stat{{background:#16213e;border-radius:8px;padding:14px 20px;flex:1;text-align:center;min-width:120px}}
.stat .n{{font-size:28px;font-weight:bold;color:#4CAF50}}
.stat .l{{font-size:12px;color:#888}}
.proenco .n{{color:#FF6B35}}
.taki .n{{color:#E91E63}}
table{{width:100%;border-collapse:collapse;background:#16213e;border-radius:8px;overflow:hidden;margin-bottom:20px}}
th{{background:#0f3460;padding:10px;text-align:left;font-size:13px}}
td{{padding:8px 10px;border-bottom:1px solid #0a2040}}
</style></head>
<body>
<h1>🤖 SUMIN / Proenco / Takicardia Bot Dashboard</h1>
<p style='color:#888'>Auto-refresh 15s | {datetime.now().strftime('%H:%M:%S')}</p>
<div class='stats'>
  <div class='stat'><div class='n'>{sumin_convs}</div><div class='l'>Chats SUMIN</div></div>
  <div class='stat proenco'><div class='n'>{proenco_convs}</div><div class='l'>Chats Proenco</div></div>
  <div class='stat proenco'><div class='n'>{leads_sent}</div><div class='l'>Leads → Aldo</div></div>
  <div class='stat taki'><div class='n'>{taki_convs}</div><div class='l'>Chats Takicardia</div></div>
  <div class='stat taki'><div class='n'>{taki_paid}</div><div class='l'>Pedidos Takicardia</div></div>
  <div class='stat'><div class='n'>{len(state.get("orders",[]))}</div><div class='l'>Órdenes SUMIN</div></div>
</div>
<h2>🌮 Pedidos Takicardia</h2>
<table><tr><th>Cliente</th><th>Número</th><th>Status</th><th>Pedido</th><th>Fecha</th></tr>
{taki_orders_html or "<tr><td colspan=5 style='color:#555'>Sin pedidos</td></tr>"}
</table>
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
<p><strong>Suministros Internacionales HN (SUMIN) / Proenco / Takicardia Taqueria</strong> - Abril 2026</p>
<p>Recopilamos el contenido de mensajes y número de teléfono únicamente para atender su solicitud comercial. No compartimos su información con terceros ajenos a nuestras empresas.</p>
<p>Contacto: <a href="mailto:danielprado@suminhn.com">danielprado@suminhn.com</a></p>
</body></html>""", media_type="text/html")
