"""
Controla un PePump que ya está corriendo con --web, desde otra consola:

    python ctl.py list                 # posiciones y su estado
    python ctl.py close 2              # cerrar (vender) la #2
    python ctl.py close <MINT>         # ... o por mint
    python ctl.py close-all            # cerrar todas

Habla con la misma API que la página web (--url, default
http://127.0.0.1:8080). Si el panel tiene contraseña, la toma de la
variable de entorno PEPUMP_WEB_PASSWORD o la pide por teclado.
"""
import argparse
import getpass
import os
import sys

import requests

# Mismas cabeceras que manda la página (ver pepump/web/server.py).
HEADERS = {"X-PePump": "1"}


def parse_args():
    p = argparse.ArgumentParser(description="Controla un PePump que está corriendo con --web")
    p.add_argument("--url", default="http://127.0.0.1:8080", help="Dirección del panel web")
    p.add_argument("-y", "--yes", action="store_true", help="No pedir confirmación")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="Lista las posiciones")
    close = sub.add_parser("close", help="Cierra (vende) una posición")
    close.add_argument("ref", help="Número de la posición (#) o su mint")
    sub.add_parser("close-all", help="Cierra todas las posiciones activas")
    return p.parse_args()


def fail(msg: str) -> None:
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def connect(url: str) -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        info = s.get(f"{url}/api/session", timeout=5).json()
    except requests.RequestException as e:
        fail(f"no se pudo conectar con {url} ({e}). ¿Está corriendo run.py con --web?")
    if info["auth_required"]:
        password = os.environ.get("PEPUMP_WEB_PASSWORD") or getpass.getpass("Contraseña del panel: ")
        r = s.post(f"{url}/api/login", json={"password": password}, timeout=5)
        if r.status_code != 200:
            fail(r.json().get("detail", f"login rechazado ({r.status_code})"))
    return s


def fmt_price(p):
    return "—" if p is None else f"{p:.10f}"


def fmt_pct(x):
    return "—" if x is None else f"{x:+.2f}%"


def label(snap: dict) -> str:
    return f"${snap['token_symbol']}" if snap.get("token_symbol") else f"{snap['mint'][:4]}…{snap['mint'][-4:]}"


def describe(snap: dict) -> str:
    pos = snap.get("position")
    if snap["phase"] == "open" and not snap["running"]:
        estado = "⚠ ABIERTA SIN VIGILAR"
    elif snap["close_requested"] and snap["running"]:
        estado = "cerrando"
    elif pos and snap["phase"] == "open":
        estado = "armada" if pos["armed"] else "esperando activación"
    else:
        estado = {"waiting_entry": "esperando entrada", "closed": "cerrada",
                  "stopped": "terminada"}.get(snap["phase"], snap["phase"])
    partes = [f"#{snap['id']:<3} {label(snap):<12} {estado:<22}"]
    if pos and pos["closed"]:
        partes.append(f"PnL {fmt_pct(pos['realized_pnl_pct'])}")
    elif pos:
        partes.append(f"precio {fmt_price(snap['latest_price'])}  mercado {fmt_pct(pos['market_pnl_pct'])}")
    return "  ".join(partes)


def confirm(question: str, yes: bool) -> bool:
    if yes:
        return True
    return input(f"{question} [s/N] ").strip().lower() in ("s", "si", "sí", "y", "yes")


def main():
    args = parse_args()
    url = args.url.rstrip("/")
    s = connect(url)
    state = s.get(f"{url}/api/state", timeout=5).json()
    snaps = state["positions"]
    modo = "REAL" if state["live"] else "SIMULADO"

    if args.cmd == "list":
        print(f"PePump · modo {modo} · {len(snaps)} posiciones")
        for snap in snaps:
            print("  " + describe(snap))
        return

    if args.cmd == "close":
        ref = args.ref.strip().lstrip("#")
        matches = [x for x in snaps if str(x["id"]) == ref or x["mint"] == ref]
        if not matches:
            fail(f"no hay ninguna posición {args.ref!r}")
        snap = matches[-1]
        print(describe(snap))
        accion = "vender" if snap.get("position") else "cancelar la entrada de"
        if not confirm(f"¿{accion.capitalize()} #{snap['id']}{' (MODO REAL)' if state['live'] else ''}?", args.yes):
            return
        r = s.post(f"{url}/api/positions/{snap['id']}/close", timeout=5)
        if r.status_code != 200:
            fail(r.json().get("detail", str(r.status_code)))
        print(f"Cierre pedido para #{snap['id']}.")
        return

    if args.cmd == "close-all":
        activas = [x for x in snaps if x["running"] and not x["close_requested"]]
        if not activas:
            print("No hay posiciones activas.")
            return
        for snap in activas:
            print(describe(snap))
        if not confirm(f"¿Cerrar estas {len(activas)}{' (MODO REAL)' if state['live'] else ''}?", args.yes):
            return
        r = s.post(f"{url}/api/positions/close-all", timeout=5)
        if r.status_code != 200:
            fail(r.json().get("detail", str(r.status_code)))
        print(f"Cierre pedido para {r.json()['requested']} posiciones.")


if __name__ == "__main__":
    main()
