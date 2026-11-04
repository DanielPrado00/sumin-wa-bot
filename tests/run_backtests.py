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


# ─── 10. Cost agent — solo Daniel founder (v30.9) ──────────────────────────
section("10. Cost agent (v30.9)")

_state_c = {"conversations": {}, "_meta": {}}
main._TEST_STATE.capture = []

# Gate de número: otro número con la palabra 'costo' NO dispara (ni toca red)
check("otro número trusted (3334-0477) NO accede a costos",
      main.cost_agent("50433340477", "cual es el costo del e71t-gs 10 lbs", _state_c) is False)
check("cliente random NO accede a costos",
      main.cost_agent("50412345678", "costo del 6011", _state_c) is False)
check("Daniel SIN palabra de costo NO dispara (sigue flujo normal)",
      main.cost_agent("50497041381", "que vale el tungsteno 1/8", _state_c) is False)
check("keywords: 'a cuánto nos llegó' dispara el regex",
      bool(main._COST_KEYWORD_RE.search("a cuánto nos llegó el alambre")))
check("keywords: 'valoración' dispara",
      bool(main._COST_KEYWORD_RE.search("según la valoración de inventario")))

# Flujo completo con red mockeada
_orig = (main.fetch_zoho_catalog, main.match_product_to_catalog, main._item_purchase_history)
main.fetch_zoho_catalog = lambda: [{"item_id": "X1", "item_name": 'MICROALAMBRE E71T-GS 0.030" 10 LBS', "rate": 1045.44, "unit": "ROLLO", "stock_on_hand": 797}]
main.match_product_to_catalog = lambda q, c, requested_unit="": c[0]
main._item_purchase_history = lambda *a, **k: [
    {"date": "2026-09-14", "vendor": "Inwelt", "qty": 500, "rate": 27.8, "currency": "USD", "bill_number": "BILL-0012"},
    {"date": "2026-06-02", "vendor": "Inwelt", "qty": 300, "rate": 26.5, "currency": "USD", "bill_number": "BILL-0007"},
]
_orig_cost_summary = main._item_cost_summary
main._item_cost_summary = lambda item_id: (745.82, -43.0, -32070.34)
_orig_extract = main.extract_items_for_quote
main.extract_items_for_quote = lambda t, h: ([], "")  # fallback → query entera
try:
    ok = main.cost_agent("50497041381", "cual es el costo del alambre e71t-gs de 10 libras", _state_c)
    _msg = main._TEST_STATE.capture[-1]["text"] if main._TEST_STATE.capture else ""
    check("Daniel + 'costo' → responde", ok is True)
    check("  con las entradas del proveedor y moneda",
          "2026-09-14" in _msg and "27.80 dólares" in _msg and "BILL-0012" in _msg)
    check("  marcado como interno", "interno" in _msg)
    check("  con precio de venta y stock", "1,045.44" in _msg and "797" in _msg)
    check("  con costo promedio en libros (WAC) y alerta de stock negativo",
          "745.82 lempiras por unidad" in _msg and "stock negativo" in _msg)

    # v30.10: seguimiento corto en modo costo ('y el de 0.9 mm?')
    main._TEST_STATE.capture = []
    ok2 = main.cost_agent("50497041381", "y el de 0.9 mm?", _state_c)
    check("seguimiento 'y el de 0.9 mm?' sigue en modo costo", ok2 is True)
    _meta_c = main.get_conv_meta(_state_c, "50497041381")
    check("  query combinada con el producto anterior",
          "e71t-gs" in _meta_c.get("last_cost_query", "").lower()
          and "0.9" in _meta_c.get("last_cost_query", ""))

    # v30.10: multi-item ('la antorcha X y el maneral Y')
    _cat2 = [
        {"item_id": "A", "item_name": "ANTORCHA DE OXICORTE SAFECUT CA-460", "rate": 2800, "unit": "UND", "stock_on_hand": 5},
        {"item_id": "B", "item_name": "MANERAL DE OXICORTE SAFECUT WH450", "rate": 3200, "unit": "UND", "stock_on_hand": 3},
    ]
    main.fetch_zoho_catalog = lambda: _cat2
    main.extract_items_for_quote = lambda t, h: (
        [{"product": "antorcha oxicorte CA-460", "quantity": 1, "unit": ""},
         {"product": "maneral oxicorte safecut", "quantity": 1, "unit": ""}], "")
    main.match_product_to_catalog = lambda q, c, requested_unit="": (
        _cat2[0] if "460" in q else _cat2[1])
    main._item_purchase_history = lambda *a, **k: []
    main._item_po_history = lambda *a, **k: [
        {"date": "2026-05-12", "vendor": "SPEED PAN", "qty": 200, "rate": 14.2, "currency": "USD", "po_number": "OC-0050", "status": "facturada"}]
    main._TEST_STATE.capture = []
    ok3 = main.cost_agent("50497041381", "cual es el costo de la antorcha CA-460 y el maneral safecut", _state_c)
    _msg3 = main._TEST_STATE.capture[-1]["text"] if main._TEST_STATE.capture else ""
    check("multi-item: responde los DOS productos en un mensaje",
          ok3 is True and "CA-460" in _msg3 and "WH450" in _msg3)
