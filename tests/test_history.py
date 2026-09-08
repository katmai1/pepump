import csv
import os

from pepump.history import CSV_FIELDS, append_closed_trade


def _row(**overrides):
    row = {
        "closed_at": "2026-08-30T12:00:00+00:00",
        "mint": "TestMint1111111111111111111111111111111111",
        "mode": "SIMULADO",
        "entry_price": "1.0000000000",
        "exit_price": "1.2000000000",
        "sol_amount": "0.050000000",
        "token_amount": "0.050000",
        "proceeds_sol": "0.060000000",
        "pnl_pct": "20.0000",
        "duration_seconds": "12.3",
        "reason": "trailing-stop",
    }
    row.update(overrides)
    return row


def test_append_creates_file_with_header_and_row(tmp_path):
    path = str(tmp_path / "history.csv")

    append_closed_trade(path, _row())

    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert rows[0] == CSV_FIELDS
    assert rows[1][CSV_FIELDS.index("mint")] == "TestMint1111111111111111111111111111111111"
    assert rows[1][CSV_FIELDS.index("reason")] == "trailing-stop"


def test_append_twice_writes_header_only_once(tmp_path):
    path = str(tmp_path / "history.csv")

    append_closed_trade(path, _row(reason="stop-loss inicial"))
    append_closed_trade(path, _row(reason="trailing-stop"))

    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert rows[0] == CSV_FIELDS
    assert len(rows) == 3  # header + 2 filas
    assert rows[1][CSV_FIELDS.index("reason")] == "stop-loss inicial"
    assert rows[2][CSV_FIELDS.index("reason")] == "trailing-stop"


def test_append_creates_intermediate_directories(tmp_path):
    path = str(tmp_path / "nested" / "dir" / "history.csv")

    append_closed_trade(path, _row())

    assert os.path.exists(path)


def test_empty_path_is_a_noop(tmp_path):
    # No debe tirar ni crear nada raro; el caller (executor.py) usa esto
    # para desactivar el historial con trade_history_csv = "".
    append_closed_trade("", _row())
    # Nada que assertear salvo que no explota.


def test_append_does_not_raise_on_unwritable_path(tmp_path, monkeypatch, caplog):
    # Simula un error de disco (permisos, disco lleno, etc.): append_closed_trade
    # debe absorberlo y solo loguear un warning, nunca propagar la excepción
    # -la venta ya se ejecutó antes de llegar acá, no puede "fallar" por esto.
    bad_path = str(tmp_path / "history.csv")

    def boom(*args, **kwargs):
        raise OSError("disco lleno (simulado)")

    monkeypatch.setattr("builtins.open", boom)
    with caplog.at_level("WARNING"):
        append_closed_trade(bad_path, _row())
    assert "No se pudo escribir" in caplog.text
