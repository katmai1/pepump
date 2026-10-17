"""
Controla un PePump que ya está corriendo con --web, desde otra consola:

    python ctl.py list                 # posiciones y su estado
    python ctl.py open <MINT>          # abrir (comprar) con los ajustes del .toml
    python ctl.py open <MINT> --sol 0.1 --trailing 12
    python ctl.py open <MINT> --real   # con SOL real (o --sim); sin poner, el del .toml
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

# Ajustes de `open` (sin poner, se usan los del .toml del bot).
OPEN_FLAGS = (
    ("--sol", "buy_sol", "SOL a invertir"),
    ("--activation", "activation_pct", "Activación del trailing (+%%)"),
    ("--trailing", "trailing_pct", "Trailing (%% desde el máximo)"),
    ("--stop", "initial_stop_pct", "Stop-loss inicial (-%%)"),
    ("--dip", "entry_dip_pct", "Baja de entrada (-%%)"),
    ("--slippage", "slippage", "Slippage (%%)"),
    ("--priority-fee", "priority_fee", "Priority fee (SOL)"),
)
MARKETS = {
    "bondingcurve": "en la curva de pump.fun",
    "pumpswap": "migrado a PumpSwap",
    "other": "no está en pump.fun ni en PumpSwap (se intentará Raydium CPMM / Meteora DLMM)",
    "unknown": "no se pudo comprobar dónde cotiza",
}


def parse_args():
    p = argparse.ArgumentParser(description="Controla un PePump que está corriendo con --web")
    p.add_argument("--url", default="http://127.0.0.1:8080", help="Dirección del panel web")
    p.add_argument("-y", "--yes", action="store_true", help="No pedir confirmación")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="Lista las posiciones")
    op = sub.add_parser("open", help="Abre (compra) una posición")
    op.add_argument("mint", help="Mint del token")
    for flag, campo, ayuda in OPEN_FLAGS:
        op.add_argument(flag, dest=campo, type=float, help=ayuda)
    op.add_argument("--pool", help="Pool para la Lightning API (auto, pump, pump-amm, ...)")
    modo = op.add_mutually_exclusive_group()
    modo.add_argument("--real", dest="live", action="store_true", default=None,
                      help="Compra con SOL REAL")
    modo.add_argument("--sim", dest="live", action="store_false", help="Posición simulada")
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
    if snap["phase"] == "open" and not snap["running"] and snap.get("detached"):
        estado = "⏸ detenida sin vender"
    elif snap["phase"] == "open" and not snap["running"]:
        estado = "⚠ ABIERTA SIN VIGILAR"
    elif snap["close_requested"] and snap["running"]:
        estado = "cerrando"
    elif pos and snap["phase"] == "open":
        estado = "armada" if pos["armed"] else "esperando activación"
    else:
        estado = {"waiting_entry": "esperando entrada", "closed": "cerrada",
                  "stopped": "terminada"}.get(snap["phase"], snap["phase"])
    modo = "REAL" if snap.get("live") else "sim "
    partes = [f"#{snap['id']:<3} {modo} {label(snap):<12} {estado:<22}"]
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
        print(f"PePump · {len(snaps)} posiciones · modo por defecto: {modo}")
        for snap in snaps:
            print("  " + describe(snap))
        return

    if args.cmd == "open":
        defaults = s.get(f"{url}/api/defaults", timeout=5).json()
        settings = {campo: getattr(args, campo) for _f, campo, _a in OPEN_FLAGS
                    if getattr(args, campo) is not None}
        if args.pool:
            settings["pool"] = args.pool
        live = defaults["settings"]["live"] if args.live is None else args.live
        settings["live"] = live
        r = s.get(f"{url}/api/preview", params={"mint": args.mint}, timeout=30)
        if r.status_code != 200:
            fail(r.json().get("detail", str(r.status_code)))
        p = r.json()
        nombre = " ".join(x for x in (p.get("name"), f"(${p['symbol']})" if p.get("symbol") else None) if x)
        print(f"Token:  {nombre or 'sin nombre on-chain'}  {p['mint']}")
        print(f"Dónde:  {MARKETS.get(p['market'], p['market'])}")
        if p.get("price"):
            print(f"Precio: {fmt_price(p['price'])} SOL/token")
        if p.get("active_position"):
            fail(f"ya hay una posición activa de este token (#{p['active_position']})")
        cfg = {**defaults["settings"], **settings}
        print(f"Modo:   {'REAL (SOL de verdad)' if live else 'simulado'}")
        print(f"Compra: {cfg['buy_sol']} SOL · activa +{cfg['activation_pct']}% · trailing "
              f"{cfg['trailing_pct']}% · stop -{cfg['initial_stop_pct']}%"
              + (f" · entra con una baja de {cfg['entry_dip_pct']}%" if cfg["entry_dip_pct"] else ""))
        if not confirm(f"¿Comprar{' con SOL REAL' if live else ' (simulado)'}?", args.yes):
            return
        r = s.post(f"{url}/api/positions", json={"mint": p["mint"], "settings": settings}, timeout=10)
        if r.status_code != 200:
            fail(r.json().get("detail", str(r.status_code)))
        print(f"Posición #{r.json()['id']} abierta. Comprará en cuanto haya precio.")
        return

    if args.cmd == "close":
        ref = args.ref.strip().lstrip("#")
        matches = [x for x in snaps if str(x["id"]) == ref or x["mint"] == ref]
        if not matches:
            fail(f"no hay ninguna posición {args.ref!r}")
        snap = matches[-1]
        print(describe(snap))
        accion = "vender" if snap.get("position") else "cancelar la entrada de"
        if not confirm(f"¿{accion.capitalize()} #{snap['id']}{' (REAL)' if snap.get('live') else ''}?", args.yes):
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
        reales = sum(1 for x in activas if x.get("live"))
        if not confirm(f"¿Cerrar estas {len(activas)}{f' ({reales} REALES)' if reales else ''}?", args.yes):
            return
        r = s.post(f"{url}/api/positions/close-all", timeout=5)
        if r.status_code != 200:
            fail(r.json().get("detail", str(r.status_code)))
        print(f"Cierre pedido para {r.json()['requested']} posiciones.")


if __name__ == "__main__":
    main()
