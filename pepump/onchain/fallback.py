"""
Fuentes de precio on-chain de respaldo, para cuando el feed en vivo de
PumpPortal (subscribeTokenTrade) no entrega nada para un mint: ya migró a
PumpSwap, el feed está mudo con la bonding curve activa, o el token ni
siquiera es de pump.fun (Raydium CPMM, Meteora DLMM).

OnchainFallback decide qué fuente usar, recuerda cuál quedó activa
(`source`) y, según esa fuente, por qué pool hay que rutear la orden
(`pool_override`). Quien decide CUÁNDO recurrir a esto es bot.py.

Valores de `source`:
  None           -> no se usa ningún fallback, todo por el feed en vivo.
  "bondingcurve" -> el mint sigue en bonding curve pero el feed no entrega
                    nada; se lee la cuenta de la curva (PumpCurveOnChainClient).
  "pumpswap"     -> el mint ya migró; se lee el pool oficial de PumpSwap.
  "raydium-cpmm" -> no es de pump.fun; pool de Raydium CPMM contra SOL.
  "meteora-dlmm" -> la liquidez contra SOL está en Meteora DLMM.
  "meteora-damm-v2" -> la liquidez contra SOL está en Meteora DAMM v2 (de
                    los dos de Meteora se usa el pool con más SOL).
  "jupiter"      -> ninguno de los anteriores: el precio es la cotización de
                    venta de Jupiter y las órdenes van por Jupiter (ver
                    jupiter.py). Solo si hay jupiter_api_key.
"""
import asyncio
import logging
from typing import Optional

from pepump.jupiter import JUPITER_POOL, PRIORITY_PRICE
from pepump.onchain.meteora_damm_v2 import MeteoraDammV2OnChainClient
from pepump.onchain.meteora_dlmm import MeteoraDlmmOnChainClient
from pepump.onchain.pump_curve import PumpCurveOnChainClient
from pepump.onchain.pumpswap import PumpSwapOnChainClient
from pepump.onchain.raydium_cpmm import RaydiumCpmmOnChainClient

logger = logging.getLogger(__name__)