finally:
    main.fetch_zoho_catalog, main.match_product_to_catalog, main._item_purchase_history = _orig
    main.extract_items_for_quote = _orig_extract
    main._item_cost_summary = _orig_cost_summary
    main._item_po_history = lambda *a, **k: []
    main._TEST_STATE.capture = None


# ─── 11. Tabla de costos por proveedor (v30.14) ─────────────────────────────
section("11. Tabla por proveedor (v30.14)")

_orig_vendors = main._fetch_vendors
main._fetch_vendors = lambda: [
    {"contact_id": "V1", "contact_name": "HANKOOK WELDTEK CO. LTD", "company_name": "HANKOOK WELDTEK CO. LTD"},
    {"contact_id": "V2", "contact_name": "NINGBO KIMPIN INDUSTRIAL PTE LTD", "company_name": ""},
]
try:
    vid, vname = main._match_vendor_in_text("costos de los microalambres que vienen de hankook")
    check("detecta HANKOOK en la frase", vid == "V1" and "HANKOOK" in vname)
    vid2, _ = main._match_vendor_in_text("cual es el costo del microalambre e71t-gs 10 lbs")
    check("sin proveedor mencionado → no dispara tabla", vid2 is None)
    vid3, _ = main._match_vendor_in_text("costos de kimpin")
    check("detecta KIMPIN", vid3 == "V2")
    # v30.16: proveedor de palabras cortas → match por frase completa
    main._fetch_vendors = lambda: [
        {"contact_id": "V3", "contact_name": "LIN AN DA YANG", "company_name": ""}]
    vid4, _ = main._match_vendor_in_text("costos de los microalambres lin an da yang")
    check("v30.16: detecta LIN AN DA YANG (frase completa, palabras cortas)",
          vid4 == "V3")
    vid5, _ = main._match_vendor_in_text("costo del microalambre e71t-gs 33 lbs")
    check("v30.16: sin mención de Lin An Da Yang → no matchea", vid5 is None)
finally:
    main._fetch_vendors = _orig_vendors


# ─── 12. Costo liquidado por factura (v30.15, caso HWT250512SU-1) ───────────
section("12. Costo liquidado (v30.15)")

# Caso real validado por Daniel: 160 rollos a 14.34 USD, subtotal 13,283.40,
# cargos asociados 1,605.26 USD, cambio ~25.68 → liquidado ≈ 412.5 L
_det = {
    "exchange_rate": 25.68, "sub_total": 13283.40, "currency_code": "USD",
    "landed_costs": [{"amount": 1104.99}, {"amount": 5.09},
                     {"amount": 136.54}, {"amount": 358.64}],
}
_li = {"rate": 14.34, "quantity": 160}
_lcy, _landed = main._bill_landed_unit(_det, _li)
check("al cambio sin cargos ≈ 368.25 L", _lcy is not None and abs(_lcy - 14.34 * 25.68) < 0.5)
check("liquidado ≈ 412.5 L (±3) — reproduce la valoración de Zoho",
      _landed is not None and abs(_landed - 412.54) < 3.0,
      f"got {_landed}")
# Sin cargos ni cambio → None
_lcy2, _landed2 = main._bill_landed_unit({"sub_total": 100}, {"rate": 10, "quantity": 5})
check("sin exchange_rate/cargos → sin estimación (no inventa)",
      _lcy2 is None and _landed2 is None)


# ─── 13. Agente interno (v31.0) ─────────────────────────────────────────────
section("13. Agente interno (v31.0)")

check("números del agente: ambos de Daniel, nadie más",
      main.INTERNAL_AGENT_NUMBERS == {"50497041381", "50433340477"})
_t_founder = {t["name"] for t in main._agent_tools_for("50497041381")}
_t_second  = {t["name"] for t in main._agent_tools_for("50433340477")}
check("founder tiene las 6 tools (incluye costos y ventas)",
      _t_founder == {"buscar_items", "costo_item", "tabla_costos_proveedor",
                     "calcular_flete_expreco", "listar_clientes", "ventas_item"})
