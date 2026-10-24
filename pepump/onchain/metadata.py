"""Nombre y símbolo de un token, leídos on-chain (solo para mostrar)."""
import logging
from typing import Optional

from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey

from pepump.onchain.programs import TOKEN_METADATA_PROGRAM_ID
from pepump.onchain.rpc import rpc_value

logger = logging.getLogger(__name__)

# Más que esto no aporta nada en un panel y descuadra las columnas.
_MAX_LABEL_LEN = 64


def clean_label(text: Optional[str]) -> Optional[str]:
    """Nombre/símbolo de un token sin caracteres de control. Los pone quien
    crea el token: un símbolo con secuencias de escape ANSI o saltos de
    línea salía tal cual en la terminal (rompía el panel o manipulaba la
    terminal). str.isprintable() deja fuera también los de formato, como
    los que invierten el sentido del texto. El relleno \\0 de Metaplex se va
    con ellos."""
    if text is None:
        return None
    limpio = "".join(c for c in text if c.isprintable()).strip()
    return limpio[:_MAX_LABEL_LEN]


class TokenMetadataOnChainClient:
    """Nombre y símbolo del token, leídos on-chain. Solo se usan para
    mostrarlos (panel / log de arranque): nada de la estrategia depende
    de esto, así que cualquier fallo devuelve None y listo.

    Hay dos sitios donde puede estar, según cómo se creó el mint:
      - Cuenta de Metaplex Token Metadata (PDA ["metadata", programa,
        mint]): los mints SPL Token clásicos de pump.fun.
      - Extensión TokenMetadata de Token-2022, dentro de la propia
        cuenta del mint: los mints que pump.fun crea con Token-2022.
    Se piden las dos cuentas en un solo getMultipleAccounts y se usa la
    que tenga datos."""

    # Layout de Metaplex: key(1) + update_authority(32) + mint(32), y
    # después name/symbol como strings Borsh (u32 de largo + bytes,
    # rellenos con \0 hasta su tamaño máximo).
    _METAPLEX_NAME_OFFSET = 1 + 32 + 32
    # Token-2022: el mint base ocupa 82 bytes, se rellena hasta 165 (largo
    # de una cuenta de token), luego 1 byte de tipo de cuenta y a partir de
    # ahí las extensiones en TLV (u16 tipo, u16 largo, valor).
    _TOKEN2022_TLV_OFFSET = 166
    _TOKEN2022_EXT_TOKEN_METADATA = 19

    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url

    def _metadata_address(self, mint: str) -> Pubkey:
        program = Pubkey.from_string(TOKEN_METADATA_PROGRAM_ID)
        pda, _bump = Pubkey.find_program_address(
            [b"metadata", bytes(program), bytes(Pubkey.from_string(mint))],
            program,
        )
        return pda

    @staticmethod
    def _borsh_string(data: bytes, offset: int) -> tuple[str, int]:
        n = int.from_bytes(data[offset:offset + 4], "little")
        start = offset + 4
        if start + n > len(data):
            raise ValueError("string fuera de la cuenta")
        return clean_label(data[start:start + n].decode("utf-8", errors="replace")) or "", start + n

    @classmethod
    def _parse_metaplex(cls, data: bytes) -> Optional[tuple[str, str]]:
        name, off = cls._borsh_string(data, cls._METAPLEX_NAME_OFFSET)
        symbol, _ = cls._borsh_string(data, off)
        return (name, symbol) if name or symbol else None

    @classmethod
    def _parse_token2022(cls, data: bytes) -> Optional[tuple[str, str]]:
        off = cls._TOKEN2022_TLV_OFFSET
        while off + 4 <= len(data):
            ext_type = int.from_bytes(data[off:off + 2], "little")
            length = int.from_bytes(data[off + 2:off + 4], "little")
            value = data[off + 4:off + 4 + length]
            if ext_type == cls._TOKEN2022_EXT_TOKEN_METADATA:
                # update_authority(32) + mint(32), luego name/symbol Borsh.
                name, o = cls._borsh_string(value, 64)
                symbol, _ = cls._borsh_string(value, o)
                return (name, symbol) if name or symbol else None
            if ext_type == 0 and length == 0:
                break  # relleno: no hay más extensiones
            off += 4 + length
        return None

    async def fetch_name_symbol(self, mint: str) -> Optional[tuple[str, str]]:
        """(nombre, símbolo), o None si no se encontró o falló la consulta.
        Nunca lanza."""
        try:
            pda = self._metadata_address(mint)
            async with AsyncClient(self.rpc_url) as client:
                resp = await client.get_multiple_accounts([pda, Pubkey.from_string(mint)], encoding="base64")
                metaplex_info, mint_info = rpc_value(resp, f"la metadata de {mint}")
            if metaplex_info is not None:
                found = self._parse_metaplex(bytes(metaplex_info.data))
                if found:
                    return found
            if mint_info is not None and len(mint_info.data) > self._TOKEN2022_TLV_OFFSET:
                return self._parse_token2022(bytes(mint_info.data))
            return None
        except Exception as e:
            logger.debug(f"[On-chain metadata] No se pudo leer el nombre de {mint}: {e}")
            return None
