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
