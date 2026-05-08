# Backtests del bot

Suite de regresión para los bugs que Daniel ha reportado en producción.
Cada vez que agregamos un fix, agregamos un test acá para que no se rompa de nuevo.

## Correr

```bash
python tests/run_backtests.py
```

Imprime ✅/❌ por caso y un resumen al final. Exit code 1 si algo falla.

## Cobertura actual

- **Heurísticas puras**: confirm vs no-confirm (`tensile`→`si` false-positive),
  mm→fracción, sinónimos (manometro→regulador, vidrio→careta, carbon→arcair),
  detección de aclaración técnica vs nombre de cliente, extracción de hint MIG.
- **MIG handoff**: regex de respuestas numeradas (1-6), parsing de notas extra.
- **MIG consumable detection**: difusor / gas diffuser → handoff; electrodos
  comunes → no handoff.
- **extract_items_for_quote** (con Anthropic mockeado): 4 items se extraen
  los 4 (no se pierde el último), backfill de unidad para electrodos (LB)
  y tungsteno (UND).
- **Decisión LB vs UND**: 6011/7018/INCONEL → libras, tungsteno → unidades.

## Agregar un test

Solo agregar `check(label, expression)` en la sección apropiada. Sin pytest,
sin fixtures — un solo archivo plano.

Para casos que requieran LLM, mockear `main.claude.messages.create`:

```python
main.claude.messages.create = MagicMock(return_value=fake_anthropic_response({
    "items": [{"product": "...", "quantity": 1, "unit": "LB"}],
    "customer_name": "",
}))
```

## Lo que NO está cubierto (todavía)

- Anthropic real (cuesta $ y depende de internet).
- Zoho catalog matching (depende del catálogo en vivo).
- WhatsApp send / receive (depende de Meta).
- End-to-end con webhook simulado.
