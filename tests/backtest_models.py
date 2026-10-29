"""Backtest de modelos: reproduce chats reales contra /debug/chat y compara cerebros.

Reproduce cada chat real (los turnos del CLIENTE) contra el bot desplegado,
una vez por modelo, y guarda las respuestas del bot + los line_items de las
cotizaciones dry-run. Después, el modo `judge` califica cada respuesta contra
lo que Daniel contestó de verdad, usando un modelo juez.

NUNCA manda WhatsApp real ni crea cotizaciones en Zoho: el endpoint
/debug/chat corre en test mode (v30.2) con dry-run de Zoho.

Uso:

  # 1. Correr la suite (una vez por modelo; con resume automático):
  DEBUG_TOKEN=xxx python tests/backtest_models.py run \
      --chats tests/chats_reales.json \
      --models claude-opus-4-6 claude-opus-5-5 claude-fable-5 \
      [--base-url https://sumin-wa-bot.onrender.com] [--limit N]

  # 2. Calificar contra las respuestas reales de Daniel:
  ANTHROPIC_API_KEY=xxx python tests/backtest_models.py judge \
      [--results tests/results] [--judge-model claude-sonnet-4-6]

Formato de tests/chats_reales.json:
  [{"id": "chat-001",
    "turns": [{"role": "customer", "text": "..."},
              {"role": "daniel",   "text": "..."}, ...]}, ...]
Turnos "daniel" consecutivos después de un turno "customer" son la referencia
de ese turno. Turnos "customer" consecutivos se mandan uno por uno.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

DEFAULT_BASE_URL = "https://sumin-wa-bot.onrender.com"
RESULTS_DIR = Path(__file__).parent / "results"


# ─── run ─────────────────────────────────────────────────────────────────────

def _model_slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")


def load_chats(path: Path) -> list[dict]:
    chats = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(chats, list):
        raise SystemExit("El archivo de chats debe ser una lista JSON")
    return chats


def group_turns(turns: list[dict]) -> list[dict]:
    """Agrupa: cada turno customer + la(s) respuesta(s) reales de Daniel que siguen."""
    grouped = []
    for t in turns:
        role = (t.get("role") or "").lower()
        if role == "customer":
            grouped.append({"customer": t.get("text", ""), "reference": []})
        elif grouped:
            grouped[-1]["reference"].append(t.get("text", ""))
    return grouped


def run_chat(client: httpx.Client, base_url: str, token: str, chat: dict,
             model: str) -> dict:
    slug = _model_slug(model)
    phone = f"bt-{slug[:20]}-{chat['id']}"
    out_turns = []
    grouped = group_turns(chat.get("turns", []))
    first = True
    for g in grouped:
        t0 = time.time()
        r = client.post(
            f"{base_url}/debug/chat",
            headers={"X-Debug-Token": token},
            json={"phone": phone, "text": g["customer"],
                  "reset_state": first, "model": model},
            timeout=240,
        )
        first = False
        r.raise_for_status()
        data = r.json()
        out_turns.append({
            "input": g["customer"],
            "reference": g["reference"],
            "replies": [m.get("text", "") for m in data.get("replies", [])],
            "estimates": data.get("estimates", []),
            "latency_s": round(time.time() - t0, 1),
        })
    return {"id": chat["id"], "model": model, "turns": out_turns}


def cmd_run(args: argparse.Namespace) -> None:
    token = os.environ.get("DEBUG_TOKEN")
    if not token:
        raise SystemExit("Falta DEBUG_TOKEN en el entorno")
    chats = load_chats(Path(args.chats))
    if args.limit:
        chats = chats[: args.limit]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    with httpx.Client() as client:
        for model in args.models:
            out_path = RESULTS_DIR / f"{_model_slug(model)}.json"
            done: dict[str, dict] = {}
            if out_path.exists():  # resume
                done = {c["id"]: c for c in json.loads(out_path.read_text())}
            print(f"\n=== {model} ({len(done)}/{len(chats)} ya corridos) ===")
            for i, chat in enumerate(chats, 1):
                if chat["id"] in done:
                    continue
                try:
                    result = run_chat(client, args.base_url, token, chat, model)
                except Exception as exc:
                    print(f"  ❌ {chat['id']}: {exc}")
                    result = {"id": chat["id"], "model": model, "error": str(exc),
                              "turns": []}
                done[chat["id"]] = result
                out_path.write_text(json.dumps(list(done.values()),
                                               ensure_ascii=False, indent=1))
                n_turns = len(result.get("turns", []))
                print(f"  [{i}/{len(chats)}] {chat['id']}: {n_turns} turnos OK")
    print(f"\nResultados en {RESULTS_DIR}/")


# ─── judge ───────────────────────────────────────────────────────────────────

JUDGE_SYSTEM = """Sos un auditor de calidad de un bot de ventas de soldadura \
(SUMIN, Honduras). Te doy: el mensaje del CLIENTE, lo que DANIEL (el dueño, \
estándar de oro) contestó de verdad, y lo que contestó el BOT.

