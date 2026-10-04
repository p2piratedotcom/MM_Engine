from __future__ import annotations

import argparse
import json
import sys
import time
from decimal import Decimal, InvalidOperation

from .config import Settings
from .mexc import MexcClient, MexcError


def _doctor(settings: Settings, *, json_output: bool) -> int:
    client = MexcClient(base_url=settings.mexc_base_url)
    result: dict[str, object] = {
        "pair": settings.pair,
        "mode": "LIVE" if settings.live_trading else "SIMULATION",
        "transfers": "ENABLED" if settings.live_transfers else "DISABLED",
    }
    try:
        check = client.check_symbol(settings.pair)
        result.update(
            {
                "mexc_reachable": True,
                "listed": check.listed,
                "spot_trading_allowed": check.spot_trading_allowed,
                "order_types": list(check.order_types),
                "problems": list(check.problems),
            }
        )
        if check.raw:
            detail_names = (
                "status",
                "baseAsset",
                "quoteAsset",
                "baseAssetPrecision",
                "quoteAssetPrecision",
                "baseSizePrecision",
                "quoteAmountPrecision",
                "quoteAmountPrecisionMarket",
                "maxQuoteAmount",
                "maxQuoteAmountMarket",
                "tradeSideType",
            )
            result["symbol_details"] = {
                name: check.raw[name] for name in detail_names if name in check.raw
            }
        exit_code = 0 if check.listed and check.spot_trading_allowed else 2
    except (MexcError, OSError, ValueError) as exc:
        result.update({"mexc_reachable": False, "problems": [str(exc)]})
        exit_code = 2

    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Modalita: {result['mode']} (trasferimenti: {result['transfers']})")
        print(f"Pair MEXC: {settings.pair}")
        print(f"MEXC raggiungibile: {'si' if result.get('mexc_reachable') else 'no'}")
        if result.get("mexc_reachable"):
            print(f"Spot API consentita: {'si' if result.get('spot_trading_allowed') else 'no'}")
            print("Tipi ordine: " + ", ".join(result.get("order_types", [])))
            if result.get("symbol_details"):
                print("Vincoli simbolo: " + json.dumps(result["symbol_details"], sort_keys=True))
        for problem in result.get("problems", []):
            print(f"ATTENZIONE: {problem}")
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kdf-mm")
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor = subparsers.add_parser(
        "doctor", help="verifica pubblica e non operativa del pair MEXC"
    )
    doctor.add_argument("--json", action="store_true", help="stampa JSON")
    subparsers.add_parser("agent", help="avvia il VPS Agent locale")
    subparsers.add_parser("plugin-capabilities", help="report the common plugin protocol without starting trading")
    subparsers.add_parser("exchange-plugin-host", help="internal isolated Spot plugin host")
    subparsers.add_parser("wallet-service", help="servizio locale per P2Pirate via stdin")
    local = subparsers.add_parser(
        "local-service",
        help="avvia Supervisor KDF, feed pubblico MEXC e API locale",
    )
    local.add_argument("--with-mexc", action="store_true", help="include il worker MEXC locale senza GUI; usa i permessi live configurati")
    local.add_argument("--mexc-profile", default="default")
    local.add_argument(
        "--config",
        default=None,
        help="configurazione privata MM2.json da gestire",
    )
    local.add_argument(
        "--start-kdf",
        action="store_true",
        help="avvia KDF insieme al servizio",
    )
    tui = subparsers.add_parser("tui", help="apre la TUI del VPS Agent")
    tui.add_argument("--url", default="http://127.0.0.1:8765", help="URL del VPS Agent")
    tui.add_argument("--mexc-profile", default="default", help="profilo credenziali MEXC nel portachiavi")
    tui.add_argument(
        "--desktop-journal",
        default=None,
        help="journal locale da mostrare nella pagina eventi e coperture",
    )
    tui.add_argument(
        "--monitor-only",
        action="store_true",
        help="apre il journal locale senza collegarsi al VPS Agent",
    )
    gui = subparsers.add_parser(
        "gui", help="apre il monitor grafico locale in sola lettura"
    )
    gui.add_argument("--url", default="http://127.0.0.1:8765", help="URL del VPS Agent")
    gui.add_argument(
        "--desktop-journal",
        default=None,
        help="journal locale degli eventi e delle coperture",
    )
    gui.add_argument(
        "--offline",
        action="store_true",
        help=(
            "non si collega al VPS Agent; MEXC resta in sola lettura salvo "
            "--without-mexc"
        ),
    )
    gui.add_argument(
        "--refresh-interval",
        type=float,
        default=3.0,
        help="secondi tra due aggiornamenti (minimo 0.5)",
    )
    gui.add_argument(
        "--mexc-profile",
        default="default",
        help="profilo MEXC nel portachiavi Zorin",
    )
    gui.add_argument(
        "--without-mexc",
        action="store_true",
        help="non legge i dati privati MEXC",
    )
    desktop = subparsers.add_parser(
        "desktop-agent",
        help="riceve e registra localmente gli eventi di copertura firmati",
    )
    desktop.add_argument("--url", default=None, help="URL privato del VPS Agent")
    desktop.add_argument("--journal", default=None, help="database locale del journal")
    desktop.add_argument(
        "--consumer-id", default=None, help="identita stabile di questo PC Zorin"
    )
    desktop.add_argument(
        "--poll-interval", type=float, default=None, help="secondi tra due letture"
    )
    desktop.add_argument(
        "--once", action="store_true", help="esegue una sola sincronizzazione"
    )
    desktop.add_argument(
        "--mexc-profile",
        default="default",
        help="profilo MEXC nel portachiavi Zorin",
    )
    desktop.add_argument(
        "--publish-coverage",
        action="store_true",
        help="invia alla VPS i saldi Spot firmati per il blocco preventivo",
    )
    desktop.add_argument(
        "--enable-live-hedging",
        action="store_true",
        help=(
            "abilita la copertura automatica reale; richiede anche "
            "KDF_MM_LIVE_TRADING=true"
        ),
    )
    keyring = subparsers.add_parser(
        "mexc-keyring", help="configura le credenziali MEXC nel portachiavi Zorin"
    )
    keyring.add_argument("action", choices=("set", "status"))
    keyring.add_argument("--profile", default="default")
    gate_keyring = subparsers.add_parser(
        "gate-keyring", help="configura le credenziali Gate nel portachiavi Zorin"
    )
    gate_keyring.add_argument("action", choices=("set", "status"))
    gate_keyring.add_argument("--profile", default="default")
    mexc_test = subparsers.add_parser(
        "mexc-test", help="valida una copertura senza inviarla al matching engine"
    )
    mexc_test.add_argument("--swap-uuid", required=True)
    mexc_test.add_argument("--journal", default=None)
    mexc_test.add_argument("--profile", default="default")
    mexc_fills = subparsers.add_parser(
        "mexc-import-fills",
        help="importa e riconcilia fill e commissioni di un ordine MEXC terminale",
    )
    mexc_fills.add_argument("--swap-uuid", required=True)
    mexc_fills.add_argument("--sequence", type=int, default=1)
    mexc_fills.add_argument("--journal", default=None)
    mexc_fills.add_argument("--profile", default="default")
    resolve = subparsers.add_parser(
        "hedge-resolve",
        help="registra la risoluzione manuale di una esposizione incerta",
    )
    resolve.add_argument("--swap-uuid", required=True)
    resolve.add_argument("--note", required=True)
    resolve.add_argument("--confirmation", required=True)
    resolve.add_argument("--journal", default=None)
    baseline = subparsers.add_parser(
        "inventory-baseline",
        help="registra costo e quantita iniziali dell'inventario base",
    )
    baseline.add_argument("--baseline-key", required=True)
    baseline.add_argument(
        "--asset", default=None, help="ticker; usa KDF_MM_BASE_TICKER se omesso"
    )
    baseline.add_argument("--quantity", required=True)
    baseline.add_argument("--total-cost-usdt", required=True)
    baseline.add_argument("--observed-at-ms", type=int, default=None)
    baseline.add_argument("--source", default="MANUAL")
    baseline.add_argument("--note", default=None)
    baseline.add_argument("--journal", default=None)
    adjustment = subparsers.add_parser(
        "inventory-adjustment",
        help="registra un ingresso o un'uscita dell'asset base",
    )
    adjustment.add_argument("--adjustment-key", required=True)
    adjustment.add_argument("--kind", choices=("ACQUIRE", "DISPOSE"), required=True)
    adjustment.add_argument(
        "--asset", default=None, help="ticker; usa KDF_MM_BASE_TICKER se omesso"
    )
    adjustment.add_argument("--quantity", required=True)
    adjustment.add_argument("--total-cost-usdt", default=None)
    adjustment.add_argument("--occurred-at-ms", type=int, default=None)
    adjustment.add_argument("--source", default="MANUAL")
    adjustment.add_argument("--note", default=None)
    adjustment.add_argument("--journal", default=None)
    binary = subparsers.add_parser("kdf-binary", help="verifica il binario KDF fissato")
    binary.add_argument("action", choices=("info", "verify", "probe"))
    binary.add_argument("--manifest", default=None)
    runtime = subparsers.add_parser("kdf-config", help="valida una configurazione KDF")
    runtime.add_argument("action", choices=("validate",))
    runtime.add_argument("--path", default=None)
    contract = subparsers.add_parser(
        "kdf-contract-test", help="avvia KDF isolato e verifica le RPC di base"
    )
    contract.add_argument("--manifest", default=None)
    contract.add_argument("--fixture", default="tests/fixtures/MM2.contract.json")
    contract.add_argument("--coins", default=None)
    contract.add_argument("--timeout", type=float, default=15.0)
    activation = subparsers.add_parser(
        "kdf-activation-test",
        help="attiva ARRR, BNB, USDT-BEP20 e LTC con un wallet pubblico isolato",
    )
    activation.add_argument("--manifest", default=None)
    activation.add_argument("--fixture", default="tests/fixtures/MM2.contract.json")
    activation.add_argument("--coins", default=None)
    activation.add_argument("--coins-manifest", default=None)
    activation.add_argument("--timeout", type=float, default=900.0)
    activation.add_argument("--poll-interval", type=float, default=2.0)
    funded = subparsers.add_parser(
        "kdf-funded-swap-test",
        help="esegue lo swap ARRR/USDT-BEP20 minimo tra due wallet finanziati",
    )
    funded.add_argument("--maker-config", required=True)
    funded.add_argument("--taker-config", required=True)
    funded.add_argument("--maker-addresses", required=True)
    funded.add_argument("--taker-addresses", required=True)
    funded.add_argument("--run-dir", required=True)
    funded.add_argument("--arrr-sync-height", required=True, type=int)
    funded.add_argument("--volume", default="0.1")
    funded.add_argument("--price", default="0.25")
    funded.add_argument("--activation-timeout", type=float, default=900.0)
    funded.add_argument("--swap-timeout", type=float, default=10800.0)
    funded.add_argument("--poll-interval", type=float, default=10.0)
    funded.add_argument(
        "--scenario",
        choices=(
            "normal",
            "restart_maker",
            "recover_taker_db_loss",
            "refund_maker_payment",
        ),
        default="normal",
        help=(
            "scenario normale, riavvio Maker, recupero Taker senza database "
            "o rimborso del pagamento Maker"
        ),
    )
    funded.add_argument("--manifest", default=None)
    funded.add_argument("--coins", default=None)
    funded.add_argument("--coins-manifest", default=None)
    multi_funded = subparsers.add_parser(
        "kdf-funded-multi-pair-test",
        help="collauda OCO e gara ARRR/USDT-BEP20 + ARRR/LTC con fondi reali",
    )
    multi_funded.add_argument("--maker-config", required=True)
    multi_funded.add_argument("--taker-config", required=True)
    multi_funded.add_argument("--maker-addresses", required=True)
    multi_funded.add_argument("--taker-addresses", required=True)
    multi_funded.add_argument("--run-dir", required=True)
    multi_funded.add_argument("--arrr-sync-height", required=True, type=int)
    multi_funded.add_argument("--volume", default="0.1")
    multi_funded.add_argument("--usdt-price", default="0.25")
    multi_funded.add_argument("--ltc-price", default="0.006")
    multi_funded.add_argument("--activation-timeout", type=float, default=1800.0)
    multi_funded.add_argument("--swap-timeout", type=float, default=10800.0)
    multi_funded.add_argument("--poll-interval", type=float, default=5.0)
    multi_funded.add_argument(
        "--mode", choices=("full", "race-only"), default="full"
    )
    multi_funded.add_argument("--manifest", default=None)
    multi_funded.add_argument("--coins", default=None)
    multi_funded.add_argument("--coins-manifest", default=None)
    wallet = subparsers.add_parser(
        "wallet-activate",
        help="attiva ARRR, BNB, USDT-BEP20 e LTC e salva gli indirizzi pubblici",
    )
    wallet.add_argument("--config", required=True, help="percorso del file MM2.json")
    wallet.add_argument(
        "--output", required=True, help="file JSON in cui salvare indirizzi e saldi"
    )
    wallet.add_argument("--manifest", default=None, help="manifest del coin registry")
    wallet.add_argument("--timeout", type=float, default=1800.0)
    wallet.add_argument("--poll-interval", type=float, default=5.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "plugin-capabilities":
        print(json.dumps(dict(protocol=1, transport="stdio", wallet_protocol=1)))
        return 0
    if args.command == "exchange-plugin-host":
        from .exchange_plugin_host import main as plugin_main
        return plugin_main()
    if args.command == "wallet-service":
        from .wallet_service import main as wallet_main

        return wallet_main()
    settings = Settings.from_env()
    if args.command == "doctor":
        return _doctor(settings, json_output=args.json)
    if args.command == "agent":
        from .vps_agent import serve

        serve(settings)
        return 0
    if args.command == "local-service":
        from .vps_agent import local_settings, serve

        selected = local_settings(
            settings,
            args.config or settings.kdf_config_path,
        )
        serve(selected, start_kdf=args.start_kdf, with_mexc=args.with_mexc, mexc_profile=args.mexc_profile)
        return 0
    if args.command == "tui":
        from .tui import run_tui

        try:
            run_tui(
                agent_url=args.url,
                token=settings.agent_token,
                desktop_journal=(
                    args.desktop_journal or settings.desktop_journal_db
                ),
                monitor_only=args.monitor_only,
                mexc_profile=args.mexc_profile,
            )
            return 0
        except ValueError as exc:
            print(f"ERRORE TUI: {exc}", file=sys.stderr)
            return 2
    if args.command == "gui":
        from .gui import DesktopGuiUnavailable, run_gui
        from .portfolio import PortfolioPolicy

        try:
            return run_gui(
                agent_url=args.url,
                token=settings.agent_token,
                desktop_journal=(
                    args.desktop_journal or settings.desktop_journal_db
                ),
                offline=args.offline,
                refresh_interval=args.refresh_interval,
                mexc_enabled=not args.without_mexc,
                mexc_profile=args.mexc_profile,
                mexc_base_url=settings.mexc_base_url,
                mexc_symbol=settings.pair,
                mexc_base_asset=settings.mexc_base_asset,
                mexc_quote_asset=settings.mexc_quote_asset,
                base_ticker=settings.base_ticker,
                portfolio_policy=PortfolioPolicy(
                    kdf_arrr_target_fraction=(
                        settings.portfolio_kdf_arrr_target_fraction
                    ),
                    kdf_usdt_target_fraction=(
                        settings.portfolio_kdf_usdt_target_fraction
                    ),
                    rebalance_minimum_usdt=(
                        settings.portfolio_rebalance_minimum_usdt
                    ),
                    cex_taker_fee=settings.cex_taker_fee,
                ),
            )
        except (DesktopGuiUnavailable, ValueError) as exc:
            print(f"ERRORE GUI: {exc}", file=sys.stderr)
            return 2
    if args.command == "desktop-agent":
        from .local_worker import WorkerLock, coordinated_cycle
        from .auto_hedge import AutomaticHedgeEngine
        from .credentials import LinuxSecretService, SecretServiceError
        from .gate import GateClient
        from .desktop_agent import DesktopAgent, DesktopAgentError, VpsEventClient
        from .desktop_coverage import DesktopCoveragePublisher
        from .desktop_runtime import DesktopRuntime
        from .journal import HedgeJournal, JournalConflict

        publish_coverage = bool(
            args.publish_coverage
            or args.enable_live_hedging
            or settings.mexc_coverage
            or settings.auto_hedge
        )
        live_hedging = bool(args.enable_live_hedging or settings.auto_hedge)
        if live_hedging and not settings.live_trading:
            print(
                "ERRORE Desktop Agent: la copertura reale richiede anche "
                "KDF_MM_LIVE_TRADING=true",
                file=sys.stderr,
            )
            return 2
        try:
            credentials = (
                LinuxSecretService(profile=args.mexc_profile).load_mexc()
                if publish_coverage
                else None
            )
            try:
                gate_credentials = LinuxSecretService(profile=args.mexc_profile).load_gate() if publish_coverage else None
            except SecretServiceError:
                gate_credentials = None
            journal_path = args.journal or settings.desktop_journal_db
            with WorkerLock(journal_path, cooperative=True), HedgeJournal(journal_path) as journal:
                events = VpsEventClient(
                    base_url=args.url or settings.desktop_agent_url,
                    token=settings.agent_token,
                )
                desktop = DesktopAgent(
                    journal=journal,
                    events=events,
                    event_secret=settings.event_secret,
                    consumer_id=args.consumer_id or settings.desktop_consumer_id,
                )
                mexc = (
                    MexcClient(
                        api_key=credentials.api_key,
                        api_secret=credentials.api_secret,
                        base_url=settings.mexc_base_url,
                        trading_enabled=live_hedging,
                        transfers_enabled=False,
                    )
                    if credentials is not None
                    else None
                )
                clients = {"MEXC": mexc} if mexc is not None else {}
                if gate_credentials is not None:
                    clients["GATE"] = GateClient(
                        api_key=gate_credentials.api_key,
                        api_secret=gate_credentials.api_secret,
                        base_url=settings.gate_base_url,
                        trading_enabled=live_hedging,
                    )
                coverage = (
                    DesktopCoveragePublisher(
                        mexc=mexc,
                        clients=clients,
                        vps=events,
                        event_secret=settings.event_secret,
                        consumer_id=args.consumer_id or settings.desktop_consumer_id,
                        assets=(settings.mexc_base_asset, settings.mexc_quote_asset),
                        ttl_seconds=settings.coverage_lease_ttl_seconds,
                        live_hedging_enabled=live_hedging,
                        include_all_spot_assets=True,
                    )
                    if mexc is not None
                    else None
                )
                hedging = (
                    AutomaticHedgeEngine(
                        journal=journal,
                        mexc=mexc,
                        clients=clients,
                        venue_fees={"MEXC": settings.cex_taker_fee,
                                    "GATE": settings.gate_taker_fee},
                        max_slippage=settings.max_slippage,
                        fee_buffer=settings.cex_taker_fee,
                        depth_limit=settings.mexc_depth_limit,
                        max_attempts=settings.auto_hedge_max_attempts,
                    )
                    if live_hedging and mexc is not None
                    else None
                )
                runtime = DesktopRuntime(
                    desktop=desktop,
                    coverage=coverage,
                    hedging=hedging,
                )
                cycle = runtime.run_once
                runtime.run_once = lambda: coordinated_cycle(cycle, journal_path)
                if args.once:
                    result = runtime.run_once()
                    print(json.dumps(result.payload(), indent=2, sort_keys=True))
                    return 2 if result.errors else 0
                if live_hedging:
                    print(
                        "ATTENZIONE: COPERTURA AUTOMATICA MEXC REALE ATTIVA; "
                        "trasferimenti disabilitati"
                    )
                elif coverage is not None:
                    print(
                        "Desktop Agent in ascolto; controllo fondi MEXC attivo, "
                        "ordini MEXC reali disabilitati"
                    )
                else:
                    print(
                        "Desktop Agent in ascolto; controllo fondi e ordini MEXC "
                        "reali disabilitati"
                    )
                runtime.run_forever(
                    poll_interval=(
                        args.poll_interval
                        if args.poll_interval is not None
                        else settings.desktop_poll_interval_seconds
                    )
                )
        except (
            SecretServiceError,
            DesktopAgentError,
            JournalConflict,
            MexcError,
            ValueError,
        ) as exc:
            print(f"ERRORE Desktop Agent: {exc}", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            print("Desktop Agent arrestato")
        return 0
    if args.command == "mexc-keyring":
        import getpass

        from .credentials import (
            LinuxSecretService,
            MexcCredentials,
            SecretServiceError,
        )

        try:
            keyring = LinuxSecretService(profile=args.profile)
            if args.action == "status":
                keyring.load_mexc()
                print(f"Credenziali MEXC presenti nel profilo {args.profile}")
                return 0
            api_key = getpass.getpass("MEXC API key: ")
            api_secret = getpass.getpass("MEXC API secret: ")
            keyring.store_mexc(
                MexcCredentials(api_key=api_key, api_secret=api_secret)
            )
            print(f"Credenziali MEXC salvate nel profilo {args.profile}")
            return 0
        except (SecretServiceError, ValueError) as exc:
            print(f"ERRORE portachiavi: {exc}", file=sys.stderr)
            return 2
    if args.command == "gate-keyring":
        import getpass

        from .credentials import GateCredentials, LinuxSecretService, SecretServiceError

        try:
            keyring = LinuxSecretService(profile=args.profile)
            if args.action == "status":
                keyring.load_gate()
                print(f"Credenziali Gate presenti nel profilo {args.profile}")
                return 0
            api_key = getpass.getpass("Gate API key: ")
            api_secret = getpass.getpass("Gate API secret: ")
            keyring.store_gate(GateCredentials(api_key=api_key, api_secret=api_secret))
            print(f"Credenziali Gate salvate nel profilo {args.profile}")
            return 0
        except (SecretServiceError, ValueError) as exc:
            print(f"ERRORE portachiavi: {exc}", file=sys.stderr)
            return 2
    if args.command == "mexc-test":
        from .credentials import LinuxSecretService, SecretServiceError
        from .journal import HedgeJournal, JournalConflict
        from .mexc_test_connector import MexcTestConnector, MexcTestConnectorError

        try:
            credentials = LinuxSecretService(profile=args.profile).load_mexc()
            with HedgeJournal(args.journal or settings.desktop_journal_db) as journal:
                connector = MexcTestConnector(
                    journal=journal,
                    mexc=MexcClient(
                        api_key=credentials.api_key,
                        api_secret=credentials.api_secret,
                        base_url=settings.mexc_base_url,
                        trading_enabled=False,
                        transfers_enabled=False,
                    ),
                    max_slippage=settings.max_slippage,
                    fee_buffer=settings.cex_taker_fee,
                    depth_limit=settings.mexc_depth_limit,
                )
                result = connector.validate(args.swap_uuid)
            print(json.dumps(result.payload(), indent=2, sort_keys=True))
            return 0
        except (
            SecretServiceError,
            MexcError,
            MexcTestConnectorError,
            JournalConflict,
            ValueError,
        ) as exc:
            print(f"ERRORE test MEXC: {exc}", file=sys.stderr)
            return 2
    if args.command == "mexc-import-fills":
        from .credentials import LinuxSecretService, SecretServiceError
        from .journal import HedgeJournal, JournalConflict
        from .mexc_fill_importer import MexcFillImporter, MexcFillImportError

        try:
            credentials = LinuxSecretService(profile=args.profile).load_mexc()
            with HedgeJournal(args.journal or settings.desktop_journal_db) as journal:
                result = MexcFillImporter(
                    journal=journal,
                    mexc=MexcClient(
                        api_key=credentials.api_key,
                        api_secret=credentials.api_secret,
                        base_url=settings.mexc_base_url,
                        trading_enabled=False,
                        transfers_enabled=False,
                    ),
                ).import_swap(args.swap_uuid, sequence=args.sequence)
            print(json.dumps(result.payload(), indent=2, sort_keys=True))
            return 0
        except (
            SecretServiceError,
            MexcError,
            MexcFillImportError,
            JournalConflict,
            ValueError,
        ) as exc:
            print(f"ERRORE importazione fill MEXC: {exc}", file=sys.stderr)
            return 2
    if args.command == "hedge-resolve":
        from .journal import HedgeJournal, JournalConflict

        if args.confirmation != "ESPOSIZIONE RISOLTA":
            print(
                'ERRORE: usare --confirmation "ESPOSIZIONE RISOLTA" solo dopo '
                "aver verificato e corretto manualmente la posizione MEXC",
                file=sys.stderr,
            )
            return 2
        try:
            with HedgeJournal(args.journal or settings.desktop_journal_db) as journal:
                resolved = journal.resolve_attention(
                    args.swap_uuid,
                    note=args.note,
                )
            print(
                json.dumps(
                    {
                        "swap_uuid": resolved.swap_uuid,
                        "state": resolved.state.value,
                        "note": resolved.last_error,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        except (JournalConflict, KeyError, ValueError) as exc:
            print(f"ERRORE risoluzione copertura: {exc}", file=sys.stderr)
            return 2
    if args.command == "inventory-baseline":
        from .journal import HedgeJournal, JournalConflict

        try:
            with HedgeJournal(args.journal or settings.desktop_journal_db) as journal:
                baseline = journal.record_inventory_baseline(
                    baseline_key=args.baseline_key,
                    asset=args.asset or settings.base_ticker,
                    quantity=Decimal(args.quantity),
                    total_cost_usdt=Decimal(args.total_cost_usdt),
                    observed_at_ms=(
                        args.observed_at_ms
                        if args.observed_at_ms is not None
                        else time.time_ns() // 1_000_000
                    ),
                    source=args.source,
                    note=args.note,
                )
            payload = {
                "baseline_key": baseline.baseline_key,
                "asset": baseline.asset,
                "quantity": str(baseline.quantity),
                "total_cost_usdt": str(baseline.total_cost_usdt),
                "observed_at_ms": baseline.observed_at_ms,
                "source": baseline.source,
                "note": baseline.note,
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        except (InvalidOperation, JournalConflict, ValueError) as exc:
            print(f"ERRORE baseline inventario: {exc}", file=sys.stderr)
            return 2
    if args.command == "inventory-adjustment":
        from .journal import (
            HedgeJournal,
            InventoryAdjustmentKind,
            JournalConflict,
        )

        try:
            with HedgeJournal(args.journal or settings.desktop_journal_db) as journal:
                adjustment = journal.record_inventory_adjustment(
                    adjustment_key=args.adjustment_key,
                    asset=args.asset or settings.base_ticker,
                    kind=InventoryAdjustmentKind(args.kind),
                    quantity=Decimal(args.quantity),
                    total_cost_usdt=(
                        Decimal(args.total_cost_usdt)
                        if args.total_cost_usdt is not None
                        else None
                    ),
                    occurred_at_ms=(
                        args.occurred_at_ms
                        if args.occurred_at_ms is not None
                        else time.time_ns() // 1_000_000
                    ),
                    source=args.source,
                    note=args.note,
                )
            payload = {
                "adjustment_key": adjustment.adjustment_key,
                "asset": adjustment.asset,
                "kind": adjustment.kind.value,
                "quantity": str(adjustment.quantity),
                "total_cost_usdt": (
                    str(adjustment.total_cost_usdt)
                    if adjustment.total_cost_usdt is not None
                    else None
                ),
                "occurred_at_ms": adjustment.occurred_at_ms,
                "source": adjustment.source,
                "note": adjustment.note,
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        except (InvalidOperation, JournalConflict, ValueError) as exc:
            print(f"ERRORE movimento inventario: {exc}", file=sys.stderr)
            return 2
    if args.command == "kdf-binary":
        from dataclasses import asdict

        from .kdf_runtime import KdfBinaryManager

        manager = KdfBinaryManager(args.manifest or settings.kdf_binary_manifest)
        if args.action == "info":
            print(json.dumps(manager.manifest, indent=2, sort_keys=True))
        elif args.action == "verify":
            print(json.dumps(asdict(manager.verify()), indent=2, sort_keys=True))
        else:
            print(manager.probe_banner())
        return 0
    if args.command == "kdf-config":
        from .kdf_runtime import validate_runtime_config

        problems = validate_runtime_config(args.path or settings.kdf_config_path)
        if problems:
            for problem in problems:
                print(f"ERRORE: {problem}")
            return 2
        print("Configurazione KDF valida")
        return 0
    if args.command == "kdf-contract-test":
        from dataclasses import asdict

        from .kdf_runtime import run_kdf_contract_test

        result = run_kdf_contract_test(
            manifest_path=args.manifest or settings.kdf_binary_manifest,
            fixture_path=args.fixture,
            coins_path=args.coins or settings.kdf_coins_path,
            timeout=args.timeout,
        )
        print(json.dumps(asdict(result), indent=2, sort_keys=True))
        return 0
    if args.command == "kdf-activation-test":
        from dataclasses import asdict

        from .kdf_runtime import run_kdf_activation_smoke_test

        result = run_kdf_activation_smoke_test(
            manifest_path=args.manifest or settings.kdf_binary_manifest,
            fixture_path=args.fixture,
            coins_manifest_path=args.coins_manifest or settings.kdf_coins_manifest,
            coins_path=args.coins or settings.kdf_coins_path,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
        )
        print(json.dumps(asdict(result), indent=2, sort_keys=True))
        return 0
    if args.command == "kdf-funded-swap-test":
        from dataclasses import asdict

        from .funded_swap_test import run_funded_swap_test

        result = run_funded_swap_test(
            binary_manifest_path=args.manifest or settings.kdf_binary_manifest,
            coins_manifest_path=args.coins_manifest or settings.kdf_coins_manifest,
            coins_path=args.coins or settings.kdf_coins_path,
            maker_source_config=args.maker_config,
            taker_source_config=args.taker_config,
            maker_expected_addresses=args.maker_addresses,
            taker_expected_addresses=args.taker_addresses,
            run_dir=args.run_dir,
            arrr_sync_height=args.arrr_sync_height,
            volume=Decimal(args.volume),
            price=Decimal(args.price),
            activation_timeout=args.activation_timeout,
            swap_timeout=args.swap_timeout,
            poll_interval=args.poll_interval,
            scenario=args.scenario,
        )
        print(json.dumps(asdict(result), indent=2, sort_keys=True))
        return 0
    if args.command == "kdf-funded-multi-pair-test":
        from dataclasses import asdict

        from .multi_pair_funded_test import run_multi_pair_funded_test

        result = run_multi_pair_funded_test(
            binary_manifest_path=args.manifest or settings.kdf_binary_manifest,
            coins_manifest_path=args.coins_manifest or settings.kdf_coins_manifest,
            coins_path=args.coins or settings.kdf_coins_path,
            maker_source_config=args.maker_config,
            taker_source_config=args.taker_config,
            maker_expected_addresses=args.maker_addresses,
            taker_expected_addresses=args.taker_addresses,
            run_dir=args.run_dir,
            arrr_sync_height=args.arrr_sync_height,
            volume=Decimal(args.volume),
            usdt_price=Decimal(args.usdt_price),
            ltc_price=Decimal(args.ltc_price),
            activation_timeout=args.activation_timeout,
            swap_timeout=args.swap_timeout,
            poll_interval=args.poll_interval,
            mode=args.mode,
        )
        print(json.dumps(asdict(result), indent=2, sort_keys=True))
        return 0
    if args.command == "wallet-activate":
        from .wallet_activation import activate_wallet_from_config

        result = activate_wallet_from_config(
            config_path=args.config,
            manifest_path=args.manifest or settings.kdf_coins_manifest,
            output_path=args.output,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
            progress=lambda message: print(message, file=sys.stderr),
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        print(f"Indirizzi salvati in {args.output}", file=sys.stderr)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
