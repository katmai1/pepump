"""Precio de un mint ya migrado, desde su pool oficial de PumpSwap."""
import logging
from typing import Optional

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Processed
from solders.pubkey import Pubkey

from pepump.onchain.programs import PUMPFUN_PROGRAM_ID, PUMPSWAP_PROGRAM_ID, WSOL_MINT
from pepump.onchain.rpc import rpc_value

logger = logging.getLogger(__name__)


class PumpSwapOnChainClient:
    """
    Fallback de precio ÚNICAMENTE para mints que ya migraron a PumpSwap
    (ver bot.py: se usa solo si subscribeTokenTrade confirma el ack pero
    no entrega NINGÚN trade dentro de `live_feed_timeout_seconds` — el
    síntoma real, confirmado a mano, de un mint que ya salió de la
    bonding curve). Lee las reservas del pool DIRECTO de Solana vía RPC
    -la misma cuenta contra la que se ejecutaría el trade real- así que
    no mete ningún desfase de una fuente externa tipo DexScreener.

    El descubrimiento del pool a partir del mint y el parseo de la cuenta
    los hacemos nosotros, con un getProgramAccounts + memcmp directo
    sobre el programa de PumpSwap y los offsets de abajo.

    Antes esta parte se apoyaba en la librería `pumpswapamm`
    (github.com/FLOCK4H/PumpSwapAMM) para leer el pool y su precio, pero
    su `fetch_pool_base_price` cotiza `quote_vault / base_vault` a secas
    -sin la reserva VIRTUAL de quote que el programa sí suma en su curva
    (ver _VIRTUAL_QUOTE_OFFSET)-, así que daba un precio hasta ~27% por
    debajo del real en los pools nuevos. Parseando aquí, además, se deja
    de depender de una librería externa sin auditar.
    """

    # Offsets en bytes dentro de la cuenta del pool, verificados contra el
    # struct `Pool` del IDL que el propio programa de PumpSwap publica
    # on-chain (cuenta anchor:idl). Los campos son todos de largo fijo y
    # los layouts viejo/nuevo coinciden hasta `lp_supply` -solo difieren
    # en lo que viene después-, así que estos offsets valen para todos:
    #   0   discriminador Anchor        (8)
    #   8   pool_bump                   (1)
    #   9   index                       (2)
    #   11  creator                     (32)
    #   43  base_mint                   (32)
    #   75  quote_mint                  (32)
    #   107 lp_mint                     (32)
    #   139 pool_base_token_account     (32)
    #   171 pool_quote_token_account    (32)
    #   203 lp_supply                   (u64 little-endian)
    #   211 coin_creator               (32)
    #   243 is_mayhem_mode             (1)
    #   244 is_cashback_coin           (1)
    #   245 reserva VIRTUAL de quote   (u64 little-endian, lamports)
    _CREATOR_OFFSET = 11
    _BASE_MINT_OFFSET = 43
    _QUOTE_MINT_OFFSET = 75
    _BASE_VAULT_OFFSET = 139
    _QUOTE_VAULT_OFFSET = 171
    _LP_SUPPLY_OFFSET = 203
    _MIN_POOL_ACCOUNT_LEN = _LP_SUPPLY_OFFSET + 8

    # BUGFIX (precio ~27% por debajo del real): el precio de un pool de
    # PumpSwap NO es `quote_vault / base_vault`. Los pools nuevos (301
    # bytes) traen, después de los dos flags `is_mayhem_mode` /
    # `is_cashback_coin`, un u64 con una reserva VIRTUAL de quote en
    # lamports que el programa SUMA a la reserva real del vault para su
    # curva x*y=k. Ese campo todavía no está en el IDL publicado on-chain
    # (el struct `Pool` del IDL termina en `is_cashback_coin`), pero está
    # verificado contra la cadena:
    #
    #   - Caso FZrn97...2cra (FLIPPY): vaults = 231.429.599 tokens /
    #     66,0657 SOL -> ratio crudo 0,0000002855. Campo virtual =
    #     17,5845 SOL -> (66,0657 + 17,5845) / 231.429.599 =
    #     0,00000036145, que es el precio real (DexScreener: 0,0000003619).
    #   - Despejando la reserva efectiva de quote de la matemática de los
    #     BuyEvent/SellEvent reales del programa da 17.584.502.436 /
    #     17.584.468.479 / 17.584.505.258 lamports, contra los
    #     17.584.505.366 que trae este campo: coincide hasta el lamport.
    #   - En pools donde el campo es 0 (los viejos), sumarlo no cambia
    #     nada y el precio sigue siendo el ratio de siempre.
    #
    # Ignorarlo hacía que el bot entrara con un precio de referencia ~21%
    # más bajo que el real (y que el stop-loss saltara solo, porque el
    # precio de entrada REAL leído del fill sí era el correcto).
    _VIRTUAL_QUOTE_OFFSET = 245
    _MIN_POOL_ACCOUNT_LEN_CON_VIRTUAL = _VIRTUAL_QUOTE_OFFSET + 8

    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url

    async def fetch_price_for_migrated_mint(self, mint: str) -> Optional[float]:
        """Busca el pool de PumpSwap para `mint` y devuelve su precio
        actual en SOL/token leyendo las reservas on-chain. None si no
        encuentra el pool, si no está denominado en SOL, o si falla la
        lectura (red, RPC caído, etc.) — nunca lanza ninguna excepción hacia
        arriba, para que el bot pueda seguir esperando el feed en vivo
        en vez de caerse por un problema de este fallback secundario."""
        price, _pool_confirmed_absent = await self.fetch_price_or_confirm_absent(mint)
        return price

    async def fetch_price_or_confirm_absent(self, mint: str) -> tuple[Optional[float], bool]:
        """Igual que `fetch_price_for_migrated_mint`, pero además devuelve
        `pool_confirmed_absent`: True ÚNICAMENTE cuando el
        getProgramAccounts para este mint respondió sin lanzar ninguna excepción
        y no encontró ningún pool -es decir, una confirmación limpia de
        que el mint TODAVÍA NO migró a PumpSwap (sigue en bonding
        curve). En cualquier otro caso (se encontró un pool pero no se
        pudo leer/parsear, no está denominado en SOL, o la consulta
        on-chain en sí falló) devuelve False, porque ahí no hay ninguna
        confirmación real de que el mint no haya migrado -puede ser un
        problema genuino de RPC, no un mint sin pool- así que el
        llamador no debería asumir que conviene reintentar el feed en
        vivo."""
        async with AsyncClient(self.rpc_url) as client:
            try:
                pool_address, pool = await self._find_pool(client, mint)
                if pool_address is None:
                    logger.debug(f"[On-chain PumpSwap] No se encontró ningún pool de PumpSwap para {mint}.")
                    return None, True

                if pool is None:
                    logger.debug("[On-chain PumpSwap] No se pudo leer/parsear la cuenta del pool.")
                    return None, False

                if pool["quote_mint"] != WSOL_MINT:
                    logger.debug(f"[On-chain PumpSwap] El pool de {mint} no está denominado en SOL "
                                 f"(quote_mint={pool['quote_mint']}); no lo puedo usar aquí.")
                    return None, False

                reserves = await self._fetch_reserves(client, pool)
                if reserves is None:
                    logger.debug("[On-chain PumpSwap] No se pudieron leer las reservas del pool.")
                    return None, False

                base_balance, quote_balance = reserves
                # La reserva de quote que usa el programa para su curva es
                # la del vault MÁS la virtual (ver _VIRTUAL_QUOTE_OFFSET).
                virtual_quote = pool["virtual_quote_lamports"] / 1e9
                if not base_balance:
                    return None, False
                price = (quote_balance + virtual_quote) / base_balance
                if price <= 0:
                    return None, False

                logger.debug(f"[On-chain PumpSwap] Pool {pool_address} | reservas: "
                             f"{base_balance} tokens / {quote_balance} SOL"
                             + (f" + {virtual_quote} SOL virtuales" if virtual_quote else ""))
                return float(price), False
            except Exception as e:
                logger.warning(f"[On-chain PumpSwap] Falló la consulta on-chain para {mint}: {e}")
                return None, False

    @classmethod
    def _pool_quote_mint(cls, data: bytes) -> Optional[str]:
        """quote_mint del pool, leído del account data crudo."""
        if data is None or len(data) < cls._MIN_POOL_ACCOUNT_LEN:
            return None
        raw = data[cls._QUOTE_MINT_OFFSET:cls._QUOTE_MINT_OFFSET + 32]
        try:
            return str(Pubkey.from_bytes(raw))
        except Exception:
            return None

    @classmethod
    def _pool_lp_supply(cls, data: bytes) -> Optional[int]:
        """lp_supply del pool (u64 little-endian), leído del account data
        crudo."""
        if data is None or len(data) < cls._MIN_POOL_ACCOUNT_LEN:
            return None
        return int.from_bytes(data[cls._LP_SUPPLY_OFFSET:cls._LP_SUPPLY_OFFSET + 8], "little")

    @staticmethod
    def canonical_pool_creator(mint: str) -> str:
        """`creator` que tiene el pool OFICIAL de migración de pump.fun: el
        PDA ["pool-authority", mint] del programa de pump.fun. Cualquier
        otro pool de PumpSwap para el mismo mint lo creó a mano una wallet
        cualquiera."""
        pda, _bump = Pubkey.find_program_address(
            [b"pool-authority", bytes(Pubkey.from_string(mint))],
            Pubkey.from_string(PUMPFUN_PROGRAM_ID),
        )
        return str(pda)

    @classmethod
    def _pool_creator(cls, data: bytes) -> Optional[str]:
        """creator del pool, leído del account data crudo."""
        if data is None or len(data) < cls._MIN_POOL_ACCOUNT_LEN:
            return None
        try:
            return str(Pubkey.from_bytes(data[cls._CREATOR_OFFSET:cls._CREATOR_OFFSET + 32]))
        except Exception:
            return None

    @classmethod
    def _pool_virtual_quote_lamports(cls, data) -> int:
        """Reserva VIRTUAL de quote del pool, en lamports (ver
        _VIRTUAL_QUOTE_OFFSET). 0 si la cuenta es de un layout viejo/más
        corto que no trae el campo -ahí no hay nada que sumar."""
        if data is None or len(data) < cls._MIN_POOL_ACCOUNT_LEN_CON_VIRTUAL:
            return 0
        return int.from_bytes(
            bytes(data)[cls._VIRTUAL_QUOTE_OFFSET:cls._VIRTUAL_QUOTE_OFFSET + 8], "little")

    @classmethod
    def _parse_pool(cls, data) -> Optional[dict]:
        """Los campos del pool que hacen falta para cotizarlo, leídos del
        account data crudo que ya trajo el getProgramAccounts -sin ningún
        round-trip extra de RPC. None si la cuenta está cortada."""
        if data is None or len(data) < cls._MIN_POOL_ACCOUNT_LEN:
            return None
        data = bytes(data)
        return {
            "quote_mint": cls._pool_quote_mint(data),
            "base_vault": str(Pubkey.from_bytes(
                data[cls._BASE_VAULT_OFFSET:cls._BASE_VAULT_OFFSET + 32])),
            "quote_vault": str(Pubkey.from_bytes(
                data[cls._QUOTE_VAULT_OFFSET:cls._QUOTE_VAULT_OFFSET + 32])),
            "virtual_quote_lamports": cls._pool_virtual_quote_lamports(data),
        }

    async def _fetch_reserves(self, client: AsyncClient, pool: dict) -> Optional[tuple[float, float]]:
        """(reserva de tokens, reserva de SOL) de los vaults del pool, en
        unidades enteras (no lamports), en UNA sola llamada de RPC. None
        si alguna de las dos cuentas no se pudo leer."""
        resp = await client.get_multiple_accounts_json_parsed(
            [Pubkey.from_string(pool["base_vault"]), Pubkey.from_string(pool["quote_vault"])],
            commitment=Processed,
        )
        infos = rpc_value(resp, "los vaults del pool de PumpSwap")
        if not infos or len(infos) < 2 or infos[0] is None or infos[1] is None:
            return None
        balances = []
        for info in infos:
            amount = info.data.parsed["info"]["tokenAmount"]
            # Se calcula desde el entero crudo (no desde `uiAmount`, que
            # viene como float y puede llegar en None).
            balances.append(int(amount["amount"]) / 10 ** int(amount["decimals"]))
        return balances[0], balances[1]

    async def _find_pool_address(self, client: AsyncClient, mint: str) -> Optional[str]:
        """Dirección del pool elegido por _find_pool (ver más arriba)."""
        address, _pool = await self._find_pool(client, mint)
        return address

    @classmethod
    def canonical_pool_address(cls, mint: str) -> str:
        """Dirección del pool OFICIAL de migración de pump.fun en PumpSwap,
        calculada sin consultar nada: es el PDA ["pool", index (u16 LE, 0),
        creator, base_mint, quote_mint] del programa de PumpSwap, con
        creator = canonical_pool_creator(mint) y quote = WSOL. Verificado
        contra pools reales de mainnet (ver tests)."""
        pda, _bump = Pubkey.find_program_address(
            [b"pool", (0).to_bytes(2, "little"),
             bytes(Pubkey.from_string(cls.canonical_pool_creator(mint))),
             bytes(Pubkey.from_string(mint)),
             bytes(Pubkey.from_string(WSOL_MINT))],
            Pubkey.from_string(PUMPSWAP_PROGRAM_ID),
        )
        return str(pda)

    async def _find_pool(self, client: AsyncClient, mint: str) -> tuple[Optional[str], Optional[dict]]:
        """Pool oficial de migración de `mint` en PumpSwap: (dirección,
        campos ya parseados -ver _parse_pool-), (None, None) si no existe
        (el mint no migró, o nunca fue de pump.fun), o (dirección, None) si
        existe pero no se puede usar.

        BUGFIX (429 Too Many Requests): antes se buscaba con un
        getProgramAccounts sobre TODO el programa de PumpSwap, filtrando
        por base_mint. Es la consulta más cara de Solana y los RPC le dan
        un cupo propio muy bajo (en Helius, el plan gratis se agota
        enseguida), y se repetía en cada comprobación de migración (cada
        stall_timeout_seconds sin trades, por posición) y en cada ciclo del
        polling on-chain de un token ya migrado. Con varias posiciones de
        poco volumen a la vez, el RPC empezaba a responder 429.

        Como solo cuenta el pool OFICIAL (ver el BUGFIX de abajo), su
        dirección se puede calcular (canonical_pool_address) y basta un
        getAccountInfo, que es de las consultas más baratas.

        BUGFIX (anterior, se mantiene): cualquiera puede crear un pool de
        PumpSwap para cualquier mint -incluidos tokens de bonk.fun/LaunchLab
        que nunca pasaron por pump.fun-. Esos pools de relleno (lp_supply de
        100, reservas ridículas) daban un precio de referencia inventado, el
        bot forzaba pool="pump-amm" y la Lightning API respondía "Pool
        account not found", porque solo rutea contra el pool oficial de
        migración. Solo se usa el pool cuyo `creator` es el pool-authority
        de pump.fun para este mint, que es exactamente el que da el PDA."""
        address = self.canonical_pool_address(mint)
        resp = await client.get_account_info(Pubkey.from_string(address), encoding="base64")
        info = rpc_value(resp, f"el pool oficial de PumpSwap de {mint}")
        if info is None:
            return None, None

        data = bytes(info.data)
        owner = str(getattr(info, "owner", PUMPSWAP_PROGRAM_ID))
        if owner != PUMPSWAP_PROGRAM_ID:
            logger.warning(f"[On-chain PumpSwap] La cuenta {address} no es del programa de PumpSwap "
                           f"(owner {owner}); no la uso.")
            return address, None
        base_mint = (str(Pubkey.from_bytes(data[self._BASE_MINT_OFFSET:self._BASE_MINT_OFFSET + 32]))
                     if len(data) >= self._BASE_MINT_OFFSET + 32 else None)
        if base_mint != mint or self._pool_creator(data) != self.canonical_pool_creator(mint):
            logger.warning(f"[On-chain PumpSwap] El pool {address} no corresponde a {mint} "
                           f"(¿cambió el layout?); no lo uso.")
            return address, None
        return address, self._parse_pool(data)
