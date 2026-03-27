"""
SUMIN WhatsApp Business API Bot
Multi-agent system: SalesAgent, VisionAgent, PaymentAgent, FulfillmentAgent
"""
import os, json, re, httpx, base64
from datetime import datetime
from fastapi import FastAPI, Request, Response, BackgroundTasks
from fastapi.responses import PlainTextResponse
import anthropic

app = FastAPI()

# ─── CONFIG ───────────────────────────────────────────────────────────────────
VERIFY_TOKEN      = os.environ["WA_VERIFY_TOKEN"]       # string que tú defines
WA_TOKEN          = os.environ["WA_ACCESS_TOKEN"]        # token permanente
PHONE_NUMBER_ID   = os.environ["WA_PHONE_NUMBER_ID"]     # 1090319730822008
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
STATE_FILE        = "orders_state.json"
LOG_FILE          = "bot_log.json"

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SKIP_NUMBERS = {
    "Sumin Oficina SPS", "Arnold Sumin", "Daniel Prado",
    "Confirmación de transferencias Sumin", "Servicio Al Cliente Boxful"
}

# ─── SYSTEM PROMPT ────────────────────────────────────────────────────────────
SUMIN_SYSTEM = """Eres Daniel Prado, agente de ventas de Suministros Internacionales HN (SUMIN).
Respondes en español, tono profesional pero amigable. NO uses "Estimado/a" para abrir.

UBICACIONES:
📍 San Pedro Sula: 1ra calle, entre 1ra y 2da avenida, Barrio Guamilito | https://maps.app.goo.gl/fRpNHwpqSPxHjYzP9
📍 Tegucigalpa (Comayagüela): 6a Av. entre 8a y 9a Calle, local #47 | https://maps.app.goo.gl/TegusLink

HORARIO: Lunes a Viernes 8am-5pm, Sábados 8am-12pm

ENVÍOS:
- Nacional: Expreco (1-2 días hábiles)
- Roatán, Guanaja, Utila: Island Shipping o Bahía Shipping
- Flete Tarifa A (SPS↔Tegucigalpa o SPS↔Puerto Cortés): L87 base + L1/lb adicional
- Flete Tarifa B (otros destinos): L174 base + L1.96/lb adicional
- Islas: cotizar directo con naviera

REGLAS CLAVE:
- Si el cliente pregunta por cotización, pedí: producto, cantidad, unidad, destino de envío
- Si mandó imagen de producto, identificá qué es y respondé con disponibilidad/precio
- Si mandó comprobante de pago, respondé: "Con gusto [nombre]! Recibimos su comprobante, ya lo procesamos ✅"
- Código W.A. de Zoho (formato: letras+números como "abc123"): NO es comprobante, ignorar
- NUNCA inventes precios que no conocés, decí "le consulto y le confirmo"
- Para consumibles MIG: solicitar foto del producto actual para identificar referencia correcta
"""

# ─── HELPERS ──────────────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {"orders": [], "conversations": {}}

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
        logs = logs[-200:]  # keep last 200 entries
        with open(LOG_FILE, "w") as f:
            json.dump(logs, f, indent=2, ensure_ascii=False)
    except:
        pass

def wa_send(to: str, text: str):
    """Send a WhatsApp text message."""
    url = f"https://graph.facebook.com/v22.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    body = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    r = httpx.post(url, json=body, headers=headers, timeout=15)
    log_action("WA_SEND", f"→ {to}", text[:100])
    return r.json()

def wa_forward_image(media_id: str, to: str):
    """Forward an image by media_id."""
    url = f"https://graph.facebook.com/v22.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    body = {"messaging_product": "whatsapp", "to": to, "type": "image", "image": {"id": media_id}}
    r = httpx.post(url, json=body, headers=headers, timeout=15)
    return r.json()

def wa_download_image(media_id: str) -> bytes:
    """Download image bytes from WhatsApp media."""
    headers = {"Authorization": f"Bearer {WA_TOKEN}"}
    # Get media URL
    r = httpx.get(f"https://graph.facebook.com/v22.0/{media_id}", headers=headers, timeout=15)
    media_url = r.json().get("url", "")
    if not media_url:
        return b""
    # Download image
    r2 = httpx.get(media_url, headers=headers, timeout=30)
    return r2.content

