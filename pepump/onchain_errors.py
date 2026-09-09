"""
Traducción de códigos de error on-chain a algo legible.

Cuando una compra/venta revierte, el RPC solo devuelve algo como
`InstructionError(3, Custom(6005))`. El número por sí solo no dice nada, y
peor: el MISMO número significa cosas distintas según el programa que lo
tiró (6005 es `BondingCurveComplete` en pump.fun pero `InvalidAdmin` en
PumpSwap). Por eso acá hay una tabla POR PROGRAMA, y
`failing_program_from_logs()` para saber cuál de ellas aplica leyendo la
línea "Program <id> failed:" de los logs de la transacción.

Las tablas de pump.fun y PumpSwap salen de los IDL oficiales publicados en
https://github.com/pump-fun/pump-public-docs/tree/main/idl (`pump.json` y
`pump_amm.json`), con los mensajes tal cual los define el programa -en
inglés a propósito: así son greppables contra el IDL y contra cualquier
explorer, sin una traducción propia que se desincronice-. Para
regenerarlas, bajá esos dos .json y volcá su lista `errors`.

La de spl-token viene de `spl_token::error::TokenError`, que es estable y
no cambia. Casi cualquier ruta de trading termina haciendo un CPI a
spl-token para mover el WSOL o el token, así que sus códigos también se
ven seguido.
"""
import re
from typing import Optional

# Anchor reserva los códigos custom a partir de 6000 para los errores
# definidos por el programa; abajo de eso son errores de programas nativos
# (spl-token, system, etc.). Sirve como desempate cuando no se pudo
# identificar el programa que falló.
ANCHOR_ERROR_CODE_OFFSET = 6000

PUMPFUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP_PROGRAM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
SPL_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

PROGRAM_NAMES = {
    PUMPFUN_PROGRAM_ID: "pump.fun",
    PUMPSWAP_PROGRAM_ID: "PumpSwap",
    SPL_TOKEN_PROGRAM_ID: "spl-token",
    TOKEN_2022_PROGRAM_ID: "spl-token-2022",
}

_SPL_TOKEN_ERRORS = {
    0: "NotRentExempt: la cuenta quedaría por debajo del mínimo exento de rent",
    1: "InsufficientFunds: fondos insuficientes para la transferencia (revisá el balance de SOL/WSOL o del token)",
    2: "InvalidMint",
    3: "MintMismatch: el mint de la cuenta no coincide con el esperado",
    4: "OwnerMismatch: la cuenta no pertenece al owner esperado",
    5: "FixedSupply",
    6: "AlreadyInUse",
    7: "InvalidNumberOfProvidedSigners",
    8: "InvalidNumberOfRequiredSigners",
    9: "UninitializedState",
    10: "NativeNotSupported",
    11: "NonNativeHasBalance",
    12: "InvalidInstruction",
    13: "InvalidState",
    14: "Overflow",
    15: "AuthorityTypeNotSupported",
    16: "MintCannotFreeze",
    17: "AccountFrozen",
    18: "MintDecimalsMismatch",
    19: "NonNativeNotSupported",
}