class OnchainFallback:
    def __init__(self, mint: str, rpc_url: str, source: Optional[str] = None, jupiter=None,
                 live: bool = False):
        self.mint = mint
        self.rpc_url = rpc_url
        self.source: Optional[str] = source
        # JupiterClient, o None si no hay jupiter_api_key.
        self.jupiter = jupiter
        # Para decidir si las órdenes de Meteora pueden ir por Jupiter: en
        # REAL solo si hay clave con la que firmar (ver pool_override).
        self.live = live
        # Tokens con los que se cotiza la venta en Jupiter: los de la
        # posición una vez comprada (los pone bot.py), y antes de comprar
        # los que darían por buy_sol (ver resolve_jupiter). El precio de
        # Jupiter depende del tamaño (impacto en el pool), por eso se cotiza
        # siempre lo que de verdad se va a vender.
        self.jupiter_tokens: Optional[float] = None
        self.jupiter_priority = PRIORITY_PRICE

    async def fetch_price(self) -> Optional[float]:
        """Consulta puntual al fallback on-chain ACTUALMENTE activo
        (self.source), usada para refrescar/pollear el precio
        de una posición ya resuelta por ese fallback (ver
        bot._poll_onchain_price_loop, bot._resolve_shutdown_price,
        bot._wait_for_dip_entry).

        Si el fallback activo es la bonding curve y ésta ya completó
        (migró mientras estábamos pollando), pasa automáticamente al
        fallback de PumpSwap para ESTA MISMA consulta y deja
        `self.source` en "pumpswap" para las próximas -mismo
        espíritu que bot._handle_feed_stall detectando una migración a
        mitad de posición, pero aquí para el caso en que ya veníamos
        on-chain por bonding curve."""
        if self.source == "jupiter":
            if self.jupiter is None:
                raise RuntimeError("esta posición va por Jupiter y no hay jupiter_api_key configurada")
            if not self.jupiter_tokens:
                raise RuntimeError("no se sabe cuántos tokens cotizar en Jupiter")
            return await self.jupiter.sell_price(self.mint, self.jupiter_tokens, self.rpc_url,
                                                 priority=self.jupiter_priority)
        if self.source == "raydium-cpmm":
            cpmm = RaydiumCpmmOnChainClient(self.rpc_url)
            return await cpmm.fetch_price(self.mint)
        if self.source == "meteora-dlmm":
            dlmm = MeteoraDlmmOnChainClient(self.rpc_url)
            return await dlmm.fetch_price(self.mint)
        if self.source == "meteora-damm-v2":
            damm = MeteoraDammV2OnChainClient(self.rpc_url)
            return await damm.fetch_price(self.mint)
        if self.source == "bondingcurve":
            curve = PumpCurveOnChainClient(self.rpc_url)
            price, complete, exists = await curve.fetch_price_or_status(self.mint)
            if exists and complete:
                logger.info("[On-chain bonding curve] La curva completó (migró) mientras se hacía "
                            "polling -> paso al fallback de PumpSwap.")
                self.source = "pumpswap"
                swap = PumpSwapOnChainClient(self.rpc_url)
                return await swap.fetch_price_for_migrated_mint(self.mint)
            return price
        onchain = PumpSwapOnChainClient(self.rpc_url)
        return await onchain.fetch_price_for_migrated_mint(self.mint)

    async def confirm_not_listed(self) -> bool:
        """Comprobación temprana y SIN efectos secundarios (no toca
        self.source, a diferencia de resolve_reference):
        confirma lo antes posible si este mint NUNCA se lanzó en
        pump.fun, consultando en paralelo con la espera del feed en vivo
        desde el arranque de bot._get_reference_price -en vez de solo
        después de `live_feed_timeout_seconds`, que existe para tolerar
        tokens de poco volumen y no tiene nada que ver con esta pregunta.

        Devuelve True solo si TODAS las consultas on-chain (PumpSwap,
        bonding curve, Raydium CPMM y Meteora DLMM) respondieron bien y confirmaron
        ausencia -nunca ante un fallo de RPC (ver PumpSwapOnChainClient.
        fetch_price_or_confirm_absent y PumpCurveOnChainClient.
        fetch_price_or_status), para no abortar una entrada válida por
        un problema transitorio de conexión."""
        onchain = PumpSwapOnChainClient(self.rpc_url)
        _, pool_confirmed_absent = await onchain.fetch_price_or_confirm_absent(self.mint)
        if not pool_confirmed_absent:
            return False
        curve = PumpCurveOnChainClient(self.rpc_url)
        _, _, exists = await curve.fetch_price_or_status(self.mint)
        if exists is not False:
            return False
        cpmm = RaydiumCpmmOnChainClient(self.rpc_url)
        _, cpmm_confirmed_absent = await cpmm.fetch_price_or_confirm_absent(self.mint)
        if not cpmm_confirmed_absent:
            return False
        _, meteora_confirmed_absent = await self._best_meteora()
        return meteora_confirmed_absent

    async def _best_meteora(self) -> tuple[Optional[tuple[str, float]], bool]:
        """Mejor pool de Meteora contra SOL entre DLMM y DAMM v2: el de más
        SOL. Un token puede tener los dos, y el que manda es donde está la
        liquidez: en un DLMM pequeño el precio solo se mueve a saltos de un
        bin (2% con bin_step 200) y se queda quieto entre medias.
        ((source, price) o None, confirmed_absent): ausencia confirmada solo
        si los DOS lo confirman."""
        (dlmm_price, dlmm_sol, dlmm_absent), (damm_price, damm_sol, damm_absent) = await asyncio.gather(
            MeteoraDlmmOnChainClient(self.rpc_url).fetch_price_reserve_or_confirm_absent(self.mint),
            MeteoraDammV2OnChainClient(self.rpc_url).fetch_price_reserve_or_confirm_absent(self.mint),
        )
        options = [(sol or 0.0, source, price) for source, price, sol in
                   (("meteora-dlmm", dlmm_price, dlmm_sol), ("meteora-damm-v2", damm_price, damm_sol))
                   if price is not None]
        if not options:
            return None, dlmm_absent and damm_absent
        _sol, source, price = max(options)
        return (source, price), False

    async def _try_meteora(self) -> tuple[Optional[float], bool]:
        """Fallback de Meteora (DLMM o DAMM v2, ver _best_meteora) para las
        dos ramas de resolve_reference que no encontraron pool contra SOL en
        pump.fun/PumpSwap/Raydium CPMM. (price, confirmed_absent); si hay
        precio, deja `self.source` en "meteora-dlmm" o "meteora-damm-v2"."""
        best, confirmed_absent = await self._best_meteora()
        if best is None:
            return None, confirmed_absent
        source, price = best
        nombre = "Meteora DLMM" if source == "meteora-dlmm" else "Meteora DAMM v2"
        logger.info(f"Precio de referencia (fallback on-chain {nombre}): {price:.10f} SOL/token")
        self.source = source
        return price, False

    async def resolve_reference(self) -> tuple[Optional[float], bool, bool]:
        """Consulta puntual a los fallbacks on-chain, en dos pasos:

        1. PumpSwap (ver PumpSwapOnChainClient): si aparece un pool con
           precio válido, el mint ya migró de verdad -se usa ese precio.

        2. Si PumpSwap confirma que NO hay pool (`pool_confirmed_absent`
           -el mint sigue en bonding curve), en vez de simplemente
           volver a esperar a ciegas el feed en vivo -que es
           precisamente el que no está entregando nada-, leemos el
           precio DIRECTO de la cuenta de la bonding curve on-chain (ver
           PumpCurveOnChainClient). Si responde con un precio válido
           (curva todavía no completada), lo usamos como referencia y
           quedamos en modo polling on-chain por bonding curve para el
           resto de la posición.

        Devuelve (price, pool_confirmed_absent, mint_not_pumpfun):
          - price no-None: se resolvió por alguno de los dos fallbacks
            -`self.source` queda seteado ("pumpswap",
            "bondingcurve", "raydium-cpmm" o "meteora-dlmm") para que el monitoreo posterior de la
            posición (ver bot._poll_onchain_price_loop) sepa con cuál seguir.
          - price None, pool_confirmed_absent True, mint_not_pumpfun
            True: NI el pool de PumpSwap NI la cuenta de bonding curve
            existen para este mint (ambas consultas respondieron bien y
            confirmaron ausencia, no un fallo de RPC) -> este mint NUNCA
            se lanzó en pump.fun (ej. un mint nativo de Raydium/
            Meteora/otro DEX). No tiene sentido seguir esperando el feed
            en vivo ni reintentar este fallback: nunca va a aparecer
            nada aquí. El llamador (bot._get_reference_price) debería
            abortar de inmediato en vez de esperar hasta el timeout.
          - price None, pool_confirmed_absent True, mint_not_pumpfun
            False: la bonding curve existe pero no dio un precio
            utilizable (ya completada y el pool de PumpSwap todavía no
            está indexado, cuenta con datos ilegibles, o falló la
            consulta on-chain en sí -RPC caído-). El llamador
            (bot._get_reference_price) decide si sigue esperando el feed en
            vivo un ciclo más.
          - price None, pool_confirmed_absent False: la consulta de
            PumpSwap en sí no dio ninguna confirmación útil (se
            encontró un pool pero no se pudo leer, o falló la query) -
            no hay nada más que probar aquí, el llamador debe abortar en
            vez de reintentar a ciegas."""
        onchain = PumpSwapOnChainClient(self.rpc_url)
        price, pool_confirmed_absent = await onchain.fetch_price_or_confirm_absent(self.mint)
        if price is not None:
            logger.info(f"Precio de referencia (fallback on-chain PumpSwap): {price:.10f} SOL/token")
            self.source = "pumpswap"
            return price, pool_confirmed_absent, False

        if pool_confirmed_absent:
            curve = PumpCurveOnChainClient(self.rpc_url)
            curve_price, complete, exists = await curve.fetch_price_or_status(self.mint)
            if curve_price is not None and not complete:
                logger.info(f"Precio de referencia (fallback on-chain bonding curve): "
                            f"{curve_price:.10f} SOL/token")
                self.source = "bondingcurve"
                return curve_price, pool_confirmed_absent, False
            if exists and complete:
                # Curva completada sin pool oficial de PumpSwap contra SOL:
                # o todavía no está indexado, o el pool oficial está
                # cotizado contra otro token y la liquidez contra SOL vive
                # en Meteora DLMM (caso real Hg5Ja5...pump, pool contra PUMP).
                dlmm_price, _ = await self._try_meteora()
                if dlmm_price is not None:
                    return dlmm_price, pool_confirmed_absent, False
                logger.debug("[On-chain bonding curve] La curva ya completó (migrando a PumpSwap) "
                             "pero el pool de PumpSwap todavía no aparece indexado. Reintento en el "
                             "próximo ciclo.")
            elif exists is False:
                # Ni bonding curve ni pool oficial de PumpSwap: no es un
                # token de pump.fun. Último intento: Raydium CPMM contra
                # SOL (ej. tokens de bonk.fun/LaunchLab ya graduados).
                cpmm = RaydiumCpmmOnChainClient(self.rpc_url)
                cpmm_price, cpmm_confirmed_absent = await cpmm.fetch_price_or_confirm_absent(self.mint)
                if cpmm_price is not None:
                    logger.info(f"Precio de referencia (fallback on-chain Raydium CPMM): "
                                f"{cpmm_price:.10f} SOL/token")
                    self.source = "raydium-cpmm"
                    return cpmm_price, pool_confirmed_absent, False
                dlmm_price, dlmm_confirmed_absent = await self._try_meteora()
                if dlmm_price is not None:
                    return dlmm_price, pool_confirmed_absent, False
                if cpmm_confirmed_absent and dlmm_confirmed_absent:
                    # Mensaje de error único para este caso: lo loguea el
                    # llamador (bot._log_mint_not_pumpfun_abort),
                    # no aquí -de lo contrario saldrían dos ERROR casi
                    # idénticos para un solo problema (esta comprobación
                    # normalmente ya lo detectó antes vía
                    # confirm_not_listed).
                    logger.debug(f"[On-chain] {self.mint} no tiene bonding curve de pump.fun, NI pool "
                                 f"de PumpSwap, NI pool de Raydium CPMM/Meteora DLMM utilizable (todas las consultas "
                                 f"confirmaron ausencia).")
                    return price, pool_confirmed_absent, True

        return price, pool_confirmed_absent, False

    async def resolve_jupiter(self, buy_sol: float) -> Optional[float]:
        """Último recurso para un mint que no está en ninguna de las fuentes
        on-chain: cuántos tokens darían por `buy_sol` y a qué precio se
        venderían. Si Jupiter tiene ruta, deja `self.source` en "jupiter" y
        devuelve ese precio de venta; si no (o no hay Jupiter), None."""
        if self.jupiter is None:
            return None
        try:
            tokens = await self.jupiter.buy_tokens(self.mint, buy_sol, self.rpc_url)
            price = await self.jupiter.sell_price(self.mint, tokens, self.rpc_url)
        except Exception as e:
            logger.warning(f"[Jupiter] Tampoco se pudo cotizar {self.mint} en Jupiter: {e}")
            return None
        self.jupiter_tokens = tokens
        self.source = "jupiter"
        logger.info(f"Precio de referencia (cotización de venta en Jupiter, {tokens:,.2f} tokens): "
                    f"{price:.10f} SOL/token")
        return price

    def pool_override(self) -> Optional[str]:
        """Si ya confirmamos -sea al entrar (resolve_reference) o a
        mitad de posición (bot._handle_feed_stall)- que este mint migró de
        la bonding curve a PumpSwap, hay que decírselo explícito a la
        Lightning API con pool="pump-amm" en vez de confiar en
        cfg.pool="auto".

        Motivo: pool="auto" le pide a PumpPortal que resuelva solo por
        dónde rutear la orden, y esa resolución puede quedar pisada con
        la bonding curve vieja para un mint recién migrado. Ahí la orden
        revierte on-chain con el error 6005 (BondingCurveComplete) del
        programa Pump, porque esa curva ya no existe para este mint -
        aunque nosotros ya confirmamos el pool de PumpSwap on-chain.

        OJO: esto es DISTINTO de `self.source == "bondingcurve"`
        -ese caso es el mint SIGUE en bonding curve (solo cambió de
        dónde sacamos el precio, no de dónde hay que rutear el trade),
        así que ahí NO hay que overridear nada -> None, se deja que
        cfg.pool decida como siempre. Overridear a "pump-amm" en ese
        caso rompería exactamente el mismo BondingCurveComplete que este
        override existe para evitar, pero al revés."""
        # Raydium CPMM: se rutea explícito al mismo tipo de pool del que
        # sacamos el precio, en vez de dejar que "auto" lo adivine.
        # Meteora: la Lightning API no tiene un pool "meteora", así que va
        # por Jupiter si se puede (en REAL, solo con clave para firmar), y
        # si no se fuerza "auto" (aunque cfg.pool diga otra cosa) y
        # PumpPortal decide por dónde rutear.
        if self.source is None:
            return None
        if self.source in ("meteora-dlmm", "meteora-damm-v2"):
            if self.jupiter is not None and (self.jupiter.can_sign or not self.live):
                return JUPITER_POOL
            return "auto"
        return {"pumpswap": "pump-amm", "raydium-cpmm": "raydium-cpmm",
                "jupiter": JUPITER_POOL}.get(self.source)

    # ---- Consultas puntuales para detectar una migración a mitad de posición ---- #

    async def migrated_price(self) -> Optional[float]:
        """Precio del pool oficial de PumpSwap, o None si no hay (o falla)."""
        return await PumpSwapOnChainClient(self.rpc_url).fetch_price_for_migrated_mint(self.mint)

    async def curve_status(self) -> tuple[Optional[float], bool, Optional[bool]]:
        """(price, complete, exists) de la bonding curve (ver
        PumpCurveOnChainClient.fetch_price_or_status)."""
        return await PumpCurveOnChainClient(self.rpc_url).fetch_price_or_status(self.mint)
