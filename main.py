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
   Preguntar: tipo, diámetro y tamaño de caja. Los electrodos se venden por lb suelta o en cajas de 10 lbs / 50 lbs.
   El sistema consulta Zoho en tiempo real — si ves [INVENTARIO ZOHO] con precio, úsalo directamente.

   PRECIOS DE REFERENCIA — HIERROS DULCES (marca A.A., ISV incluido):
   - E6010: caja 10 lbs = L517.50 | caja 50 lbs = L2,587.50   (3/32", 1/8", 5/32")
   - E6011: caja 10 lbs = L517.50 | caja 50 lbs = L2,587.50   (3/32", 1/8", 5/32")
   - E6013: caja 10 lbs = L437.00 | caja 50 lbs = L2,185.00   (3/32", 1/8") — también hay marcas Lincoln, W.A.
   - E7018: caja 10 lbs = L414.00 | caja 50 lbs = L2,070.00   (3/32", 1/8", 5/32")
   - E7024: caja 10 lbs = L460.00 | caja 50 lbs = L2,300.00   (1/8")

   TIPOS ESPECIALES (también los manejamos — pedir precio exacto al asesor):
   - Inoxidable: E308-16, E309-16, E310-16, E312-16, E316-16, Tensile Weld
   - Revestimientos duros / hardfacing: E-300, E-900, Chrome Carb, Everwear 800, American Hard Plus, American Sugar
   - Aluminio: AL-345, AL-4043
   - Hierro colado: NI-55, NI-99
   - Bajo hidrógeno: E-8018, E-9018, E-11018M, E-12018M
   - Biselar/cortar: Chamfer Rod
   Para CUALQUIER electrodo especial o precio exacto que no tengas: → comunicar al +504 3334-0477

   Si el cliente pide FOTO de electrodos → "Para fotos y detalles técnicos de electrodos puede comunicarse al +504 3334-0477"

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

   Cuando el cliente pide foto de caretas u otro EPP: "Con gusto le mando fotos."
   (El bot enviará las fotos automáticamente — no necesitas decir nada más.)

4. MICROALAMBRE / ALAMBRE MIG:
   Preguntar: ¿con gas o sin gas? ¿qué diámetro? ¿marca actual?
   TIPOS DISPONIBLES (marca American Alloy y Washington Alloy):
   - ER70s-6 (acero al carbono, MIG con gas): 0.035" — rollo 1 lb o rollo 33 lbs
   - E71T-GS (flux core, sin gas): 0.030" y 0.035"
   - 600HT (flux core): 0.045" — rollo 33 lbs
   - Alambre aluminio ER4043/ER5356: 0.035"
   - Alambre acero inoxidable: 0.035"
   Para precio exacto: preguntar diámetro y presentación (1 lb o 33 lbs), luego consultar tienda o +504 3334-0477.
   Si el cliente tiene el producto actual: pedirle foto para identificar la referencia correcta.

5. VARILLAS (soldadura autógena y TIG):
   Disponibles: aluminio (liso y con fundente), acero inoxidable, bronce (lisa y revestida), hierro.
   Para precios y diámetros: llamar a tienda o +504 3334-0477.

6. OXICORTE / EQUIPO DE GAS:
   Kits disponibles:
   - Equipo Journeyman II Victor (profesional, servicio pesado)
   - Metal Power Super V-450 Deluxe (heavy duty, con maletín)
   Incluyen: cortador, maneral, reguladores, mangueras, antorcha, boquillas.
   Para precios: "Le comparto el precio — pásese por tienda o nos llama."

6. UBICACIÓN / DIRECCIÓN:
   📍 San Pedro Sula: 1ra calle, entre 1ra y 2da avenida, Edificio Metrocentro, Local #3
   https://maps.app.goo.gl/KUH7HU2idddQXCSPA
   📍 Tegucigalpa (Comayagüela): 8 calle, entre 3ra y 4ta avenida, frente a cafetería Macao, a la par del nuevo estacionamiento del Hospital Policlínica
   https://maps.app.goo.gl/2iNJW6wMDtKn68cg8

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
- NUNCA inventes precios. Si no lo sabés con certeza: "No tengo ese precio aquí ahora mismo, puede llamarnos o pasar por tienda."
- NO prometas enviar cotización formal si no podés.
- Si el cliente pregunta algo que no vendemos, díselo directamente sin rodeos.
- CIUDAD (San Pedro / Tegucigalpa): SOLO pregunta "¿Está en San Pedro o Tegucigalpa?" cuando el cliente ya confirmó que quiere el producto (dijo "lo llevo", "me interesa", "cuánto sería en total", "cómo pago", etc.). NO preguntes la ciudad al inicio de la consulta ni durante la presentación de productos.
"""

SUMIN_KEYWORDS  = ['soldar', 'soldadura', 'electrodo', 'mig', 'careta', 'guante',
                   'chaqueta', 'alambre', 'oxicorte', 'sumin', 'epp', 'protección',
                   'delantal', 'escudo', 'varilla', 'disco', 'lija', 'esmeril']

# ─── PRODUCT IMAGES ──────────────────────────────────────────────────────────
# URLs de imágenes hospedadas en GitHub (raw.githubusercontent.com).
# Para agregar fotos: corre upload_photos.py y pega las URLs generadas aquí.
# Formato: "keyword": ["url1", "url2", ...]
PRODUCT_IMAGES: dict[str, list[str]] = {
    # Se puebla con upload_photos.py — ver instrucciones abajo
}

# Palabras clave para detectar solicitudes de foto
PHOTO_KEYWORDS = ["foto", "fotos", "imagen", "imágen", "ver", "manda", "mandame",
                  "mándame", "muéstrame", "muestrame", "como es", "cómo es", "pic", "picture"]

# Productos que se REDIRIGEN al teléfono (no tenemos fotos del bot)
ELECTRODE_REDIRECT_PHONE = "+504 3334-0477"

def detect_photo_request(text: str) -> str | None:
    """Detect if user is asking for product photos. Returns product keyword or None."""
    text_lower = text.lower()
    if not any(w in text_lower for w in PHOTO_KEYWORDS):
        return None
    # Map product keywords to PRODUCT_IMAGES keys
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
        "delantal":    "delantal",
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

def send_product_photos(to: str, product_key: str) -> bool:
    """Send product photos via WhatsApp. Returns True if photos were sent."""
    # Electrodes → redirect to phone, don't send
    if "electrodo" in product_key or "electrode" in product_key:
        wa_send(to, f"Para fotos de electrodos puede comunicarse al {ELECTRODE_REDIRECT_PHONE} 📞")
        return True
    urls = PRODUCT_IMAGES.get(product_key, [])
    if not urls:
        return False  # No photos configured yet
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
    for url in urls[:3]:  # Max 3 photos per product
        result = wa_send_image_url(to, url, caption if sent == 0 else "")
        if result.get("messages"):
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
    """Send a product image from a public URL via WhatsApp."""
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

# ─── ZOHO BOOKS INTEGRATION ──────────────────────────────────────────────────
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
    Returns {"found": True, "names": [...], "rate": float, "unit": str}
         or {"found": False} or None on error.
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
            # Get the rate (price before ISV) from the best match
            rate  = items[0].get("rate", 0.0)
            unit  = items[0].get("unit", "")
            log_action("ZohoAPI", "item_found", f"'{query}' → {names} rate={rate}")
            return {"found": True, "names": names, "rate": rate, "unit": unit}
        log_action("ZohoAPI", "item_not_found", f"'{query}' → 0 results")
        return {"found": False}
    except Exception as e:
        log_action("ZohoAPI", "search_error", str(e))
        return None

def zoho_inventory_context(text: str) -> str:
    """If the message looks like a product inquiry, query Zoho and return context string."""
    inquiry_words = [
        "tienen", "hay", "disponible", "stock", "venden", "manejan",
        "precio", "cuánto", "cuanto", "tienen", "busco", "necesito",
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
                f"Del siguiente mensaje extrae SOLO el nombre o código del producto que pregunta el cliente. "
                f"Responde ÚNICAMENTE el nombre/código del producto, sin explicaciones. "
                f"Si no hay producto claro, responde 'NINGUNO'.\n\nMensaje: {text}"}]
        ).content[0].text.strip()
        if not extraction or extraction.upper() == "NINGUNO" or len(extraction) > 60:
            return ""
        result = zoho_check_item(extraction)
        if result is None:
            return ""   # Zoho unreachable — don't alter response
        if result["found"]:
            names_str = ", ".join(result["names"])
            rate      = result.get("rate", 0.0)
            unit      = result.get("unit", "")
            price_ctx = ""
            if rate and rate > 0:
                rate_with_isv = round(rate * 1.15, 2)
                price_ctx = (f" Precio base Zoho: L{rate}/{unit} (+ ISV 15% = L{rate_with_isv}/{unit}). "
                             f"Usa este precio como referencia REAL al responder.")
            return (f"\n\n[INVENTARIO ZOHO — DATO REAL]: El producto '{extraction}' SÍ existe en nuestro "
                    f"catálogo activo. Artículos: {names_str}.{price_ctx} "
                    f"Confirma disponibilidad y da precio con ISV incluido.")
        else:
            return (f"\n\n[INVENTARIO ZOHO — DATO REAL]: El producto '{extraction}' NO aparece en "
                    f"nuestro catálogo activo de Zoho Books. Informa amablemente que no manejamos "
                    f"ese artículo específico y ofrece alternativas si las hay.")
    except Exception as e:
        log_action("ZohoAPI", "context_error", str(e))
        return ""

# ════════════════════════════════════════════════════════════════════════════════
# ─── SUMIN AGENTS ────────────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════════

def sales_agent(from_number: str, from_name: str, text: str, state: dict):
    """SUMIN: Handle sales inquiries with real-time Zoho inventory check."""
    log_action("SalesAgent", "processing", f"{from_name}: {text}")
    if from_number not in state["conversations"]:
        state["conversations"][from_number] = []
    # Track contact name and last active time
    meta = get_conv_meta(state, from_number)
    if from_name and from_name != from_number:
        meta["name"] = from_name
    meta["last_active"] = datetime.now().isoformat()
    meta["last_msg"] = text[:80]
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
# ─── ZOHO ESTIMATES / COTIZACIONES ───────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════════

QUOTE_TRIGGERS = [
    "cotización", "cotizacion", "cotizame", "cotizar", "presupuesto",
    "estimación", "estimacion", "precio formal", "factura proforma",
    "me pueden cotizar", "me das una coti", "necesito una coti",
    "quiero una coti", "me hacen una coti", "pueden cotizar",
]

def detect_quote_request(text: str) -> bool:
    """Returns True if the customer is asking for a formal quote/estimate."""
    t = text.lower()
    return any(kw in t for kw in QUOTE_TRIGGERS)


def extract_items_for_quote(text: str, history: list) -> list[dict]:
    """Uses Claude Haiku to extract product names and quantities from conversation."""
    recent_ctx = ""
    for m in history[-6:]:
        role = "Cliente" if m["role"] == "user" else "Bot"
        recent_ctx += f"{role}: {m['content'][:200]}\n"

    prompt = (
        f"Del siguiente mensaje y conversación, extrae los productos y cantidades para una cotización.\n\n"
        f"Conversación reciente:\n{recent_ctx}\n"
        f"Mensaje actual: {text}\n\n"
        "Responde SOLO con JSON válido, ejemplo: "
        '[{"product": "careta basica 2 sensores", "quantity": 5}, {"product": "guantes", "quantity": 10}]\n'
        "Si no hay productos o cantidades claras, responde: []"
    )
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        content = resp.content[0].text.strip()
        match = re.search(r'\[.*?\]', content, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception as e:
        log_action("QuoteAgent", "extract_error", str(e))
    return []


def zoho_search_item_for_quote(product_name: str) -> dict | None:
    """Searches Zoho Books for an item and returns its id, name, rate."""
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
        if items:
            i = items[0]
            return {"item_id": i.get("item_id", ""), "name": i.get("item_name", ""), "rate": i.get("rate", 0.0), "unit": i.get("unit", "")}
    except Exception as e:
        log_action("ZohoAPI", "quote_search_error", str(e))
    return None


def zoho_create_estimate(customer_name: str, customer_phone: str, line_items: list[dict]) -> dict | None:
    """
    Creates a Zoho Books estimate.
    line_items: [{"item_id": str, "name": str, "quantity": int, "rate": float, "unit": str}]
    Returns the estimate dict (with estimate_id, estimate_number, total, etc.) or None.
    """
    token = get_zoho_access_token()
    if not token or not ZOHO_ORG_ID:
        return None
    formatted = [
        {"item_id": li["item_id"], "name": li["name"], "quantity": li["quantity"],
         "rate": li["rate"], "unit": li.get("unit", "")}
        for li in line_items
    ]
    payload = {
        "customer_name": customer_name,
        "line_items": formatted,
        "notes": f"Cotización vía WhatsApp — {customer_phone}",
        "terms": "Precios incluyen ISV (15%). Válida por 15 días.",
    }
    try:
        r = httpx.post(
            "https://www.zohoapis.com/books/v3/estimates",
            params={"organization_id": ZOHO_ORG_ID},
            headers={"Authorization": f"Zoho-oauthtoken {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
        if r.status_code == 201:
            est = r.json().get("estimate", {})
            log_action("ZohoAPI", "estimate_created", f"#{est.get('estimate_number')} for {customer_name}")
            return est
        log_action("ZohoAPI", "estimate_error", f"status={r.status_code} body={r.text[:300]}")
    except Exception as e:
        log_action("ZohoAPI", "estimate_exception", str(e))
    return None


def zoho_get_estimate_pdf(estimate_id: str) -> bytes | None:
    """Downloads the estimate as PDF bytes from Zoho Books."""
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
    """Uploads a file to WhatsApp media API and returns the media_id."""
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
    """Uploads and sends a document (PDF, etc.) via WhatsApp."""
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


def quote_agent(from_number: str, from_name: str, text: str, state: dict):
    """
    Handles formal quote requests:
    1. Extracts products/quantities from message + history
    2. Looks up items in Zoho
    3. Creates Zoho estimate
    4. Sends summary text + PDF to customer via WhatsApp
    """
    log_action("QuoteAgent", "start", f"{from_name}: {text[:80]}")
    history = state["conversations"].get(from_number, [])

    # Step 1: Extract what products the customer wants
    items_requested = extract_items_for_quote(text, history)

    if not items_requested:
        wa_send(
            from_number,
            "¡Con gusto le preparo la cotización! 📋\n\n"
            "Por favor indíqueme qué productos necesita y en qué cantidades. "
            "Ejemplo: *5 caretas básicas y 10 pares de guantes*.",
        )
        return

    # Step 2: Look up each item in Zoho
    line_items: list[dict] = []
    not_found: list[str] = []
    for req in items_requested:
        zoho_item = zoho_search_item_for_quote(req["product"])
        if zoho_item:
            line_items.append({**zoho_item, "quantity": max(1, int(req.get("quantity", 1)))})
        else:
            not_found.append(req["product"])

    if not line_items:
        wa_send(
            from_number,
            "No encontré esos productos en nuestro catálogo. "
            f"Por favor comuníquese al {ELECTRODE_REDIRECT_PHONE} para una cotización personalizada.",
        )
        return

    # Step 3: Create the Zoho estimate
    customer_name = from_name if from_name and from_name != from_number else "Cliente WhatsApp"
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

    # Step 4: Build and send summary message
    lines_txt = "\n".join(
        f"  • {li['quantity']} x {li['name']} — L{li['rate'] * li['quantity'] * 1.15:,.2f} c/ISV"
        for li in line_items
    )
    summary = (
        f"✅ *Cotización #{est_number}*\n\n"
        f"{lines_txt}\n\n"
        f"*Total (ISV incluido):* L{total:,.2f}\n\n"
        f"📄 Enviando el PDF ahora..."
    )
    if not_found:
        summary += f"\n\n⚠️ No encontramos en catálogo: {', '.join(not_found)}. Contáctenos para agregarlos."
    wa_send(from_number, summary)

    # Step 5: Send PDF
    pdf_bytes = zoho_get_estimate_pdf(est_id)
    if pdf_bytes:
        wa_send_document(from_number, pdf_bytes, f"Cotizacion_SUMIN_{est_number}.pdf", f"Cotización #{est_number} — SUMIN")
    else:
        wa_send(from_number, f"(No se pudo generar el PDF automáticamente. Su cotización #{est_number} está registrada en nuestro sistema.)")

    # Step 6: Update conversation history
    meta = get_conv_meta(state, from_number)
    if from_name and from_name != from_number:
        meta["name"] = from_name
    meta["last_active"] = datetime.now().isoformat()
    meta["last_msg"] = text[:80]
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": f"[Cotización #{est_number} generada. Total: L{total:,.2f}]"})
    state["conversations"][from_number] = history[-20:]
    save_state(state)
    log_action("QuoteAgent", "done", f"Estimate #{est_number} → {from_name} total=L{total:,.2f}")


# ════════════════════════════════════════════════════════════════════════════════
# ─── ORCHESTRATOR ────────────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════════

def orchestrate(message_data: dict):
    """Main dispatcher — routes directly to SUMIN agents (no multi-business routing)."""
    time.sleep(10)

    state     = load_state()
    from_number = message_data.get("from", "")
    from_name   = message_data.get("from_name", from_number)
    msg_type    = message_data.get("type", "text")

    log_action("Orchestrator", "received", f"from={from_name} type={msg_type}")

    # FulfillmentAgent always takes priority (Oficina SPS messages)
    if fulfillment_agent(message_data, state):
        return

    # ── IMAGE HANDLING ────────────────────────────────────────────────────────
    if msg_type == "image":
        media_id  = message_data.get("image", {}).get("id", "")
        mime_type = message_data.get("image", {}).get("mime_type", "image/jpeg")
        vision_agent(from_number, from_name, media_id, mime_type, state)
        return

    # ── TEXT HANDLING ─────────────────────────────────────────────────────────
    if msg_type == "text":
        text = message_data.get("text", {}).get("body", "")

        # Skip Zoho codes
        if re.fullmatch(r"[a-zA-Z]{2,5}\d{4,8}", text.strip()):
            log_action("Orchestrator", "skipped_zoho_code", text)
            return

        # Check for formal quote request → quote_agent generates Zoho estimate + PDF
        if detect_quote_request(text):
            quote_agent(from_number, from_name, text, state)
            return

        # Check for photo request — send images before/alongside text response
        photo_key = detect_photo_request(text)
        if photo_key:
            if "electrodo" in text.lower() or any(e in text.lower() for e in ["6010","6011","6013","7018","7024","tungsteno","inox"]):
                # Electrode photos → redirect to phone
                wa_send(from_number, f"Para fotos de electrodos puede comunicarse al {ELECTRODE_REDIRECT_PHONE} 📞")
                return
            photos_sent = send_product_photos(from_number, photo_key)
            if photos_sent:
                # Also let the sales agent respond with text context
                sales_agent(from_number, from_name, text, state)
                return
            # If no photos available yet, fall through to normal sales_agent

        sales_agent(from_number, from_name, text, state)
        return

    # ── DOCUMENT HANDLING ────────────────────────────────────────────────────
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
    """Format ISO timestamp for dashboard display."""
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

    # Build contact list sorted by most recent activity
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
        initials = ""
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

    # Log rows
    log_colors = {"SalesAgent":"#25d366","VisionAgent":"#2196F3","PaymentAgent":"#FF9800",
                  "FulfillmentAgent":"#9C27B0","Orchestrator":"#607D8B","WA_SEND":"#00BCD4",
                  "ZohoAPI":"#ff7043","PhotoAgent":"#ab47bc","Webhook":"#795548"}
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
/* SIDEBAR */
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
/* CHAT PANEL */
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
/* LOG */
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
// Auto-select first
const phones=Object.keys(C);
if(phones.length>0)setTimeout(()=>show(phones[0]),50);
// Reload page every 30s preserving selection
setTimeout(()=>{{const u=new URL(location.href);if(cur)u.searchParams.set('sel',cur);location.href=u;}},30000);
const sel=new URLSearchParams(location.search).get('sel');
if(sel&&C[sel])setTimeout(()=>show(sel),60);
</script>
</body></html>""".replace("{conversations_json}", conv_json).replace("{meta_json}", meta_json))

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
                f'<p><a href="{url}" style="font-size:20px">👉 Haz clic aquí para autorizar</a></p>'
                f'<p>Esto abrirá Zoho para que apruebes el acceso al inventario.</p></body></html>',
        media_type="text/html"
    )

@app.get("/zoho-callback")
async def zoho_callback(request: Request):
    """Exchange authorization code for refresh token."""
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
        access  = data.get("access_token", "")
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
                <br><br><p style="color:green">El bot ahora puede consultar el inventario de Zoho Books en tiempo real.</p>
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