_PUMPFUN_ERRORS = {
    6000: "NotAuthorized: The given account is not authorized to execute this instruction.",
    6001: "AlreadyInitialized: The program is already initialized.",
    6002: "TooMuchSolRequired: slippage: Too much SOL required to buy the given amount of tokens.",
    6003: "TooLittleSolReceived: slippage: Too little SOL received to sell the given amount of tokens.",
    6004: "MintDoesNotMatchBondingCurve: The mint does not match the bonding curve.",
    6005: "BondingCurveComplete: The bonding curve has completed and liquidity migrated to raydium.",
    6006: "BondingCurveNotComplete: The bonding curve has not completed.",
    6007: "NotInitialized: The program is not initialized.",
    6008: "WithdrawTooFrequent: Withdraw too frequent",
    6009: "NewSizeShouldBeGreaterThanCurrentSize: new_size should be > current_size",
    6010: "AccountTypeNotSupported: Account type not supported",
    6011: "InitialRealTokenReservesShouldBeLessThanTokenTotalSupply: initial_real_token_reserves should be less than token_total_supply",
    6012: "InitialVirtualTokenReservesShouldBeGreaterThanInitialRealTokenReserves: initial_virtual_token_reserves should be greater than initial_real_token_reserves",
    6013: "FeeBasisPointsGreaterThanMaximum: fee_basis_points greater than maximum",
    6014: "AllZerosWithdrawAuthority: Withdraw authority cannot be set to System Program ID",
    6015: "PoolMigrationFeeShouldBeLessThanFinalRealSolReserves: pool_migration_fee should be less than final_real_sol_reserves",
    6016: "PoolMigrationFeeShouldBeGreaterThanCreatorFeePlusMaxMigrateFees: pool_migration_fee should be greater than creator_fee + MAX_MIGRATE_FEES",
    6017: "DisabledWithdraw: Migrate instruction is disabled",
    6018: "DisabledMigrate: Migrate instruction is disabled",
    6019: "InvalidCreator: Invalid creator pubkey",
    6020: "BuyZeroAmount: Buy zero amount",
    6021: "NotEnoughTokensToBuy: Not enough tokens to buy",
    6022: "SellZeroAmount: Sell zero amount",
    6023: "NotEnoughTokensToSell: Not enough tokens to sell",
    6024: "Overflow: Overflow",
    6025: "Truncation: Truncation",
    6026: "DivisionByZero: Division by zero",
    6027: "NotEnoughRemainingAccounts: Not enough remaining accounts",
    6028: "AllFeeRecipientsShouldBeNonZero: All fee recipients should be non-zero",
    6029: "UnsortedNotUniqueFeeRecipients: Unsorted or not unique fee recipients",
    6030: "CreatorShouldNotBeZero: Creator should not be zero",
    6031: "StartTimeInThePast",
    6032: "EndTimeInThePast",
    6033: "EndTimeBeforeStartTime",
    6034: "TimeRangeTooLarge",
    6035: "EndTimeBeforeCurrentDay",
    6036: "SupplyUpdateForFinishedRange",
    6037: "DayIndexAfterEndIndex",
    6038: "DayInActiveRange",
    6039: "InvalidIncentiveMint",
    6040: "BuyNotEnoughSolToCoverRent: Buy: Not enough SOL to cover for rent exemption.",
    6041: "BuyNotEnoughSolToCoverFees: Buy: Not enough SOL to cover for fees.",
    6042: "BuySlippageBelowMinTokensOut: Slippage: Would buy less tokens than expected min_tokens_out",
    6043: "NameTooLong",
    6044: "SymbolTooLong",
    6045: "UriTooLong",
    6046: "CreateV2Disabled",
    6047: "CpitializeMayhemFailed",
    6048: "MayhemModeDisabled",
    6049: "CreatorMigratedToSharingConfig: creator has been migrated to sharing config, use pump_fees::reset_fee_sharing_config instead",
    6050: "UnableToDistributeCreatorVaultMigratedToSharingConfig: creator_vault has been migrated to sharing config, use pump:distribute_creator_fees instead",
    6051: "SharingConfigNotActive: Sharing config is not active",
    6052: "UnableToDistributeCreatorFeesToExecutableRecipient: The recipient account is executable, so it cannot receive lamports, remove it from the team first",
    6053: "BondingCurveAndSharingConfigCreatorMismatch: Bonding curve creator does not match sharing config",
    6054: "ShareholdersAndRemainingAccountsMismatch: Remaining accounts do not match shareholders, make sure to pass exactly the same pubkeys in the same order",
    6055: "InvalidShareBps: Share bps must be greater than 0",
    6056: "CashbackNotEnabled: Cashback is not enabled",
    6057: "BuybackFeeRecipientNotAuthorized: Buyback fee recipient not authorized",
    6058: "AllBuybackFeeRecipientsShouldBeNonZero",
    6059: "NotUniqueBuybackFeeRecipients",
    6060: "BuybackBasisPointsOutOfRange: buyback_basis_points must be <= 10_000",
    6061: "WrongBuybackFeeRecipientsCount: buyback fee recipients require exactly 8 remaining accounts (or none)",
    6062: "BuybackFeeRecipientMissing",
    6063: "UnsupportedQuoteMint: Unsupported quote mint",
    6064: "InvalidQuoteTokenProgram: Create v2: quote token program must be legacy SPL Token",
    6065: "InvalidAssociatedQuoteBondingCurve: Create v2: associated quote bonding curve address does not match derivation",
    6066: "QuoteMintWhitelistFull: Quote mint whitelist is full",
    6067: "QuoteMintAlreadyWhitelisted: Quote mint is already whitelisted",
    6068: "QuoteMintNotWhitelisted: Quote mint is not in the whitelist",
    6069: "QuoteMintNotEligibleForWhitelist: Quote mint cannot be added or removed via whitelist (default or native SOL mint)",
    6070: "UnableToDistributeCreatorFeesToUninitializedAccount: Unable to distribute creator fees to uninitialized account",
    6071: "MayhemModeQuoteMintNotAllowed: Mayhem mode quote mint not allowed",
}