Calificá al BOT (no a Daniel) en JSON exacto:
{"producto": 0-2, "precio": 0-2, "estilo": 0-2, "critico": true/false, "nota": "<máx 15 palabras>"}

- producto: ¿identificó/cotizó el producto y presentación correctos (aleación, \
diámetro, peso del rollo, fracción)? 2=igual que Daniel, 1=parcial/pregunta \
razonable, 0=producto o presentación equivocada.
- precio: 2=precio consistente con Daniel o no aplica, 1=duda menor, 0=precio errado.
- estilo: mensajes cortos, 1 pregunta por turno, sin cierres de bot. 2=como \
Daniel, 0=robótico/largo.
- critico: true si un cliente real recibiría información FALSA (precio o \
producto equivocado) o el bot crashea ([ERROR]).
Si el turno no da para evaluar un criterio, usá 2 (no castigar)."""


def judge_turn(anthropic_client, judge_model: str, turn: dict) -> dict | None:
    if not turn.get("reference"):
        return None  # sin respuesta real de Daniel no hay contra qué comparar
    bot = "\n---\n".join(turn.get("replies") or ["(sin respuesta)"])
    est = ""
    if turn.get("estimates"):
        lis = [f"{li.get('quantity')} x {li.get('name')} @ L{li.get('rate')}"
               for e in turn["estimates"] for li in e.get("line_items", [])]
        est = "\nLINE ITEMS COTIZADOS POR EL BOT:\n" + "\n".join(lis)
    prompt = (f"CLIENTE:\n{turn['input']}\n\n"
              f"DANIEL (real):\n" + "\n".join(turn["reference"]) +
              f"\n\nBOT:\n{bot}{est}")
    msg = anthropic_client.messages.create(
        model=judge_model, max_tokens=200, system=JUDGE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text
    m = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        return json.loads(m.group(0)) if m else None
    except Exception:
        return None


def cmd_judge(args: argparse.Namespace) -> None:
    import anthropic
    client = anthropic.Anthropic()  # usa ANTHROPIC_API_KEY del entorno
    results_dir = Path(args.results)
    files = sorted(results_dir.glob("*.json"))
    files = [f for f in files if not f.name.startswith("judged-")]
    if not files:
        raise SystemExit(f"No hay resultados en {results_dir}")

    summary = {}
    for f in files:
        chats = json.loads(f.read_text())
        model = chats[0].get("model", f.stem) if chats else f.stem
        scores, criticals, errors, n = [], 0, 0, 0
        judged = []
        for chat in chats:
            if chat.get("error"):
                errors += 1
            for turn in chat.get("turns", []):
                if any("[ERROR]" in r for r in turn.get("replies", [])):
                    errors += 1
                verdict = judge_turn(client, args.judge_model, turn)
                if verdict:
                    n += 1
                    scores.append(verdict)
                    criticals += 1 if verdict.get("critico") else 0
                    judged.append({"chat": chat["id"], "input": turn["input"][:60],
                                   **verdict})
        avg = lambda k: round(sum(s.get(k, 0) for s in scores) / max(n, 1), 2)
        summary[model] = {"turnos_evaluados": n, "producto": avg("producto"),
                          "precio": avg("precio"), "estilo": avg("estilo"),
                          "criticos": criticals, "errores": errors}
        (results_dir / f"judged-{f.stem}.json").write_text(
            json.dumps(judged, ensure_ascii=False, indent=1))
        print(f"{model}: {summary[model]}")

    # Reporte comparativo
    lines = ["# Backtest de modelos — reporte", "",
             "| Modelo | Turnos | Producto /2 | Precio /2 | Estilo /2 | Críticos | Errores |",
             "|---|---|---|---|---|---|---|"]
    for model, s in summary.items():
        lines.append(f"| {model} | {s['turnos_evaluados']} | {s['producto']} | "
                     f"{s['precio']} | {s['estilo']} | {s['criticos']} | {s['errores']} |")
    report = results_dir / "reporte.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nReporte: {report}")


# ─── main ────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="Reproducir chats contra /debug/chat")
    pr.add_argument("--chats", required=True)
    pr.add_argument("--models", nargs="+", required=True)
    pr.add_argument("--base-url", default=DEFAULT_BASE_URL)
    pr.add_argument("--limit", type=int, default=0)
    pr.set_defaults(func=cmd_run)

    pj = sub.add_parser("judge", help="Calificar resultados vs respuestas reales")
    pj.add_argument("--results", default=str(RESULTS_DIR))
    pj.add_argument("--judge-model", default="claude-sonnet-4-6")
    pj.set_defaults(func=cmd_judge)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