def is_comprobante(image_bytes: bytes, mime_type: str = "image/jpeg") -> bool:
    """Use Claude vision to determine if image is a payment receipt."""
    b64 = base64.standard_b64encode(image_bytes).decode()
    msg = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}},
                {"type": "text", "text": "¿Esta imagen es un comprobante/recibo de transferencia bancaria o pago? Responde SOLO 'SI' o 'NO'."}
            ]
        }]
    )
    answer = msg.content[0].text.strip().upper()
    return answer == "SI"

def identify_product(image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    """Use Claude vision to identify a product in an image."""
    b64 = base64.standard_b64encode(image_bytes).decode()
    msg = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system=SUMIN_SYSTEM,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}},
                {"type": "text", "text": "Identifica qué producto de soldadura/EPP/oxicorte es este. Dame nombre técnico, especificaciones visibles y si lo manejamos en SUMIN."}
            ]
        }]
    )
    return msg.content[0].text

def claude_respond(conversation_history: list, new_message: str) -> str:
    """Generate a sales response using Claude."""
    history = conversation_history[-10:]  # last 10 messages for context
    messages = history + [{"role": "user", "content": new_message}]
    msg = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=SUMIN_SYSTEM,
        messages=messages
    )
    return msg.content[0].text

# ─── AGENTS ───────────────────────────────────────────────────────────────────

def sales_agent(from_number: str, from_name: str, text: str, state: dict):
    """Handle sales inquiries — respond to text messages."""
    log_action("SalesAgent", "processing", f"{from_name}: {text}")

    conv_key = from_number
    if conv_key not in state["conversations"]:
        state["conversations"][conv_key] = []

    history = state["conversations"][conv_key]
    response = claude_respond(history, text)

    # Update history
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": response})
    state["conversations"][conv_key] = history[-20:]  # keep last 20

    wa_send(from_number, response)
    log_action("SalesAgent", "sent_response", response[:100])
    save_state(state)

def vision_agent(from_number: str, from_name: str, media_id: str, mime_type: str, state: dict):
    """Handle image messages — detect comprobante vs product image."""
    log_action("VisionAgent", "processing_image", f"{from_name} sent image {media_id}")

    image_bytes = wa_download_image(media_id)
    if not image_bytes:
        log_action("VisionAgent", "error", "Could not download image")
        return

    # Check if it's a payment receipt
    if is_comprobante(image_bytes, mime_type):
        log_action("VisionAgent", "detected", "COMPROBANTE → dispatching PaymentAgent")
        payment_agent(from_number, from_name, media_id, image_bytes, state)
    else:
        # It's a product image
        log_action("VisionAgent", "detected", "PRODUCT IMAGE → identifying")
        product_info = identify_product(image_bytes, mime_type)
        response = f"Identificamos el producto:\n\n{product_info}\n\n¿Cuántas unidades necesita y para qué ciudad es el envío?"
        wa_send(from_number, response)
        log_action("VisionAgent", "sent_product_response", product_info[:100])

def payment_agent(from_number: str, from_name: str, media_id: str, image_bytes: bytes, state: dict):
    """Handle payment comprobante — forward to groups, request invoice."""
    log_action("PaymentAgent", "processing", f"Comprobante from {from_name}")

    # 1. Respond to client
    client_name = from_name.split()[0] if from_name else "estimado cliente"
    wa_send(from_number, f"Con gusto {client_name}! Recibimos su comprobante, ya lo procesamos ✅")

    # 2. Forward image to Confirmación de transferencias group
    # Group numbers need to be configured — using their IDs from WhatsApp
    CONFIRMACION_GROUP = os.environ.get("WA_CONFIRMACION_GROUP", "")
    OFICINA_SPS_NUMBER = os.environ.get("WA_OFICINA_SPS", "")

    if CONFIRMACION_GROUP:
        wa_forward_image(media_id, CONFIRMACION_GROUP)
        log_action("PaymentAgent", "forwarded_to_confirmacion", CONFIRMACION_GROUP)

    # 3. Find related quote for this client
    order = None
    for o in state.get("orders", []):
        if o.get("client") == from_number and o.get("status") in ["quote_sent", "pending"]:
            order = o
            break

    # 4. Send to Sumin Oficina SPS: comprobante + quote info
    if OFICINA_SPS_NUMBER:
        wa_forward_image(media_id, OFICINA_SPS_NUMBER)
        if order:
            quote_info = f"📋 Pago recibido de {from_name} ({from_number})\nCotización: {order.get('quote', 'N/A')}\nMonto: L{order.get('amount', '?')}\n\nFavor procesar y enviar factura + guía de envío."
        else:
            quote_info = f"📋 Pago recibido de {from_name} ({from_number})\nFavor procesar y enviar factura + guía de envío."
        wa_send(OFICINA_SPS_NUMBER, quote_info)
        log_action("PaymentAgent", "notified_oficina_sps", quote_info[:100])

    # 5. Update order status
    if order:
        order["status"] = "payment_received"
        order["payment_date"] = datetime.now().isoformat()
    else:
        state["orders"].append({
            "client": from_number,
            "name": from_name,
            "status": "payment_received",
            "payment_date": datetime.now().isoformat()
        })

    save_state(state)

