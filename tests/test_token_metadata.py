"""Parseo del nombre/símbolo on-chain (TokenMetadataOnChainClient), con
cuentas armadas a mano: Metaplex y extensión TokenMetadata de Token-2022."""
from pepump.onchain.metadata import TokenMetadataOnChainClient as C


def _borsh(s: str, pad: int = 0) -> bytes:
    raw = s.encode() + b"\x00" * pad
    return len(raw).to_bytes(4, "little") + raw


def test_metaplex_quita_relleno():
    data = b"\x04" + b"\x01" * 64 + _borsh("Pepe Coin", pad=23) + _borsh("PEPE", pad=6) + _borsh("https://x")
    assert C._parse_metaplex(data) == ("Pepe Coin", "PEPE")


def test_token2022_salta_otras_extensiones():
    otra = (1).to_bytes(2, "little") + (8).to_bytes(2, "little") + b"\x00" * 8
    meta_value = b"\x02" * 64 + _borsh("Pepe Coin") + _borsh("PEPE") + _borsh("https://x")
    meta = (19).to_bytes(2, "little") + len(meta_value).to_bytes(2, "little") + meta_value
    data = b"\x00" * 165 + b"\x01" + otra + meta
    assert C._parse_token2022(data) == ("Pepe Coin", "PEPE")


def test_token2022_sin_extension_de_metadata():
    data = b"\x00" * 165 + b"\x01" + (1).to_bytes(2, "little") + (8).to_bytes(2, "little") + b"\x00" * 8
    assert C._parse_token2022(data) is None


def test_nombre_con_secuencias_de_escape_sale_limpio():
    """Los pone quien crea el token: un símbolo con ANSI o saltos de línea
    salía tal cual en la terminal (rompía el panel o la manipulaba)."""
    nombre = "Pepe\x1b[2J\x1b]0;hola\x07 Coin\n\r"
    simbolo = "PE\u202ePE"  # invierte el sentido del texto
    data = b"\x04" + b"\x01" * 64 + _borsh(nombre) + _borsh(simbolo) + _borsh("https://x")
    assert C._parse_metaplex(data) == ("Pepe[2J]0;hola Coin", "PEPE")


def test_nombre_larguisimo_se_recorta():
    data = b"\x04" + b"\x01" * 64 + _borsh("x" * 500) + _borsh("S") + _borsh("https://x")
    assert len(C._parse_metaplex(data)[0]) == 64
