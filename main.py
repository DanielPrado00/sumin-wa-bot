""" SUMIN WhatsApp Business Bot
Standalone bot for welding supplies and personal protection equipment (EPP)
"""
import os, json, re, httpx, base64, time
from datetime import datetime
from fastapi import FastAPI, Request, Response, BackgroundTasks
from fastapi.responses import PlainTextResponse
import anthropic

app = FastAPI()

# âââ CONFIG ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
VERIFY_TOKEN      = os.environ["WA_VERIFY_TOKEN"]
WA_TOKEN          = os.environ["WA_ACCESS_TOKEN"]
PHONE_NUMBER_ID   = os.environ["WA_PHONE_NUMBER_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
STATE_FILE = "orders_state.json"
LOG_FILE   = "bot_log.json"

# âââ ZOHO BOOKS CONFIG âââââââââââââââââââââââââââââââââââââââââââââââââââââââ
ZOHO_CLIENT_ID     = os.environ.get("ZOHO_CLIENT_ID", "")
ZOHO_CLIENT_SECRET = os.environ.get("ZOHO_CLIENT_SECRET", "")
ZOHO_ORG_ID        = os.environ.get("ZOHO_ORG_ID", "")
ZOHO_REFRESH_TOKEN = os.environ.get("ZOHO_REFRESH_TOKEN", "")
ZOHO_REDIRECT_URI  = "https://sumin-wa-bot.onrender.com/zoho-callback"

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SKIP_NUMBERS = {
    "Sumin Oficina SPS",
    "Arnold Sumin",
    "ConfirmaciÃ³n de transferencias Sumin",
    "Servicio Al Cliente Boxful"
}

# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# âââ SUMIN â SYSTEM PROMPT âââââââââââââââââââââââââââââââââââââââââââââââââââ
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
SUMIN_SYSTEM = """Eres un agente de ventas de Suministros Internacionales HN (SUMIN).
Respondes en espaÃ±ol, con un tono natural y cÃ¡lido â como una persona real, NO como un robot.
Imita el estilo de Daniel, el dueÃ±o: breve, amable, directo, sin exagerar con emojis ni formalismos.

âââââââââââââââââââââââââââââââââââââââ
ESTILO DE RESPUESTA
âââââââââââââââââââââââââââââââââââââââ
- Saluda siempre con "Hola buen dÃ­a" o "buen dÃ­a" (nunca "Estimado/a", nunca "Â¡Hola! Â¿CÃ³mo estÃ¡s?").
- SÃ© breve y directo. MÃ¡ximo 3-4 lÃ­neas por respuesta cuando sea posible.
- USA POCOS EMOJIS: solo en ubicaciones/mapas. En precios y productos: 0 emojis o mÃ¡ximo 1.
- No uses bullets/listas largas para todo â escribe de forma natural.
- No hagas mÃ¡s de una pregunta a la vez.
- Cuando el cliente ya dio la informaciÃ³n necesaria, da el precio directamente, no sigas preguntando.
- Cierra siempre con calidez: "estamos para servirle", "un placer atenderle", o "estaremos pendientes".

âââââââââââââââââââââââââââââââââââââââ
FLUJO SEGÃN TIPO DE CONSULTA
âââââââââââââââââââââââââââââââââââââââ

1. CONSULTA GENÃRICA ("Hola, quiero informaciÃ³n" / "Quiero mÃ¡s informaciÃ³n"):
   Responder: "Hola buen dÃ­a! Para orientarle mejor, Â¿quÃ© producto estÃ¡ buscando?"
   Luego listar las 3 categorÃ­as:
   - Electrodos (Â¿quÃ© tipo y diÃ¡metro necesita?)
   - Alambre para soldar â MIG sin gas o con gas
   - Equipo de protecciÃ³n â caretas, guantes, chaquetas, kits

2. ELECTRODOS:
   Preguntar: diÃ¡metro y tamaÃ±o de caja (10 lbs o 50 lbs).
   Precios electrodo 6011:
     - Caja 10 lbs: L517.50
     - Caja 50 lbs: L2,587.50
   DiÃ¡metros disponibles SOLO: 3/32", 1/8", 5/32". NO hay 1/16" ni 3/16".
   Para otros electrodos (7018, etc.) sin precio conocido: "No tengo ese precio aquÃ­, puede llamarnos o pasar por tienda."

3. CARETAS / EQUIPO DE PROTECCIÃN:
   Preguntar primero: "Â¿La ocupa para uso pesado o uso bÃ¡sico?"
   Luego presentar opciones segÃºn necesidad:

   CARETAS DISPONIBLES:
   - Careta bÃ¡sica con respirador: L632.50
   - Careta Pro 4.0 (para humos de soldadura, uso intensivo): L2,530.00
   - Careta PanorÃ¡mica (visiÃ³n amplia + respirador): L4,300.00
   - Careta PAPR (sistema motorizado, mÃ¡xima protecciÃ³n): L13,225.00

   OTROS EPP:
   - Delantal de cuero: L632.50
   - SafeCut Defender 450 (chaqueta/kit de corte): L13,383.70
   - Guantes, chaquetas de cuero: "Puede pasar por tienda o llamarnos para ver existencias y precios."

   Ofrecer siempre: "Si quiere le mando foto o video del producto."

4. MICROALAMBRE / ALAMBRE MIG:
   Preguntar: Â¿con gas o sin gas? Â¿quÃ© diÃ¡metro? Â¿marca actual?
   Si el cliente tiene el producto actual: pedirle foto para identificar la referencia correcta.
   Sin precio conocido: dar precio en tienda o pedir que llame.

5. OXICORTE / EQUIPO DE GAS:
   Kits disponibles â ofrecer enviar foto + descripciÃ³n + precio mensual.
   "Le mando foto del kit para que lo vea."

6. UBICACIÃN / DIRECCIÃN:
   ð San Pedro Sula: 1ra calle, entre 1ra y 2da avenida, Edificio Metrocentro, Local #3
   https://maps.app.goo.gl/KUH7HU2idddQXCSPA
   ð Tegucigalpa (ComayagÃ¼ela): 8 calle, entre 3ra y 4ta avenida, frente a cafeterÃ­a Macao, a la par del nuevo estacionamiento del Hospital PoliclÃ­nica
   https://maps.app.goo.gl/2iNJW6wMDtKn68cg8
   Preguntar: "Â¿En cuÃ¡l ciudad le gustarÃ­a visitarnos?"

7. ENVÃOS:
   "Si es fuera de San Pedro Sula y Tegucigalpa, se le hace su envÃ­o mediante Expreco."
   - Nacional (Expreco): 1-2 dÃ­as hÃ¡biles
   - RoatÃ¡n, Guanaja, Utila: Island Shipping o BahÃ­a Shipping
   - Flete Tarifa A (SPSâTegucigalpa o SPSâPuerto CortÃ©s): L87 base + L1/lb adicional
   - Flete Tarifa B (otros destinos): L174 base + L1.96/lb adicional

âââââââââââââââââââââââââââââââââââââââ
HORARIO
âââââââââââââââââââââââââââââââââââââââ
Lunes a Viernes 8am-5pm, SÃ¡bados 8am-12pm

âââââââââââââââââââââââââââââââââââââââ
REGLAS CLAVE
âââââââââââââââââââââââââââââââââââââââ
- Si mandÃ³ comprobante de pago: "Con gusto [nombre]! Recibimos su comprobante, ya lo procesamos â"
- CÃ³digo Zoho (formato letras+nÃºmeros como "abc123"): NO es comprobante, ignorar.
- Si mandÃ³ imagen de producto: identificar quÃ© es y responder con disponibilidad/precio.
- NUNCA inventes precios. Si no lo sabÃ©s: "No tengo ese precio aquÃ­ ahora mismo, puede llamarnos o pasar por tienda."
- NO prometas enviar cotizaciÃ³n formal si no podÃ©s.
- Si el cliente pregunta algo que no vendemos, dÃ­selo directamente sin rodeos.
"""

SUMIN_KEYWORDS  = ['soldar', 'soldadura', 'electrodo', 'mig', 'careta', 'guante',
                   'chaqueta', 'alambre', 'oxicorte', 'sumin', 'epp', 'protecciÃ³n',
                   'delantal', 'escudo', 'varilla', 'disco', 'lija', 'esmeril']

# âââ HELPERS âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
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
    log_action("WA_SEND", f"â {to}", text[:100])
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
            {"type": "text", "text": "Â¿Esta imagen es un comprobante/recibo de transferencia bancaria o pago? Responde SOLO 'SI' o 'NO'."}
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
            {"type": "text", "text": "Identifica quÃ© producto de soldadura/EPP/oxicorte es este. Dame nombre tÃ©cnico, especificaciones visibles y si lo manejamos en SUMIN."}
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

def get_conv_meta(state: dict, conv_key: str) -> dict:
    """Get or initialize per-conversation metadata."""
    if 'conv_meta' not in state:
        state['conv_meta'] = {}
    if conv_key not in state['conv_meta']:
        state['conv_meta'][conv_key] = {}
    return state['conv_meta'][conv_key]

# âââ ZOHO BOOKS INTEGRATION ââââââââââââââââââââââââââââââââââââââââââââââââââ
_zoho_token_cache: dict = {"token": None, "expires": 0.0}

def get_zoho_access_token() -> str | None:
    """Return a valid Zoho access token, refreshing if expired."""
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
    """Search Zoho Books for an active item matching query.
    Returns {"found": True, "names": [...]} or {"found": False} or None on error.
    """
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
            log_action("ZohoAPI", "item_found", f"'{query}' â {names}")
            return {"found": True, "names": names}
        log_action("ZohoAPI", "item_not_found", f"'{query}' â 0 results")
        return {"found": False}
    except Exception as e:
        log_action("ZohoAPI", "search_error", str(e))
        return None

def zoho_inventory_context(text: str) -> str:
    """If the message looks like a product inquiry, query Zoho and return context string."""
    inquiry_words = [
        "tienen", "hay", "disponible", "stock", "venden", "manejan",
        "precio", "cuÃ¡nto", "cuanto", "tienen", "busco", "necesito",
        "electrodo", "alambre", "careta", "guante", "chaqueta", "delantal",
        "6011", "6013", "6010", "7018", "mig", "tig", "oxicorte",
        "disco", "lija", "esmeril", "varilla",
    ]
    if not any(w in text.lower() for w in inquiry_words):
        return ""
    # Extract product name with haiku
    try:
        extraction = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=40,
            messages=[{"role": "user", "content":
                f"Del siguiente mensaje extrae SOLO el nombre o cÃ³digo del producto que pregunta el cliente. "
                f"Responde ÃNICAMENTE el nombre/cÃ³digo del producto, sin explicaciones. "
                f"Si no hay producto claro, responde 'NINGUNO'.\n\nMensaje: {text}"}]
        ).content[0].text.strip()
        if not extraction or extraction.upper() == "NINGUNO" or len(extraction) > 60:
            return ""
        result = zoho_check_item(extraction)
        if result is None:
            return ""   # Zoho unreachable â don't alter response
        if result["found"]:
            names_str = ", ".join(result["names"])
            return (f"\n\n[INVENTARIO ZOHO â DATO REAL]: El producto '{extraction}' SÃ existe en nuestro "
                    f"catÃ¡logo activo. ArtÃ­culos encontrados: {names_str}. "
                    f"Confirma disponibilidad al cliente SIN mencionar cantidades exactas de stock.")
        else:
            return (f"\n\n[INVENTARIO ZOHO â DATO REAL]: El producto '{extraction}' NO aparece en "
                    f"nuestro catÃ¡logo activo de Zoho Books. Informa amablemente que no manejamos "
                    f"ese artÃ­culo especÃ­fico y ofrece alternativas si las hay.")
    except Exception as e:
        log_action("ZohoAPI", "context_error", str(e))
        return ""

# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# âââ SUMIN AGENTS ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def sales_agent(from_number: str, from_name: str, text: str, state: dict):
    """SUMIN: Handle sales inquiries with real-time Zoho inventory check."""
    log_action("SalesAgent", "processing", f"{from_name}: {text}")
    if from_number not in state["conversations"]:
        state["conversations"][from_number] = []
    history = state["conversations"][from_number]
    # Inject live Zoho inventory data before Claude responds
    zoho_ctx = zoho_inventory_context(text)
    system = SUMIN_SYSTEM + zoho_ctx
    response = claude_respond(system, history, text)
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
        response = f"Identificamos el producto:\n\n{product_info}\n\nÂ¿CuÃ¡ntas unidades necesita y para quÃ© ciudad es el envÃ­o?"
        wa_send(from_number, response)

def payment_agent(from_number: str, from_name: str, media_id: str, image_bytes: bytes, state: dict):
    """SUMIN: Handle payment comprobante."""
    log_action("PaymentAgent", "processing", f"Comprobante from {from_name}")
    client_name = from_name.split()[0] if from_name else "estimado cliente"
    wa_send(from_number, f"Con gusto {client_name}! Recibimos su comprobante, ya lo procesamos â")
    CONFIRMACION_GROUP = os.environ.get("WA_CONFIRMACION_GROUP", "")
    OFICINA_SPS_NUMBER = os.environ.get("WA_OFICINA_SPS", "")
    if CONFIRMACION_GROUP:
        wa_forward_image(media_id, CONFIRMACION_GROUP)
    order = next((o for o in state.get("orders", [])
                  if o.get("client") == from_number and o.get("status") in ["quote_sent", "pending"]), None)
    if OFICINA_SPS_NUMBER:
        wa_forward_image(media_id, OFICINA_SPS_NUMBER)
        info = (f"ð Pago recibido de {from_name} ({from_number})\n"
                f"CotizaciÃ³n: {order.get('quote','N/A') if order else 'N/A'}\n"
                "Favor procesar y enviar factura + guÃ­a de envÃ­o.")
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
    keywords = ["factura", "guÃ­a", "guia", "envÃ­o", "envio", "tracking", "nÃºmero de guÃ­a"]
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
        wa_send(client, f"ð¦ Su pedido estÃ¡ en camino!\n{text}")
    order["status"] = "shipped"
    order["shipped_date"] = datetime.now().isoformat()
    save_state(state)
    return True

# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# âââ ORCHESTRATOR ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def orchestrate(message_data: dict):
    """Main dispatcher â routes directly to SUMIN agents (no multi-business routing)."""
    time.sleep(10)

    state     = load_state()
    from_number = message_data.get("from", "")
    from_name   = message_data.get("from_name", from_number)
    msg_type    = message_data.get("type", "text")

    log_action("Orchestrator", "received", f"from={from_name} type={msg_type}")

    # FulfillmentAgent always takes priority (Oficina SPS messages)
    if fulfillment_agent(message_data, state):
        return

    # ââ IMAGE HANDLING ââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    if msg_type == "image":
        media_id  = message_data.get("image", {}).get("id", "")
        mime_type = message_data.get("image", {}).get("mime_type", "image/jpeg")
        vision_agent(from_number, from_name, media_id, mime_type, state)
        return

    # ââ TEXT HANDLING ââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    if msg_type == "text":
        text = message_data.get("text", {}).get("body", "")

        # Skip Zoho codes
        if re.fullmatch(r"[a-zA-Z]{2,5}\d{4,8}", text.strip()):
            log_action("Orchestrator", "skipped_zoho_code", text)
            return

        sales_agent(from_number, from_name, text, state)
        return

    # ââ DOCUMENT HANDLING ââââââââââââââââââââââââââââââââââââââââââââââââââââ
    elif msg_type == "document":
        doc      = message_data.get("document", {})
        filename = doc.get("filename", "")
        sales_agent(from_number, from_name, f"[Documento adjunto: {filename}]", state)
        return

    else:
        log_action("Orchestrator", "skipped", f"unsupported type: {msg_type}")