_PUMPSWAP_ERRORS = {
    6000: "FeeBasisPointsExceedsMaximum",
    6001: "ZeroBaseAmount",
    6002: "ZeroQuoteAmount",
    6003: "TooLittlePoolTokenLiquidity",
    6004: "ExceededSlippage",
    6005: "InvalidAdmin",
    6006: "UnsupportedBaseMint",
    6007: "UnsupportedQuoteMint",
    6008: "InvalidBaseMint",
    6009: "InvalidQuoteMint",
    6010: "InvalidLpMint",
    6011: "AllProtocolFeeRecipientsShouldBeNonZero",
    6012: "UnsortedNotUniqueProtocolFeeRecipients",
    6013: "InvalidProtocolFeeRecipient",
    6014: "InvalidPoolBaseTokenAccount",
    6015: "InvalidPoolQuoteTokenAccount",
    6016: "BuyMoreBaseAmountThanPoolReserves",
    6017: "DisabledCreatePool",
    6018: "DisabledDeposit",
    6019: "DisabledWithdraw",
    6020: "DisabledBuy",
    6021: "DisabledSell",
    6022: "SameMint",
    6023: "Overflow",
    6024: "Truncation",
    6025: "DivisionByZero",
    6026: "NewSizeLessThanCurrentSize",
    6027: "AccountTypeNotSupported",
    6028: "OnlyCanonicalPumpPoolsCanHaveCoinCreator",
    6029: "InvalidAdminSetCoinCreatorAuthority",
    6030: "StartTimeInThePast",
    6031: "EndTimeInThePast",
    6032: "EndTimeBeforeStartTime",
    6033: "TimeRangeTooLarge",
    6034: "EndTimeBeforeCurrentDay",
    6035: "SupplyUpdateForFinishedRange",
    6036: "DayIndexAfterEndIndex",
    6037: "DayInActiveRange",
    6038: "InvalidIncentiveMint",
    6039: "BuyNotEnoughQuoteTokensToCoverFees: buy: Not enough quote tokens to cover for fees.",
    6040: "BuySlippageBelowMinBaseAmountOut: buy: slippage - would buy less tokens than expected min_base_amount_out",
    6041: "MayhemModeDisabled",
    6042: "OnlyPumpPoolsMayhemMode",
    6043: "MayhemModeInDesiredState",
    6044: "NotEnoughRemainingAccounts",
    6045: "InvalidSharingConfigBaseMint",
    6046: "InvalidSharingConfigCoinCreator",
    6047: "CoinCreatorMigratedToSharingConfig: coin creator has been migrated to sharing config, use pump_fees::reset_fee_sharing_config instead",
    6048: "CreatorVaultMigratedToSharingConfig: creator_vault has been migrated to sharing config, use pump:distribute_creator_fees instead",
    6049: "CashbackNotEnabled: Cashback is disabled",
    6050: "OnlyPumpPoolsCashback",
    6051: "CashbackNotInDesiredState",
    6052: "TokensInVaultLessThanCashbackEarned",
    6053: "BuybackFeeRecipientNotAuthorized: Buyback fee recipient not authorized",
    6054: "AllBuybackFeeRecipientsShouldBeNonZero",
    6055: "NotUniqueBuybackFeeRecipients",
    6056: "BuybackBasisPointsOutOfRange: buyback_basis_points must be <= 10_000",
    6057: "WrongBuybackFeeRecipientsCount: buyback fee recipients require exactly 8 remaining accounts (or none)",
    6058: "BuybackFeeRecipientMissing",
    6059: "MissingCashbackAccounts: Cashback trade is missing the required remaining accounts",
    6060: "InvalidCashbackAccumulator: Cashback user_volume_accumulator account is invalid",
    6061: "InvalidCashbackAccumulatorAta: Cashback user_volume_accumulator ATA is missing or invalid",
    6062: "InvalidPoolV2: pool_v2 remaining account is missing or invalid",
    6063: "InsufficientRealQuoteReserves: BOOST: sell output exceeds the real quote vault. effective = real + virtual is pricing-only; payout is capped at real_vault, so quote min(out, real_vault)",
    6064: "BoostPoolLiquidityUnsupported: BOOST: deposit/withdraw don't apply to boost pools",
    6065: "PoolCannotBoost: BOOST: pool cannot be boosted (no virtual reserves)",
    6066: "BoostDisabled: BOOST: boost is disabled",
    6067: "SeedLockViolation: BOOST: lp_supply must never drop below the circulating LP mint supply",
}