def fulfillment_agent(message_data: dict, state: dict):
    """
    Monitors messages FROM Sumin Oficina SPS.
    If it contains a factura or guía, matches to client and forwards.
    """
    OFICINA_SPS_NUMBER = os.environ.get("WA_OFICINA_SPS", "")
    from_number = message_data.get("from", "")

    # Only process messages from Sumin Oficina SPS
    if from_number != OFICINA_SPS_NUMBER:
        return False

    log_action("FulfillmentAgent", "checking_message", "Message from Oficina SPS")

    msg_type = message_data.get("type", "")
    text = ""
    if msg_type == "text":
        text = message_data.get("text", {}).get("body", "")

    # Look for factura/guía keywords
    keywords = ["factura", "guía", "guia", "envío", "envio", "tracking", "número de guía"]
    is_fulfillment = any(k in text.lower() for k in keywords) or msg_type == "document"

    if not is_fulfillment:
        return False

    log_action("FulfillmentAgent", "detected_fulfillment", f"type={msg_type}, text={text[:50]}")

    # Find most recent client with payment_received status
    pending_orders = [o for o in state.get("orders", []) if o.get("status") == "payment_received"]
    if not pending_orders:
        log_action("FulfillmentAgent", "no_pending_orders", "No orders to match")
        return True

    # Match to oldest pending order
    pending_orders.sort(key=lambda x: x.get("payment_date", ""))
    order = pending_orders[0]
    client_number = order.get("client")

    # Forward the message to the client
    if msg_type == "document":
        media_id = message_data.get("document", {}).get("id")
        if media_id:
            wa_forward_image(media_id, client_number)
    elif msg_type == "image":
        media_id = message_data.get("image", {}).get("id")
        if media_id:
            wa_forward_image(media_id, client_number)

    if text:
        wa_send(client_number, f"📦 Su pedido está en camino!\n{text}")

    order["status"] = "shipped"
    order["shipped_date"] = datetime.now().isoformat()
    save_state(state)

    log_action("FulfillmentAgent", "forwarded_to_client", f"→ {client_number}")
    return True

# ─── ORCHESTRATOR ─────────────────────────────────────────────────────────────

def orchestrate(message_data: dict):
    """Main dispatcher — classifies message and routes to correct agent."""
    state = load_state()

    from_number = message_data.get("from", "")
    from_name = message_data.get("from_name", from_number)
    msg_type = message_data.get("type", "text")

    log_action("Orchestrator", "received", f"from={from_name} type={msg_type}")

    # Skip internal SUMIN numbers
    OFICINA_SPS_NUMBER = os.environ.get("WA_OFICINA_SPS", "")

    # Check if message is from Oficina SPS (fulfillment flow)
    if fulfillment_agent(message_data, state):
        return

    # Route by message type
    if msg_type == "image":
        media_id = message_data.get("image", {}).get("id", "")
        mime_type = message_data.get("image", {}).get("mime_type", "image/jpeg")
        vision_agent(from_number, from_name, media_id, mime_type, state)

    elif msg_type == "text":
        text = message_data.get("text", {}).get("body", "")

        # Quick check: is it a Zoho code? (e.g. "abc1234") — ignore
        if re.fullmatch(r"[a-zA-Z]{2,5}\d{4,8}", text.strip()):
            log_action("Orchestrator", "skipped_zoho_code", text)
            return

        sales_agent(from_number, from_name, text, state)

    elif msg_type == "document":
        # Could be a quote PDF or other doc — treat as sales context
        doc = message_data.get("document", {})
        filename = doc.get("filename", "")
        sales_agent(from_number, from_name, f"[Documento adjunto: {filename}]", state)

    else:
        log_action("Orchestrator", "skipped", f"unsupported type: {msg_type}")