check("segundo número SIN costos/ventas (internas del founder)",
      _t_second == {"buscar_items", "calcular_flete_expreco", "listar_clientes"})
check("dispatcher bloquea tool interna desde el segundo número",
      "exclusiva" in main._dispatch_agent_tool("costo_item", {"producto": "x"},
                                               "50433340477").get("error", ""))
check("dispatcher: tool desconocida → error controlado",
      "desconocida" in main._dispatch_agent_tool("nada", {}, "50497041381").get("error", ""))

# buscar_items con catálogo stub (sin red)
_orig_fetch2 = main.fetch_zoho_catalog
main.fetch_zoho_catalog = lambda: [
    {"item_id": "1", "item_name": "ELECTRODO 6011 1/8 A.A.", "sku": "E1",
     "unit": "LB", "rate": 45.0, "stock_on_hand": 100, "vendor_name": "INWELCO"}]
try:
    _r = main._tool_buscar_items({"query": "electrodo 6011 1/8"})
    check("buscar_items: nombre, precio con ISV y stock",
          _r["items"] and _r["items"][0]["precio_con_isv"] == 51.75
          and _r["items"][0]["stock"] == 100)
finally:
    main.fetch_zoho_catalog = _orig_fetch2

# flete tool reutiliza el tarifario real
_rf = main._tool_flete({"destino": "marcala", "peso_lbs": 8})
check("tool de flete: Marcala 8 lbs = 194.85 L",
      abs((_rf.get("total_L_antes_isv") or 0) - 194.85) < 0.01)


# ─── 14. Modo costos vs agente + diámetros (v31.2) ──────────────────────────
section("14. Cost followup vs ventas + variantes de diámetro (v31.2)")

# Bug real 02-nov 18:09: 'cuánto 8018 ha llevado azunosa este año?' (41 chars,
# con dígitos) caía como followup de costos y repetía la respuesta anterior.
check("followup: pregunta de ventas NO es followup de costo",
      not main._is_cost_followup("cuánto 8018 ha llevado azunosa este año?"))
check("followup: 'y el de 0.9 mm?' sí es followup",
      main._is_cost_followup("y el de 0.9 mm?"))
check("followup: 'el 0.045' sí es followup",
      main._is_cost_followup("el 0.045"))
check("followup: pregunta de flete NO es followup",
      not main._is_cost_followup("flete a morazán de 10 libras"))
check("followup: 'cotizame 5 libras' NO es followup",
      not main._is_cost_followup("cotizame 5 libras"))
check("followup: 'y la última compra del 0.030?' sí es followup",
      main._is_cost_followup("y la última compra del 0.030?"))

_cat_diam = [
    {"item_id": "a", "item_name": 'MICROALAMBRE ER70S-6 0.035" 11 LBS'},
    {"item_id": "b", "item_name": 'MICROALAMBRE ER70S-6 0.030" 11 LBS'},
    {"item_id": "c", "item_name": 'MICROALAMBRE ER70S-6 0.045" 33 LBS'},
    {"item_id": "d", "item_name": "ELECTRODO 6011 1/8 A.A."},
    {"item_id": "e", "item_name": "ELECTRODO 6011 3/32 A.A."},
]
_v = main._cost_diameter_variants("microalambre er70s-6 de 11 libras",
                                  _cat_diam[0], _cat_diam)
check("variantes: sin diámetro en consulta → los 2 de 11 LBS",
      len(_v) == 2 and {i["item_id"] for i in _v} == {"a", "b"})
check("variantes: el matcheado va primero", _v[0]["item_id"] == "a")
_v2 = main._cost_diameter_variants("er70s-6 0.035 de 11 libras",
                                   _cat_diam[0], _cat_diam)
check("variantes: con diámetro en consulta → solo el matcheado",
      len(_v2) == 1 and _v2[0]["item_id"] == "a")
_v3 = main._cost_diameter_variants("electrodo 6011 aa", _cat_diam[3], _cat_diam)
check("variantes: 6011 sin fracción → 1/8 y 3/32",
      len(_v3) == 2 and {i["item_id"] for i in _v3} == {"d", "e"})
_v4 = main._cost_diameter_variants("6011 de 1/8", _cat_diam[3], _cat_diam)
check("variantes: 6011 con fracción → solo uno", len(_v4) == 1)


# ─── 15. Cotización por ROLLO para microalambres (v31.3) ────────────────────
section("15. Línea en ROLLO para microalambres (v31.3)")

