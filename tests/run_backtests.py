"""Backtests for the SUMIN bot.

Run with:  python tests/run_backtests.py

Strategy: most regressions Daniel has reported can be caught by exercising
PURE functions (regex, normalization, dictionary lookups) without hitting
Anthropic. The LLM-dependent paths are exercised with stubbed clients that
return canned JSON, so we validate the *plumbing* (parsing, fallbacks,
state mutation) — not the LLM itself.

Add a new case here for every bug Daniel reports. Then run before each
deploy:
    python tests/run_backtests.py
The script returns exit code 0 on full pass, 1 on any failure, and prints
a short summary.
"""
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Make sure we import the bot's main.py from the parent dir.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Avoid the bot trying to call real APIs at import time. Provide dummy env
# vars before importing main.
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")
os.environ.setdefault("WA_ACCESS_TOKEN", "dummy")
os.environ.setdefault("WA_PHONE_NUMBER_ID", "0")
os.environ.setdefault("WA_VERIFY_TOKEN", "dummy")
os.environ.setdefault("ZOHO_CLIENT_ID", "dummy")
os.environ.setdefault("ZOHO_CLIENT_SECRET", "dummy")
os.environ.setdefault("ZOHO_REFRESH_TOKEN", "dummy")
os.environ.setdefault("ZOHO_ORG_ID", "dummy")
os.environ.setdefault("OPENAI_API_KEY", "dummy")
os.environ.setdefault("INTERNAL_API_TOKEN", "dummy")
os.environ.setdefault("CONSOLE_API_URL", "http://localhost")

import main  # noqa: E402


# ─── tiny harness ──────────────────────────────────────────────────────────
PASS, FAIL = "✅", "❌"
results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append((label, ok, detail))
    sym = PASS if ok else FAIL
    print(f"{sym}  {label}" + (f"  — {detail}" if detail and not ok else ""))