# ─── WEBHOOK ENDPOINTS ────────────────────────────────────────────────────────

@app.get("/webhook")
async def verify_webhook(request: Request):
    """Meta webhook verification."""
    params = dict(request.query_params)
    mode      = params.get("hub.mode")
    token     = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        log_action("Webhook", "verified", "Meta webhook verification OK")
        return PlainTextResponse(challenge)
    return Response(status_code=403)

@app.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receive incoming WhatsApp messages from Meta."""
    body = await request.json()

    try:
        entry = body["entry"][0]
        changes = entry["changes"][0]["value"]
        messages = changes.get("messages", [])
        contacts = changes.get("contacts", [])

        name_map = {c["wa_id"]: c["profile"]["name"] for c in contacts}

        for msg in messages:
            msg["from_name"] = name_map.get(msg.get("from", ""), msg.get("from", ""))
            background_tasks.add_task(orchestrate, msg)

    except (KeyError, IndexError):
        pass  # Status updates, etc.

    return {"status": "ok"}

# ─── DASHBOARD ────────────────────────────────────────────────────────────────

@app.get("/dashboard")
async def dashboard():
    """Simple HTML dashboard to monitor agents."""
    try:
        with open(LOG_FILE) as f:
            logs = json.load(f)
    except:
        logs = []

    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except:
        state = {"orders": [], "conversations": {}}

    logs_html = ""
    for entry in reversed(logs[-50:]):
        color = {
            "SalesAgent": "#4CAF50",
            "VisionAgent": "#2196F3",
            "PaymentAgent": "#FF9800",
            "FulfillmentAgent": "#9C27B0",
            "Orchestrator": "#607D8B",
            "WA_SEND": "#00BCD4",
            "Webhook": "#795548"
        }.get(entry["agent"], "#999")

        logs_html += f"""
        <tr>
          <td style='color:#888;font-size:12px'>{entry['timestamp'][11:19]}</td>
          <td><span style='background:{color};color:white;padding:2px 8px;border-radius:10px;font-size:12px'>{entry['agent']}</span></td>
          <td style='font-size:13px'>{entry['action']}</td>
          <td style='font-size:12px;color:#555'>{entry['detail'][:80]}</td>
        </tr>"""

    orders_html = ""
    status_icons = {"quote_sent": "📄", "payment_received": "💰", "shipped": "📦", "pending": "⏳"}
    for o in state.get("orders", []):
        icon = status_icons.get(o.get("status", ""), "❓")
        orders_html += f"<tr><td>{o.get('name','')}</td><td>{o.get('client','')}</td><td>{icon} {o.get('status','')}</td><td>{o.get('quote','')}</td><td>{o.get('payment_date','')[:10]}</td></tr>"

    return Response(content=f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'><title>SUMIN Bot Dashboard</title>
<meta http-equiv='refresh' content='15'>
<style>body{{font-family:sans-serif;background:#1a1a2e;color:#eee;margin:0;padding:20px}}
h1{{color:#4CAF50}}h2{{color:#aaa;font-size:16px}}
table{{width:100%;border-collapse:collapse;background:#16213e;border-radius:8px;overflow:hidden;margin-bottom:20px}}
th{{background:#0f3460;padding:10px;text-align:left;font-size:13px}}
td{{padding:8px 10px;border-bottom:1px solid #0a2040}}
</style></head>
<body>
<h1>🤖 SUMIN Bot Dashboard</h1>
<p style='color:#888'>Auto-refresh cada 15s | {datetime.now().strftime('%H:%M:%S')}</p>

<h2>📦 Órdenes activas ({len(state.get('orders',[]))})</h2>
<table><tr><th>Cliente</th><th>Número</th><th>Status</th><th>Cotización</th><th>Fecha pago</th></tr>
{orders_html or "<tr><td colspan=5 style='color:#555'>Sin órdenes</td></tr>"}
</table>

<h2>📋 Log de agentes (últimas 50 acciones)</h2>
<table><tr><th>Hora</th><th>Agente</th><th>Acción</th><th>Detalle</th></tr>
{logs_html or "<tr><td colspan=4 style='color:#555'>Sin actividad</td></tr>"}
</table>
</body></html>""", media_type="text/html")

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}