_orig_uom = main._item_unit_conversions
main._item_unit_conversions = lambda item_id: {
    "mw33": [{"target_unit": "ROLLO", "conversion_rate": 33,
              "unit_conversion_id": "UC33"}],
    "mw2":  [{"target_unit": "ROLLO", "conversion_rate": 2,
              "unit_conversion_id": "UC2"}],
    "mw_nc": [],
}.get(item_id, [])
try:
    _lis = main._rollify_line_items([
        {"item_id": "mw33", "name": 'MICROALAMBRE COBRIZ 0.045" ER70S-6 33 LBS.',
         "quantity": 66, "rate": 27.88, "unit": "LB"},
        {"item_id": "mw33", "name": 'MICROALAMBRE COBRIZ 0.045" ER70S-6 33 LBS.',
         "quantity": 40, "rate": 27.88, "unit": "LB"},
        {"item_id": "e1", "name": "ELECTRODO 6011 1/8 A.A.",
         "quantity": 33, "rate": 48.0, "unit": "LB"},
        {"item_id": "mw2", "name": 'MICROALAMBRE INOX 0.035" 2 LBS',
         "quantity": 16, "rate": 100.0, "unit": "LB"},
        {"item_id": "mw_nc", "name": "MICROALAMBRE X 33 LBS",
         "quantity": 33, "rate": 10.0, "unit": "LB"},
    ])
    check("rollify: 66 LB del rollo de 33 → 2 ROLLO @ 920.04",
          _lis[0]["quantity"] == 2 and _lis[0]["unit"] == "ROLLO"
          and abs(_lis[0]["rate"] - 920.04) < 0.01
          and _lis[0]["unit_conversion_id"] == "UC33")
    check("rollify: total se conserva (2×920.04 = 66×27.88)",
          abs(_lis[0]["quantity"] * _lis[0]["rate"] - 66 * 27.88) < 0.01)
    check("rollify: 40 LB no es múltiplo de 33 → queda en LB",
          _lis[1]["unit"] == "LB" and _lis[1]["quantity"] == 40)
    check("rollify: electrodo nunca se convierte",
          _lis[2]["unit"] == "LB" and _lis[2]["quantity"] == 33)
    check("rollify: 16 LB de rollos de 2 → 8 ROLLO @ 200",
          _lis[3]["quantity"] == 8 and abs(_lis[3]["rate"] - 200.0) < 0.01)
    check("rollify: sin conversión ROLLO en Zoho → queda en LB",
          _lis[4]["unit"] == "LB")
finally:
    main._item_unit_conversions = _orig_uom


# ─── 16. Contexto de cotización: unidad pendiente + reset (v31.4) ───────────
section("16. Pregunta de unidad + cotización nueva (v31.4)")

from datetime import datetime as _dt, timedelta as _td

# Caso real 04-nov: '4 cajas de 6011 a nombre de IMBA' → pregunta de unidad →
# 'si 40 libras seran en total' debe COMBINARSE con el pedido original.
_puq = {"request_text": "cotizacion 4 cajas de 6011 a nombre de industrias IMBA",
        "asked_at": _dt.now().isoformat()}
_merged = main._puq_merge_text(_puq, "si 40 libras seran en total")
check("puq: respuesta corta se combina con el pedido original",
      _merged == "cotizacion 4 cajas de 6011 a nombre de industrias IMBA "
                 "(si 40 libras seran en total)")
check("puq: pregunta vieja (>30 min) no se combina",
      main._puq_merge_text({"request_text": "x 6011",
                            "asked_at": (_dt.now() - _td(minutes=45)).isoformat()},
                           "40 libras") is None)
check("puq: una cotización nueva no se combina",
      main._puq_merge_text(_puq, "mejor cotizame 5 cajas de 7018") is None)

check("reset: 'cotización nueva de 4 cajas' dispara y deja trigger",
      main._NEW_QUOTE_RESET_RE.search("cotización nueva de 4 cajas de 6011")
      and main._NEW_QUOTE_RESET_RE.sub("cotización",
          "cotización nueva de 4 cajas de 6011", count=1)
          == "cotización de 4 cajas de 6011")
check("reset: 'nueva cotizacion' sin tilde también",
      bool(main._NEW_QUOTE_RESET_RE.search("nueva cotizacion para imba")))
check("reset: una cotización normal NO dispara",
      not main._NEW_QUOTE_RESET_RE.search("cotizacion 4 cajas de 6011"))

# Nombre explícito en el mensaje actual (regex del path trusted)
import re
_mn = re.search(r"a\s+nombre\s+de\s+(?!qui[eé]n)([^\n,;?(]{3,60})",
                "cotizacion 4 cajas de 6011 a nombre de industrias IMBA "
                "(si 40 libras seran en total)", re.IGNORECASE)