def section(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


# ─── 1. Pure regex / dict / heuristics ─────────────────────────────────────
section("1. Heurísticas puras (sin LLM)")

# 1.1 Confirm keyword word-boundary (the famous "tensile" → "si" bug).
check(
    "'tensile' NO es confirm (false positive del v15.2)",
    not main._is_confirm_message("tensile"),
)
check(
    "'sí' simple SÍ es confirm",
    main._is_confirm_message("sí"),
)
check(
    "'todo bien' SÍ es confirm",
    main._is_confirm_message("todo bien"),
)
check(
    "'cancelar' NO es confirm",
    not main._is_confirm_message("cancelar"),
)

# 1.2 mm → fracción (v17 normalization).
check(
    "'2.4 mm' → '3/32'",
    "3/32" in main._normalize_query_for_search("electrodo 7018 2.4 mm"),
)
check(
    "'8 mm' → '5/16'",
    "5/16" in main._normalize_query_for_search("varilla carbon 8 mm"),
)
check(
    "'1.8' (typo de 1/8) → '1/8'",
    "1/8" in main._normalize_query_for_search("50 lbs de 7018 1.8"),
)

# 1.3 Sinónimos.
check(
    "'manometro' (sin specs) → 'regulador'",
    "regulador" in main._normalize_query_for_search("manometro para acetileno").lower(),
)
check(
    "'manometro 0-30 1-1/2' (con specs) mantiene literal y agrega regulador",
    "manometro" in main._normalize_query_for_search("manometro 0-30 1-1/2 PSI").lower()
    and "regulador" in main._normalize_query_for_search("manometro 0-30 1-1/2 PSI").lower(),
)
check(
    "'carbon' → 'arcair' (sinónimo)",
    "arcair" in main._normalize_query_for_search("varilla carbon 8 mm").lower(),
)
check(
    "'vidrio' → agrega 'careta' (v20)",
    "careta" in main._normalize_query_for_search("vidrio #12").lower(),
)

# 1.4 Detección de aclaración técnica vs nombre de cliente (v20.1).
check(
    "'son boquillas de corte' detectado como aclaración",
    main._looks_like_product_clarification("son boquillas de corte"),
)
check(
    "'es para soldar' detectado como aclaración",
    main._looks_like_product_clarification("es para soldar"),
)
check(
    "'1-101- de acetileno' detectado como aclaración",
    main._looks_like_product_clarification("1-101- de acetileno"),
)
check(
    "'Aceites y Derivados' NO es aclaración (es nombre)",
    not main._looks_like_product_clarification("Aceites y Derivados"),
)
check(
    "'Azucarera del Norte' NO es aclaración (es nombre)",
    not main._looks_like_product_clarification("Azucarera del Norte"),
)
# v25: bug del 11-may-26 — "de 33 libras es el producto" se aceptó como nombre.
# El regex original solo aceptaba "\d+\s*lb" (sin la s) y no tenía "producto"
# ni "rollo" / "microalambre" como triggers. Fix verificado abajo.
check(
    "v25: 'de 33 libras es el producto' detectado como aclaración (no nombre)",
    main._looks_like_product_clarification("de 33 libras es el producto"),
)
check(
    "v25: '33 lbs' detectado como aclaración",
    main._looks_like_product_clarification("33 lbs"),
)
check(
    "v25: 'er70s-6 de 33 lbs' (correction-style reply) detectado como aclaración",
    main._looks_like_product_clarification("er70s-6 de 33 lbs"),
)
check(
    "v25: 'rollo de microalambre' detectado como aclaración",
    main._looks_like_product_clarification("rollo de microalambre"),
)
# v25: regresión guard — nombres reales con números o sufijos S.A. que NO
# deben pasar como aclaración aunque tengan algún token sospechoso.
check(
    "v25: 'Constructora García S.A.' NO es aclaración (regression guard)",
    not main._looks_like_product_clarification("Constructora García S.A."),
)

# v25: weight canonicalization in query normalizer. Customers write the rollo
# weight in many shapes; we canonicalize to "<N> lbs" before tokenization so
# the prefilter substring-matches against Zoho's "33 LBS" names.
check(
    "v25: '33LB' → '33 lbs' (canonicalizado)",
    "33 lbs" in main._normalize_query_for_search("rollo microalambre 33LB er70s-6"),
)
check(
    "v25: '33 libras' → '33 lbs' (canonicalizado)",
    "33 lbs" in main._normalize_query_for_search("33 libras de microalambre"),
)
check(
    "v25: '11 LBS' (ya correcto) se mantiene como '11 lbs'",
    "11 lbs" in main._normalize_query_for_search("11 LBS rollo"),
)
# Guard: no canonicalizar palabras que NO son unidad (ej. "33lbX" donde sigue
# una letra) — el regex tiene \b al final.
check(
    "v25: '33lbX' (no es unidad) NO se canonicaliza",
    "33lbX" in main._normalize_query_for_search("33lbX no es peso"),
)
# v25.2: bug del 11-may-26 (parte 2) — el matcher devolvió NINGUNO para
# "MICROALAMBRE 33 LB" porque el catálogo lo tiene como "MICROALAMBRE
# COBRIZADO ER70S-6 ... 33 LBS". Verificamos que el sinónimo está agregado
# para que el prefilter incluya el variant correcto.
_norm = main._normalize_query_for_search("rollo microalambre 0.035 33LB er70s-6")
check(
    "v25.2: 'microalambre' expande sinónimo 'cobrizado' para matchear catálogo",
    "cobrizado" in _norm.lower(),
)
check(
    "v25.2: 'microalambre' expande sinónimo 'cobriz' (variant 0.045 33 LBS)",
    "cobriz" in _norm.lower(),
)
# Regression guard: si el query NO tiene "microalambre", NO agregamos cobrizado.
check(
    "v25.2: query sin microalambre NO agrega cobrizado",
    "cobrizado" not in main._normalize_query_for_search("electrodo 6011 1/8").lower(),
)

# 1.5 Hint extraction.
check(
    "'son de corte' → 'para oxicorte'",
    main._extract_clarification_hint("son boquillas de corte") == "para oxicorte",
)
check(
    "'para soldar' → 'para soldar'",
    main._extract_clarification_hint("son boquillas para soldar") == "para soldar",
)

# 1.6 MIG handoff regex (v22).
re_match = main._MIG_HANDOFF_REPLY_RE.match("1")
check("MIG handoff: '1' parsea OK", re_match and re_match.group(1) == "1")
re_match = main._MIG_HANDOFF_REPLY_RE.match("2: le aclaramos que tenemos en stock")
check(
    "MIG handoff: '2: nota' parsea con extra",
    re_match
    and re_match.group(1) == "2"
    and "le aclaramos" in (re_match.group(2) or ""),
)
re_match = main._MIG_HANDOFF_REPLY_RE.match("hola buenos días")
# This DOES match because "hola" doesn't start with a digit, but our string
# starts with 'h'. So we expect None.
check(
    "MIG handoff: texto sin número inicial NO parsea",
    re_match is None,
)

# 1.7 Trusted numbers.
check(
    "Daniel (50497041381) es trusted",
    main.is_trusted_number("50497041381"),
)
check(
    "Cliente random no es trusted",
    not main.is_trusted_number("50412345678"),
)


# ─── 2. _looks_like_mig_consumable ─────────────────────────────────────────
section("2. Detección de consumibles MIG")

check(
    "'difusor para antorcha MIG' detectado",
    main._looks_like_mig_consumable("difusor para antorcha MIG"),
)
check(
    "'gas diffuser' detectado",
    main._looks_like_mig_consumable("gas diffuser de bronce"),
)
check(
    "'electrodo 6011' NO es consumible MIG",
    not main._looks_like_mig_consumable("electrodo 6011 de 1/8"),
)


# ─── 3. extract_items_for_quote (con LLM mockeado) ─────────────────────────
section("3. extract_items_for_quote (Anthropic mockeado)")


def fake_anthropic_response(json_payload: dict) -> MagicMock:
    """Build a mock that mimics the Anthropic SDK response shape."""
    mock = MagicMock()
    mock.content = [MagicMock(text=json.dumps(json_payload))]
    return mock


# 3.1 Simulamos audio con 4 items — confirma que el bot devuelve los 4.
fake_4items = {
    "items": [
        {"product": "electrodo 6011 1/8", "quantity": 100, "unit": "LB"},
        {"product": "electrodo 7018 1/8", "quantity": 50, "unit": "LB"},
        {"product": "electrodo 309-16 3/32", "quantity": 10, "unit": "LB"},
        {"product": "boquilla #2 victor para corte", "quantity": 5, "unit": "UND"},
    ],
    "customer_name": "",
}
main.claude.messages.create = MagicMock(return_value=fake_anthropic_response(fake_4items))
items, name = main.extract_items_for_quote("audio largo con 4 items", [])
check(
    f"4 items pedidos → 4 items extraídos (got {len(items)})",
    len(items) == 4,
)
check(
    "El último item NO se perdió (boquilla #2)",
    any("boquilla" in (it.get("product") or "").lower() for it in items),
)

# 3.2 LB inferido para electrodos sin unit.
fake_no_unit = {
    "items": [
        {"product": "electrodo 6011 1/8", "quantity": 100, "unit": ""},
    ],
    "customer_name": "",
}
main.claude.messages.create = MagicMock(return_value=fake_anthropic_response(fake_no_unit))
items, _ = main.extract_items_for_quote("100 de 6011 1/8", [])
check(
    "Electrodo sin unit explícita → backfill a LB",
    items and items[0].get("unit") == "LB",
)

# 3.3 Tungsteno sin unit → UND.
fake_tig = {
    "items": [
        {"product": "tungsteno cerio 3/32", "quantity": 10, "unit": ""},
    ],
    "customer_name": "",
}
main.claude.messages.create = MagicMock(return_value=fake_anthropic_response(fake_tig))
items, _ = main.extract_items_for_quote("10 tungsteno cerio 3/32", [])
check(
    "Tungsteno sin unit → backfill a UND",
    items and items[0].get("unit") == "UND",
)


# ─── 4. _quantity_question_for_product ─────────────────────────────────────
section("4. Decisión LB vs UND para electrodos")

q = main._quantity_question_for_product("electrodo 6011 1/8")
check("6011 → pregunta libras", "libra" in q.lower())

q = main._quantity_question_for_product("tungsteno wt-20 3/32")
check("Tungsteno → pregunta unidades (no libras)", "unidad" in q.lower())

q = main._quantity_question_for_product("INCONEL NiCrFe-3 1/8")
check("INCONEL → pregunta libras", "libra" in q.lower())


# ─── 5. Flete Expreco (v30.3 — puro, usa data/expreco_rates.json real) ──────
section("5. Flete Expreco")

q = main._detect_flete_query("que vale envío a márcala la paz 8 libras")
check("'envío a márcala la paz 8 libras' → destino MARCALA",
      q and q["row"] and q["row"]["destino"] == "MARCALA")
check("  peso detectado = 8 lbs", q and q["peso_lbs"] == 8)
check("  origen default = SPS", q and q["origen"] == "SAN PEDRO SULA")
if q and q["row"]:
    calc = main.calcular_flete(q["row"], q["peso_lbs"])
    check("  total = 188.10 + 3×2.25 = L194.85", abs(calc["total"] - 194.85) < 0.01,
          f"got {calc['total']}")

q = main._detect_flete_query("cuanto cuesta el envio a tela 50 lbs")
check("'envio a tela 50 lbs' → TELA, total L145.37",
      q and q["row"] and q["row"]["destino"] == "TELA"
      and abs(main.calcular_flete(q["row"], q["peso_lbs"])["total"] - 145.37) < 0.01)

q = main._detect_flete_query("flete a santiago tela de una caja")
check("'santiago tela' le gana a 'tela' (substring más largo) y es tarifa fija",
      q and q["row"] and q["row"]["destino"] == "SANTIAGO TELA"
      and q["row"]["tipo"] == 3
      and abs(main.calcular_flete(q["row"], None)["total"] - 273.93) < 0.01)

q = main._detect_flete_query("envío a tegucigalpa 8 libras")
check("'envío a tegucigalpa' → destino TGU con ORIGEN SPS (no confundir)",
      q and q["row"] and q["row"]["destino"] == "TEGUCIGALPA"
      and q["origen"] == "SAN PEDRO SULA"
      and abs(main.calcular_flete(q["row"], q["peso_lbs"])["total"] - (94.07 + 3 * 1.14)) < 0.01)

q = main._detect_flete_query("envío a choloma 10 kg")
check("kg → lbs: 10 kg ≈ 22 lbs para el cálculo", q and abs(q["peso_lbs"] - 22.0) < 0.1)

q = main._detect_flete_query("envío a marcala")
check("sin peso → total None (pregunta cuántas libras)",
      q and q["row"] and main.calcular_flete(q["row"], None)["total"] is None)

check("sin keyword de envío NO dispara ('tienen electrodo en marcala')",
      main._detect_flete_query("tienen electrodo en marcala") is None)
check("'hola buen dia' NO dispara", main._detect_flete_query("hola buen dia") is None)
check("'quiero una cotización de 6011' NO dispara",
      main._detect_flete_query("quiero una cotización de 6011") is None)
check("keyword sin destino conocido NO dispara ('me hacen envíos?')",
      main._detect_flete_query("me hacen envíos?") is None)

check("'agregar flete' matchea el regex de agregar",
      bool(main._ADD_FLETE_RE.search("agregar flete")) and
      bool(main._ADD_FLETE_RE.search("agregue el flete a la cotización")) and
      not main._ADD_FLETE_RE.search("cuanto vale el flete"))

check("modo asistente: trusted tiene módulo, cliente no",
      "ASISTENTE INTERNO" in main._build_internal_assistant_context("50497041381")
      and main._build_internal_assistant_context("50412345678") == "")


# ─── 6. Peso de presentación — constraint duro (v30.4, caso EST-007072) ────
section("6. Peso de presentación (v30.4)")

_cat_2lb  = {"item_name": 'MICROALAMBRE E71T-GS 0.030" 2 LBS A.A.', "sku": "MA-2", "rate": 298.13}
_cat_10lb = {"item_name": 'MICROALAMBRE E71T-GS 0.030"(0.8mm) 10 LBS', "sku": "MA-10", "rate": 1045.44}
_cat_elec = {"item_name": "ELECTRODO 6011 1/8 A.A.", "sku": "E-6011", "rate": 45.0}
_focused = [_cat_10lb, _cat_2lb, _cat_elec]

# Caso real EST-007072: pidió 2 lbs, Haiku devolvió 10 LBS → swap al de 2 LBS
got = main._enforce_weight_match("microalambre e71t-gs 0.030 2 lbs", _cat_10lb, _focused)
check("EST-007072: pidió 2 lbs + matcher devolvió 10 LBS → swap a 2 LBS",
      got is _cat_2lb, f"got {got and got.get('sku')}")

# Peso coincide → no tocar
got = main._enforce_weight_match("microalambre 0.030 10 lbs", _cat_10lb, _focused)
check("peso coincide (10 lbs) → se mantiene", got is _cat_10lb)

# Pidió peso que no existe (5 lbs) y el elegido tiene otro → NINGUNO (preguntar)
got = main._enforce_weight_match("rollo e71t-gs 0.030 5 lbs", _cat_10lb, [_cat_10lb, _cat_elec])
check("peso inexistente (5 lbs) sin variante correcta → None (pregunta)", got is None)

# Electrodo por libra: '50 lbs de 6011' — el 50 es CANTIDAD, no presentación → no tocar
got = main._enforce_weight_match("electrodo 6011 1/8 50 lbs", _cat_elec, _focused)
check("electrodo '50 lbs de 6011' (cantidad, no rollo) → se mantiene", got is _cat_elec)

# Query sin peso → no aplica
got = main._enforce_weight_match("microalambre e71t-gs 0.030", _cat_10lb, _focused)
check("query sin peso → se mantiene", got is _cat_10lb)

# Rollo sin peso en nombre del elegido pero query pide 2 lbs → swap en contexto rollo
_cat_noweight = {"item_name": "MICROALAMBRE E71T-GS 0.030", "sku": "MA-X", "rate": 500.0}
got = main._enforce_weight_match("rollo microalambre 0.030 2 lbs", _cat_noweight, _focused)
check("elegido sin peso en nombre + contexto rollo → swap al de 2 LBS", got is _cat_2lb)


# ─── 7. Flete + cotización combinados (v30.5) ───────────────────────────────
section("7. Flete dentro de cotización (v30.5)")

# El bug del 02-nov: "cotizame 1 lb de E308-16 3/32 y una caja de 10 lbs de
# 6011 1/8 con envío hacia Morazán" — flete_agent se tragaba el turno.
_txt = "cotizame una libra de e308-16 3/32 y una caja de 10 libras de 6011 1/8 con envío hacia morazan"
check("mensaje items+envío ES quote request (no debe interceptarse)",
      main.detect_quote_request(_txt))
_state = {"conversations": {}, "_meta": {}}
main._TEST_STATE.capture = []
_res = main.flete_agent("50499999999", _txt, _state)
_meta = main.get_conv_meta(_state, "50499999999")
check("flete_agent devuelve False (pasa el turno al quote flow)", _res is False)
check("  y guarda flete_ctx con destino MORAZAN",
      (_meta.get("flete_ctx") or {}).get("destino") == "MORAZAN")
check("  sin mandar ningún mensaje", len(main._TEST_STATE.capture) == 0)
main._TEST_STATE.capture = None

check("_expreco_rate_for SPS→MORAZAN existe",
      main._expreco_rate_for("SAN PEDRO SULA", "MORAZAN") is not None)

# Peso estimado del pedido
_lis = [
    {"name": "ELECTRODO E308L-16 3/32 A.A.", "quantity": 1, "rate": 300, "unit": "LB"},
    {"name": "ELECTRODO 6011 1/8 A.A.", "quantity": 10, "rate": 51.75, "unit": "LB"},
]
check("peso pedido: 1 LB + 10 LB = 11 lbs",
      abs(main._estimate_order_weight_lbs(_lis) - 11.0) < 0.01)
_lis2 = [
    {"name": 'MICROALAMBRE E71T-GS 0.030" 2 LBS A.A.', "quantity": 8, "rate": 298, "unit": "UND"},
    {"name": "TOBERA MILLER ML-N-M1200C A.A.", "quantity": 2, "rate": 310, "unit": "UND"},
]
check("peso pedido: 8 rollos × 2 LBS (peso en nombre) + accesorios = 16 lbs",
      abs(main._estimate_order_weight_lbs(_lis2) - 16.0) < 0.01)

# flete SOLO (sin items) sigue respondiendo directo
main._TEST_STATE.capture = []
_res = main.flete_agent("50499999998", "que vale el envío a morazan 10 libras", _state)
check("flete solo (sin cotización) sigue respondiendo directo",
      _res is True and len(main._TEST_STATE.capture) == 1
      and "Morazan" in main._TEST_STATE.capture[0]["text"])
main._TEST_STATE.capture = None


# ─── 8. Contexto Zoho multi-producto (v30.7, caso tungstenos 1/8 y 3/32) ───
section("8. Contexto Zoho multi-producto (v30.7)")

_fake_catalog = [
    {"item_name": "ELECTRODO PARA TIG TUNGSTENO DE 1/8 (CERIO 2%) GRIS",
     "sku": "6030002", "rate": 280, "unit": "UND", "stock_on_hand": 544},
    {"item_name": "ELECTRODO PARA TIG TUNGSTENO DE 3/32 (CERIO 2%) GRIS",
     "sku": "6030005", "rate": 170, "unit": "UND", "stock_on_hand": 433},
    {"item_name": "CARETA BASICA", "sku": "CB1", "rate": 550, "unit": "UND",
     "stock_on_hand": 10},
]
_orig_fetch, _orig_match = main.fetch_zoho_catalog, main.match_product_to_catalog
main.fetch_zoho_catalog = lambda: _fake_catalog
main.match_product_to_catalog = lambda q, c, requested_unit="": None  # Haiku dice NINGUNO
try:
    ctx = main.zoho_inventory_context("que vale el tungsteno de 1/8 y de 3/32")
    check("matcher=NINGUNO pero el contexto trae los candidatos con precio",
          "TUNGSTENO DE 1/8" in ctx and "TUNGSTENO DE 3/32" in ctx)
    check("  con precio ISV calculado (280→322, 170→195.50)",
          "322" in ctx and "195.50" in ctx)
    check("  y NO dice 'No se encontró' (el cerebro tiene datos)",
          "No se encontró" not in ctx)
    # con match único, la tabla igual acompaña
    main.match_product_to_catalog = lambda q, c, requested_unit="": _fake_catalog[0]
    ctx2 = main.zoho_inventory_context("tungsteno 1/8")
    check("con match único: dato real + tabla de candidatos",
          "Producto encontrado" in ctx2 and "CANDIDATOS DEL CATÁLOGO" in ctx2)
finally:
    main.fetch_zoho_catalog, main.match_product_to_catalog = _orig_fetch, _orig_match


# ─── 9. Fracciones habladas (v30.8, notas de voz) ──────────────────────────
section("9. Fracciones habladas (v30.8)")

check("'tungsteno de un octavo' → 1/8",
      "1/8" in main._normalize_query_for_search("tungsteno de un octavo"))
check("'la variedad tungsteno de un 16' → 1/16",
      "1/16" in main._normalize_query_for_search("la variedad tungsteno de un 16"))
check("'tres treinta y dos' → 3/32",
      "3/32" in main._normalize_query_for_search("tungsteno de tres treinta y dos"))
check("'cinco treinta y dos' → 5/32",
      "5/32" in main._normalize_query_for_search("electrodo 7018 de cinco treinta y dos"))
check("'tres octavos' → 3/8 (no 1/8)",
      "3/8" in main._normalize_query_for_search("varilla de tres octavos")
      and "1/8" not in main._normalize_query_for_search("varilla de tres octavos"))
check("'un dieciseisavo' → 1/16",
      "1/16" in main._normalize_query_for_search("er4043 de un dieciseisavo"))
# Guard: '16' suelto (cantidad) NO se convierte
check("'quiero 16 libras' NO se vuelve fracción",
      "1/16" not in main._normalize_query_for_search("quiero 16 libras de 6011"))

# El caso completo del 02-nov: prefilter con fake catalog de tungstenos
_tung_cat = [
    {"item_name": "ELECTRODO PARA TIG TUNGSTENO DE 1/8 (CERIO 2%) GRIS", "sku": "a", "rate": 280, "unit": "UND", "stock_on_hand": 544},
    {"item_name": "ELECTRODO PARA TIG TUNGSTENO DE 3/32 (CERIO 2%) GRIS", "sku": "b", "rate": 170, "unit": "UND", "stock_on_hand": 433},
    {"item_name": "ELECTRODO PARA TIG TUNGSTENO DE 1/16 (TORIO 2%) ROJO", "sku": "c", "rate": 75, "unit": "UND", "stock_on_hand": 424},
]
_q = main._normalize_query_for_search(
    "que vale la variedad tungsteno de un octavo, la variedad tungsteno de 3.32 y la variedad tungsteno de un 16")
_top = main._prefilter_catalog(_q, _tung_cat, top_n=3)
check("transcript real 10:33 → los TRES diámetros en el prefilter",
      len(_top) == 3)


# ─── Resumen final ─────────────────────────────────────────────────────────
print()
total = len(results)
failed = sum(1 for _, ok, _ in results if not ok)
passed = total - failed
print(f"\n{'=' * 70}")
print(f"RESUMEN: {passed}/{total} OK" + (f", {failed} fallaron" if failed else " 🎉"))
print(f"{'=' * 70}")

if failed:
    print("\nFallos:")
    for label, ok, detail in results:
        if not ok:
            print(f"  {FAIL} {label}" + (f"  ({detail})" if detail else ""))
    sys.exit(1)
sys.exit(0)