# âââ WEBHOOK ENDPOINTS âââââââââââââââââââââââââââââââââââââââââââââââââââââââ
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

# âââ DASHBOARD âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
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
            "SalesAgent": "#4CAF50",
            "VisionAgent": "#2196F3",
            "PaymentAgent": "#FF9800", "FulfillmentAgent": "#9C27B0",
            "Orchestrator": "#607D8B", "WA_SEND": "#00BCD4", "Webhook": "#795548"
        }.get(entry["agent"], "#999")
        logs_html += f"""<tr>
          <td style='color:#888;font-size:12px'>{entry['timestamp'][11:19]}</td>
          <td><span style='background:{color};color:white;padding:2px 8px;border-radius:10px;font-size:12px'>{entry['agent']}</span></td>
          <td style='font-size:13px'>{entry['action']}</td>
          <td style='font-size:12px;color:#555'>{entry['detail'][:80]}</td>
        </tr>"""

    # Conversations and orders
    conv_meta = state.get("conv_meta", {})
    sumin_convs     = len(conv_meta)

    orders_html = ""
    status_icons = {"quote_sent": "ð", "payment_received": "ð°", "shipped": "ð¦", "pending": "â³"}
    for o in state.get("orders", []):
        icon = status_icons.get(o.get("status", ""), "â")
        orders_html += f"<tr><td>{o.get('name','')}</td><td>{o.get('client','')}</td><td>{icon} {o.get('status','')}</td><td>{o.get('payment_date','')[:10]}</td></tr>"

    return Response(content=f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'><title>SUMIN Bot</title>
<meta http-equiv='refresh' content='15'>
<style>
body{{font-family:sans-serif;background:#1a1a2e;color:#eee;margin:0;padding:20px}}
h1{{color:#4CAF50}}h2{{color:#aaa;font-size:16px}}
.stats{{display:flex;gap:16px;margin-bottom:20px;flex-wrap:wrap}}
.stat{{background:#16213e;border-radius:8px;padding:14px 20px;flex:1;text-align:center;min-width:120px}}
.stat .n{{font-size:28px;font-weight:bold;color:#4CAF50}}
.stat .l{{font-size:12px;color:#888}}
table{{width:100%;border-collapse:collapse;background:#16213e;border-radius:8px;overflow:hidden;margin-bottom:20px}}
th{{background:#0f3460;padding:10px;text-align:left;font-size:13px}}
td{{padding:8px 10px;border-bottom:1px solid #0a2040}}
</style></head>
<body>
<h1>â¡ SUMIN Bot Dashboard</h1>
<p style='color:#888'>Auto-refresh 15s | {datetime.now().strftime('%H:%M:%S')}</p>
<div class='stats'>
  <div class='stat'><div class='n'>{sumin_convs}</div><div class='l'>Chats SUMIN</div></div>
  <div class='stat'><div class='n'>{len(state.get("orders",[]))}</div><div class='l'>Ãrdenes</div></div>
</div>
<h2>ð¦ Ãrdenes SUMIN</h2>
<table><tr><th>Cliente</th><th>NÃºmero</th><th>Status</th><th>Fecha pago</th></tr>
{orders_html or "<tr><td colspan=4 style='color:#555'>Sin Ã³rdenes</td></tr>"}
</table>
<h2>ð Log de agentes (Ãºltimas 50 acciones)</h2>
<table><tr><th>Hora</th><th>Agente</th><th>AcciÃ³n</th><th>Detalle</th></tr>
{logs_html or "<tr><td colspan=4 style='color:#555'>Sin actividad</td></tr>"}
</table>
</body></html>""", media_type="text/html")

@app.get("/zoho-auth")
async def zoho_auth():
    """Redirect to Zoho OAuth page to authorize the bot."""
    scope = "ZohoBooks.items.READ"
    url = (
        f"https://accounts.zoho.com/oauth/v2/auth"
        f"?scope={scope}"
        f"&client_id={ZOHO_CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={ZOHO_REDIRECT_URI}"
        f"&access_type=offline"
    )
    return Response(
        content=f'<html><body><h2>Autorizar Zoho Books</h2>'
                f'<p><a href="{url}" style="font-size:20px">ð Haz clic aquÃ­ para autorizar</a></p>'
                f'<p>Esto abrirÃ¡ Zoho para que apruebes el acceso al inventario.</p></body></html>',
        media_type="text/html"
    )

@app.get("/zoho-callback")
async def zoho_callback(request: Request):
    """Exchange authorization code for refresh token."""
    code = dict(request.query_params).get("code", "")
    if not code:
        return Response("<html><body><h2>â No se recibiÃ³ cÃ³digo de autorizaciÃ³n.</h2></body></html>",
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
        access  = data.get("access_token", "")
        if refresh:
            log_action("ZohoAPI", "oauth_success", "Refresh token obtained")
            return Response(
                content=f"""<html><body style='font-family:sans-serif;padding:30px'>
                <h2>â Â¡AutorizaciÃ³n exitosa!</h2>
                <p><b>Refresh Token:</b></p>
                <textarea rows="3" cols="90" style="font-size:13px">{refresh}</textarea>
                <br><br>
                <p>ð Agrega este valor en Render como variable de entorno:</p>
                <code style="background:#eee;padding:5px">ZOHO_REFRESH_TOKEN = {refresh}</code>
                <br><br><p style="color:green">El bot ahora puede consultar el inventario de Zoho Books en tiempo real.</p>
                </body></html>""",
                media_type="text/html"
            )
        log_action("ZohoAPI", "oauth_error", str(data))
        return Response(f"<html><body><h2>â Error: {data}</h2></body></html>",
                        media_type="text/html", status_code=400)
    except Exception as e:
        return Response(f"<html><body><h2>â Error: {e}</h2></body></html>",
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
<p>Recopilamos el contenido de mensajes y nÃºmero de telÃ©fono Ãºnicamente para atender su solicitud comercial. No compartimos su informaciÃ³n con terceros.</p>
<p>Contacto: <a href="mailto:danielprado@suminhn.com">danielprado@suminhn.com</a></p>
</body></html>""", media_type="text/html")