check("nombre explícito: captura 'industrias IMBA' y corta en el paréntesis",
      _mn and _mn.group(1).strip(" .") == "industrias IMBA")
check("nombre explícito: 'a nombre de quién' NO captura",
      not re.search(r"a\s+nombre\s+de\s+(?!qui[eé]n)([^\n,;?(]{3,60})",
                    "¿a nombre de quién la hago?", re.IGNORECASE))

# v31.6: respuesta corta a pregunta del agente vuelve al agente
_hist_puq = [{"role": "user", "content": "precio del er70s-6 0.035"},
             {"role": "assistant",
              "content": "*MICROALAMBRE ER70S-6 0.035*... ¿Cuál presentación necesitás?"}]
check("followup: 'de 33 libras' tras '¿cuál presentación?' SÍ es seguimiento",
      main._is_followup_to_bot_question("de 33 libras", _hist_puq))
_hist_libre = [{"role": "assistant",
                "content": "¿Querés que incluya también el de 11 lbs?"}]
check("followup interno: pregunta libre del agente + respuesta corta",
      main._is_followup_to_bot_question("si el de 33", _hist_libre, internal=True))
check("followup interno: 'cotizame 5 cajas de 7018' NO es seguimiento",
      not main._is_followup_to_bot_question("cotizame 5 cajas de 7018",
                                            _hist_libre, internal=True))
check("followup NO interno: pregunta libre no listada no captura",
      not main._is_followup_to_bot_question("si el de 33", _hist_libre))

# v31.5: reglas de marca en el prompt del agente interno
check("agente: regla de marcas (sin W.A. salvo pedido) está en el prompt",
      "W.A." in main._AGENT_SYSTEM and "UN SOLO precio" in main._AGENT_SYSTEM)
check("agente: regla de precios reenviables está en el prompt",
      "REENVIABLES" in main._AGENT_SYSTEM)


# ─── 17. Transcripciones + candado de cantidades + editar EST (v31.7) ───────
section("17. Limpieza de voz, cantidades y editar estimación (v31.7)")

# Casos REALES de los logs del 04-nov
_t1 = main._clean_transcript(
    "Hola, ocupo una cotización nueva de NI99, de 1,8, 44 libras, "
    "150 libras de 6011, 1,8, y 22 libras de 308, 116, de 1,16.")
check("transcript: fracciones con coma y 308,116 → 1/8, 308L-16, 1/16",
      "1/8" in _t1 and "308L-16" in _t1 and "1/16" in _t1 and "1,8" not in _t1)
check("transcript: Crown Car 532 → Chrome Carb 5/32",
      main._clean_transcript("precio por libra del Crown Car 532?")
      == "precio por libra del Chrome Carb 5/32?")
check("transcript: Precio 309.80 → 309 1/8",
      main._clean_transcript("Precio 309.80") == "Precio 309 1/8")
check("transcript: ER70S-6 de 0.35 → 0.035",
      "0.035" in main._clean_transcript("el ER70S-6 de 0.35"))
check("transcript: 0,9 mm y 1,2 mm NO se tocan",
      main._clean_transcript("microalambre de 0,9 mm y de 1,2 mm")
      == "microalambre de 0,9 mm y de 1,2 mm")

_nums = main._explicit_qty_numbers(
    "ocupo 44 libras de NI99 1/8, 150 libras de 6011, dos rollos y son 48")
check("qty: números explícitos detectados (44, 150, 2, 48)",
      {44.0, 150.0, 2.0, 48.0} <= _nums)
check("qty: '1/8' no mete un 8 fantasma", 8.0 not in _nums)
check("qty: texto sin cantidades → vacío",
      main._explicit_qty_numbers("cotizame electrodo 6011 de 1/8") == set())

check("est-ref: 'termina en 7125' matchea",
      bool(main._EST_REF_RE.search("agregale a la que termina en 7125 20 libras")))
check("est-ref: 'estimacion 007119' matchea",
      bool(main._EST_REF_RE.search("a la estimacion 007119 quitale el flete")))
check("est-ref: 'cotizacion 100 libras de 6011' NO matchea",
      not main._EST_REF_RE.search("agregame a la cotizacion 100 libras de 6011"))
check("est-trigger: 'agregale' dispara, 'cuanto vale' no",
      bool(main._EST_EDIT_TRIGGER_RE.search("agregale 20 libras"))
      and not main._EST_EDIT_TRIGGER_RE.search("cuanto vale el 6011"))


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
