"""
Historial de órdenes cerradas en CSV.

Una única función, `append_closed_trade`, que agrega UNA fila por venta ya
confirmada (real o simulada) a un archivo .csv. Se abre en modo append y se
cierra en cada llamada -no se mantiene el archivo abierto entre ventas- para
que cada fila quede persistida en el momento, sin depender de un flush/close
prolijo al final: si el proceso se corta de golpe (Ctrl+C duro, crash, kill
-9), las ventas ya escritas no se pierden.
"""
import csv
import logging
import os

logger = logging.getLogger(__name__)

# Orden fijo de columnas. Si se le agrega un campo nuevo a una fila en el
# futuro, hay que agregarlo acá también (y el header de archivos .csv viejos
# va a quedar corto -no se migra automáticamente, hay que rotarlo a mano).
CSV_FIELDS = [
    "closed_at",        # timestamp ISO-8601 (UTC) de cuándo se cerró la orden
    "mint",
    "mode",              # "REAL" o "SIMULADO"
    "entry_price",        # SOL por token, al comprar
    "exit_price",         # SOL por token, al vender
    "sol_amount",          # SOL invertidos en la compra
    "token_amount",         # tokens vendidos (estimado)
    "proceeds_sol",          # SOL recibidos (aprox) en la venta
    "pnl_pct",
    "duration_seconds",       # tiempo que estuvo abierta la posición
    "reason",                  # motivo del cierre (trailing-stop, stop-loss, etc.)
]


def append_closed_trade(path: str, row: dict) -> None:
    """Agrega una fila al CSV de historial en `path`, escribiendo el header
    primero si el archivo todavía no existe o está vacío.

    Nunca propaga excepciones: un problema de disco (permisos, ruta
    inválida, disco lleno) no debe tirar abajo el bot ni, peor, hacer
    parecer que la VENTA en sí falló -la venta ya se ejecutó (o se simuló)
    antes de llegar acá, así que como mucho se pierde el registro en el
    historial, nunca la operación. Solo se loguea como warning.
    """
    if not path:
        return
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        write_header = not os.path.exists(path) or os.path.getsize(path) == 0
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            if write_header:
                writer.writeheader()
            writer.writerow(row)
    except OSError as e:
        logger.warning(f"No se pudo escribir en el historial de órdenes cerradas ({path}): {e}")