ERROR_TABLES = {
    PUMPFUN_PROGRAM_ID: _PUMPFUN_ERRORS,
    PUMPSWAP_PROGRAM_ID: _PUMPSWAP_ERRORS,
    SPL_TOKEN_PROGRAM_ID: _SPL_TOKEN_ERRORS,
    TOKEN_2022_PROGRAM_ID: _SPL_TOKEN_ERRORS,
}

# Los logs de Solana marcan el programa que revirtió con una línea del
# estilo "Program <pubkey> failed: custom program error: 0x1775".
_FAILED_PROGRAM_RE = re.compile(r"Program ([1-9A-HJ-NP-Za-km-z]{32,44}) failed")


def failing_program_from_logs(logs) -> Optional[str]:
    """Devuelve el program id que revirtió, sacado de los logs de la
    transacción. Si hay varios (un CPI que falla hace fallar también al
    programa que lo llamó), gana el PRIMERO: es el más adentro de la
    cadena, o sea el que realmente originó el error. None si no matchea
    ninguna línea."""
    for line in logs or []:
        m = _FAILED_PROGRAM_RE.search(line)
        if m:
            return m.group(1)
    return None


def describe_custom_error(code: int, program_id: Optional[str] = None) -> Optional[str]:
    """Traduce un código `Custom(N)` a "<programa> <Nombre>: <mensaje>".

    `program_id` es el que devolvió `failing_program_from_logs()`. Si no se
    pudo identificar, se cae a una heurística por rango: abajo de 6000 solo
    puede ser un programa nativo (spl-token es el candidato realista en un
    trade), y de 6000 para arriba es un error Anchor de ALGÚN programa que
    no podemos atribuir -así que se dice explícitamente en vez de adivinar
    y mostrar el nombre equivocado.

    None si no hay nada mejor que decir que el código crudo.
    """
    if program_id is not None:
        table = ERROR_TABLES.get(program_id)
        if table is None:
            return f"error {code} del programa {program_id} (no tengo su tabla de errores)"
        name = table.get(code)
        label = PROGRAM_NAMES.get(program_id, program_id)
        if name is None:
            return f"error {code} del programa {label} (no está en su tabla de errores)"
        return f"{label} {name}"

    if code < ANCHOR_ERROR_CODE_OFFSET:
        name = _SPL_TOKEN_ERRORS.get(code)
        if name is not None:
            return f"probablemente spl-token {name}"
        return None

    return (f"error Anchor {code} de un programa que no pude identificar "
            f"(mirá los logs con -v o abrí el link de Solscan)")
