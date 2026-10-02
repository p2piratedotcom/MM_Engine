"""Readable strategy wizard. One input owner; all HTTP work off the UI thread."""
from __future__ import annotations

import curses
import time
import uuid
import textwrap
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation

from .models import DexSide
from .strategy import AssetRoute, StrategySpec


def automatic_asset(ticker):
    # Explicit aliases only: never strip an arbitrary network suffix.
    aliases = {"USDT-BEP20": "USDT", "USDT-ERC20": "USDT",
               "USDC-BEP20": "USDC", "USDC-ERC20": "USDC"}
    return aliases.get(ticker) or (ticker if ticker and "-" not in ticker else None)


def delete_targets(rows, selected):
    """Freeze exact identities; never infer a new target after a refresh."""
    row = next(r for r in rows if r["id"] == selected)
    spec = StrategySpec.from_payload(row["spec"])
    result = [selected]
    for other in rows:
        candidate = StrategySpec.from_payload(other["spec"])
        if ((candidate.sold.ticker, candidate.bought.ticker) == (spec.bought.ticker, spec.sold.ticker)
                and candidate.premium == -spec.premium):
            result.append(other["id"])
            break
    return result


def choices_for(key, values, active):
    if key in {"sold", "bought"}:
        return [t for t in active if key == "sold" or t != values.get("sold")]
    return {"cex": ["MEXC", "GATE"],
            "price_mode": ["auto", "fixed"], "quantity_mode": ["max auto", "custom auto", "fixed"],
            "max_mode": ["auto", "fixed"], "budget_mode": ["tutto", "50%", "25%", "personalizza"],
            "replenish": ["no", "si"], "opposite": ["no", "si", "personalizza"]}.get(key)


def validate_field(key, text):
    value = text.strip()
    if key == 'scale_quantity':
        try:
            amount = Decimal(value.replace(',', '.'))
        except InvalidOperation:
            raise ValueError('Inserisci una quantità oppure 0 per copiare quella pubblicata') from None
        if not amount.is_finite() or amount < 0:
            raise ValueError('Inserisci una quantità positiva oppure 0 per la stessa quantità pubblicata')
        return str(amount)
    if key in {"premium", "opposite_premium", "opposite_quantity", "fixed_price", "max_sold", "fixed_sold", "budget", "daily",
               "quantity_threshold", "update_seconds", "auto_percent"}:
        try:
            number = Decimal(value.replace(",", "."))
        except InvalidOperation:
            raise ValueError("Inserisci un numero valido in questo campo") from None
        if not number.is_finite() or (key not in {"premium", "opposite_premium"} and number <= 0):
            raise ValueError("Inserisci un numero finito" if key == "premium" else "Inserisci un numero maggiore di zero")
        if key == 'auto_percent' and number > 100:
            raise ValueError("La percentuale deve essere maggiore di 0 e al massimo 100")
        return str(number)
    if not value:
        raise ValueError("Seleziona o inserisci un valore prima di proseguire")
    return value


def fields(values, *, opposite=False):
    result = [] if opposite else [
        ("cex", "CEX da usare per prezzo, fondi e copertura", "MEXC"),
        ("sold", "Coin da vendere su KDF", ""),
        ("bought", "Coin da comprare su KDF", ""),
    ]
    if not opposite:
        for side in ("sold", "bought"):
            if values.get(side) and not automatic_asset(values[side]):
                result.append((side + "_asset", f"Corrispondenza {values.get('cex', 'MEXC')} non nota per {values[side]}: asset Spot", ""))
    result += [("premium", "Premium in percentuale (es. 3 oppure -3)", "3"),
               ("price_mode", "Prezzo", "auto")]
    if values.get("price_mode") == "fixed":
        result.append(("fixed_price", "Prezzo FINALE quote/base (mostrati sopra); premium NON aggiunto", ""))
    result += [("quantity_mode", "Quantità", "max auto")]
    if values.get('quantity_mode') == 'custom auto':
        result.append(('auto_percent', 'Percentuale del massimo copribile residuo (0 < % <= 100)', '50'))
    if values.get("quantity_mode") == "fixed":
        result.append(("fixed_sold", "Quantità fissa in coin VENDUTA (nessuna riduzione silenziosa)", ""))
    result.append(("max_mode", "Quantità massima da vendere per volta", "auto"))
    if values.get("max_mode") == "fixed":
        result.append(("max_sold", "Quantità massima da vendere per volta — limite manuale", ""))
    result.append(("replenish", "Rifornire dopo swap e copertura conclusi?", "no"))
    if values.get("replenish", "no") == "no":
        result.append(("budget_mode", "Quantità totale da vendere prima di fermarsi", "tutto"))
        if values.get("budget_mode") == "personalizza":
            result.append(("budget", "Quantità totale da vendere — personalizzata", ""))
    result += [("update_seconds", "Intervallo minimo aggiornamenti ordinari (secondi)", "60")]
    if not opposite:
        result.append(("opposite", "Mercato opposto?", "no"))
    return result


def budget_amount(values, balances):
    if values.get("replenish") == "si":
        return Decimal(1)  # unused when replenishing; not a per-order cap
    mode = values.get("budget_mode", "personalizza")
    if mode == "personalizza":
        return Decimal(validate_field("budget", values.get("budget", "")))
    row = balances.get(values["sold"], {})
    if not row.get("available"):
        raise ValueError("Saldo KDF non disponibile: riprova prima di scegliere la percentuale")
    amount = Decimal(row["balance"]) * {"tutto": Decimal(1), "50%": Decimal(".5"), "25%": Decimal(".25")}[mode]
    if amount <= 0:
        raise ValueError("Saldo KDF nullo: nessuna quantità da vendere")
    return amount


def draft_spec(values, *, original=None, strategy_id=None):
    if original:
        base, quote, side = original.base, original.quote, original.side
    else:
        sold = AssetRoute.parse(values["sold"], automatic_asset(values["sold"]) or values.get("sold_asset") or None)
        bought = AssetRoute.parse(values["bought"], automatic_asset(values["bought"]) or values.get("bought_asset") or None)
        if sold.asset == "USDT":
            base, quote, side = bought, sold, DexSide.BUY_ARRR
        else:
            base, quote, side = sold, bought, DexSide.SELL_ARRR
    if values["replenish"] not in {"si", "no"} or values.get("opposite", "no") not in {"si", "no", "personalizza"}:
        raise ValueError("rispondere si/no (o personalizza per il mercato opposto)")
    return StrategySpec(
        strategy_id or (original.strategy_id if original else uuid.uuid4().hex[:12]), base, quote, side,
        Decimal(values["premium"]) / 100, values["price_mode"],
        Decimal(values["fixed_price"]) if values["price_mode"] == "fixed" else None,
        'fixed' if values["quantity_mode"] == 'fixed' else 'auto', Decimal(values["max_sold"]) if values.get("max_mode", "fixed") == "fixed" else None, values["replenish"] == "si",
        Decimal(values.get("budget") or "1"), None,
        Decimal(values["fixed_sold"]) if values["quantity_mode"] == "fixed" else None,
        quantity_threshold=Decimal(0),
        update_seconds=Decimal(values["update_seconds"]),
        scale_group=original.scale_group if original else "",
        auto_fraction=Decimal(validate_field('auto_percent', values.get('auto_percent', '50'))) / 100
                      if values['quantity_mode'] == 'custom auto' else Decimal(1),
        cex=original.cex if original else values.get("cex", "MEXC"),
    )


def capacity_spec(values, *, original=None):
    draft = {**values, 'replenish': 'si', 'budget': '1', 'max_mode': 'auto',
             'update_seconds': values.get('update_seconds', '60'), 'opposite': 'no'}
    try:
        fixed = Decimal(draft.get('fixed_sold', '0'))
        fixed = fixed if fixed.is_finite() and fixed > 0 else None
    except InvalidOperation:
        fixed = None
    draft['quantity_mode'] = ('fixed' if fixed is not None and values.get('quantity_mode') == 'fixed'
                              else 'custom auto' if values.get('quantity_mode') == 'custom auto' else 'max auto')
    return draft_spec(draft, original=original, strategy_id=original.strategy_id if original else 'capacity-preview').payload()


def capacity_lines(report):
    from .strategy import limit_description
    def compact(v): return f'{Decimal(v):.8g}'
    cex = report.get('cex', 'MEXC')
    lines = [f"Massimo fattibile ora: {report['feasible_maximum']} {report['coin']}",
             f"Minimo {cex}: {report['minimum']} | limite superiore: {report['maximum']} {report['coin']}"]
    if Decimal(report['feasible_maximum']) == 0:
        lines.append('Nessuna quantità pubblicabile con questi saldi e questo book.')
    lines.append(f"Copertura per {compact(report['quantity_for_funds'])} {report['coin']} (valori arrotondati):")
    for r in report['funds']:
        lines.append(f"{r['asset']}: mancano {compact(r['missing'])}; richiesti {compact(r['required'])}, netti {compact(r['available'])}")
    lines.append('Limite: ' + ', '.join(limit_description(k) for k in report['limiting']))
    lines.append('Stima corrente: ricontrollata al salvataggio e alla pubblicazione.')
    return lines


def scale_fields(values):
    result = [("scale_quantity", "Quantità del NUOVO livello (0 = stessa quantità pubblicata)", "0"),
              ("premium", "Premium del nuovo livello (%)", ""),
              ("opposite", "Crea anche il livello speculare?", "no")]
    if values.get("opposite") == "personalizza":
        result += [("opposite_quantity", "Quantità speculare nella coin venduta opposta", ""),
                   ("opposite_premium", "Premium speculare (%)", "")]
    return result


def values_for(spec):
    return {"cex": spec.cex, "sold": spec.sold.ticker, "bought": spec.bought.ticker, "sold_asset": spec.sold.asset,
            "bought_asset": spec.bought.asset, "premium": str(spec.premium * 100), "price_mode": spec.price_mode,
            "fixed_price": str(spec.fixed_price or ""),
            "quantity_mode": 'fixed' if spec.quantity_mode == 'fixed' else 'custom auto' if spec.auto_fraction < 1 else 'max auto',
            "auto_percent": str(spec.auto_fraction * 100),
            "max_mode": "auto" if spec.max_sold is None else "fixed",
            "max_sold": str(spec.max_sold or ""), "fixed_sold": str(spec.fixed_sold or ""),
            "budget_mode": "personalizza",
            "replenish": "si" if spec.replenish else "no", "budget": str(spec.total_sold_budget),
            "daily": str(spec.daily_sold_cap), "quantity_threshold": str(spec.quantity_threshold * 100),
            "update_seconds": str(spec.update_seconds), "opposite": "no"}


def preview_lines(payload):
    lines = ["ANTEPRIMA — nessun ordine pubblicato. Importi in USDT, non USD garantiti."]
    for preview in payload["previews"]:
        p = preview["plan"]
        lines += ["", f"Vendi {p['kdf_volume']} {p['kdf_base']} per {p['kdf_rel']}",
                  f"Quantità: {preview.get('quantity_policy', 'n/d')}",
                  f"Prezzo: {p['human_price_usdt_per_arrr']} {preview['price_unit']}",
                  f"Premium: {Decimal(p['configured_premium']) * 100}%  fee: {Decimal(p['cex_taker_fee']) * 100}%  buffer: {Decimal(p['risk_buffer']) * 100}%",
                  "Tetti alla quantità VENDUTA:"]
        valuation = preview.get("valuations_usdt", {})
        if valuation:
            lines.insert(len(lines) - 1, f"Valore indicativo: venduto {valuation['totale_venduto']} USDT; ricevuto {valuation['totale_ricevuto']} USDT")
        lines += [f"  {name}: {amount} {p['kdf_base']}" for name, amount in preview["caps_sold"].items()]
        lines += [f"{leg.get('cex', 'MEXC')}: {leg['side']} {leg['quantity']} {leg['asset']} — {leg['symbol']}" for leg in preview["hedge_legs"]]
        lines += ["Profondità entro 1%; utilizzata al massimo per metà. Snapshot, non garanzia."]
    if payload.get("scale"):
        lines += ["", "PUBBLICAZIONE LIVE di quantità aggiuntive; non sostituisce l'ordine originale."]
        lines += [f"Riduzione auto {r['strategy_id']}: {r['from']} → {r['to']} (nuovo massimo)" for r in payload.get("reductions", [])]
        lines += [payload.get("notice", "")]
        return lines
    lines += ["", "Salvataggio in PAUSA. Avvio live separato e confermato.",
              "I due lati condividono i fondi: non sono due promesse indipendenti."]
    return lines


def scale_recovery_lines(request, payload):
    """Match durable identities, never infer success from a similar premium."""
    expected = ['scala-' + request['request_id']]
    if request.get('opposite', 'no') != 'no':
        expected.append(expected[0] + '-opposto')
    found = {r['id']: r for r in payload.get('strategies', []) if r['id'] in expected}
    lines = ['VERIFICA RICHIESTA SCALA', '', 'Nessun nuovo ordine viene inviato.', '']
    resolved = len(found) == len(expected) and all(r.get('state') in {
        'RUNNING', 'STABILIZING', 'WAITING', 'PAUSED', 'REVIEW_REQUIRED', 'EXHAUSTED', 'PREVIEW_ONLY', 'DELETED'
    } for r in found.values())
    lines += ['RICHIESTA REGISTRATA — controllare lo stato dei livelli' if resolved
              else 'ESITO ANCORA DA VERIFICARE — non ripetere Scala', '']
    for sid in expected:
        row = found.get(sid)
        lines += [sid, '  Stato: ' + str(row.get('state', 'SCONOSCIUTO')) if row else
                  '  Non ancora presente nella risposta; non significa che l’invio sia fallito.']
        if row and row.get('detail'):
            lines.append('  ' + str(row['detail']))
        lines.append('')
    lines += ['Registrata non significa necessariamente pubblicata: controllare anche I miei ordini KDF.']
    return lines, next(iter(found), None), resolved


def run_strategy_page(screen, api, theme):
    from .tui import _write, _footer, _normalize_key, _strategy_repricing_label
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="strategy-ui")
    future = None
    tag = ""
    mode = "list"
    rows = []
    worker_running = None
    selected = None
    message = ""
    values, specs = {}, []
    original = None
    step = 0
    text = ""
    scroll = 0
    preview = {}
    last_refresh = 0.0
    write_pending = False
    editing = False
    active = []
    balances = {}
    error_return = "list"
    action = 1  # default to Modify/Back, never an accidental live start
    deleting = False
    deletion_ids = []
    scaling = False
    scale_request = {}
    recovery_request = getattr(api, '_pending_scale_request', None)
    recovery_lines = ['ESITO SCALA DA VERIFICARE', 'Lettura dello stato; nessun reinvio.']
    capacity_report, capacity_error, capacity_key, capacity_at = None, '', None, 0.0
    last_input_at = 0.0

    def current_fields():
        return scale_fields(values) if scaling else fields(values, opposite=original is not None)

    def confirmation_labels():
        if deleting:
            return ("Solo questa", "Entrambe", "Indietro") if len(deletion_ids) == 2 else ("Elimina", "Indietro")
        return ("Avvia", "Indietro")

    def actions(y, labels):
        x = 2
        for index, label in enumerate(labels):
            caption = f"{'>' if action == index else ' '}[{label}]"
            _write(screen, y, x, caption, theme.selected if action == index else 0)
            x += len(caption) + 2

    def submit(name, method, path, payload=None, *, writing=False):
        nonlocal future, tag, write_pending
        tag, write_pending = name, writing
        future = pool.submit(api.post, path, payload) if method == "post" else pool.submit(api.get, path)

    def input_value():
        key, _, default = current_fields()[step]
        options = choices_for(key, values, active)
        value = values.get(key, default)
        return (value if value in options else options[0] if options else "") if options is not None else value

    if recovery_request:
        mode = 'scale_recovery'
    submit('scale_recovery' if recovery_request else 'list', 'get', '/v1/strategies')
    screen.timeout(100)
    try:
        while True:
            if future is not None and future.done():
                completed, future = future, None
                try:
                    result = completed.result()
                    if tag == 'capacity':
                        capacity_report, capacity_error, capacity_at = result, '', time.monotonic()
                    elif tag == 'scale_recovery':
                        recovery_lines, recovered_id, resolved = scale_recovery_lines(recovery_request, result)
                        rows = result.get('strategies', [])
                        worker_running = result.get('worker_running')
                        selected = recovered_id or (rows[0]['id'] if rows else None)
                        mode, scroll = 'scale_recovery', 0
                        if resolved:
                            api._pending_scale_request = None
                    elif tag == "coins":
                        if not result.get("reachable"):
                            raise ValueError("KDF non raggiungibile: impossibile leggere le coin attive")
                        payload = result.get("enabled_coins", {})
                        active = sorted({c["ticker"] for c in payload.get("coins", [])})
                        if len(active) < 2:
                            raise ValueError("Attiva almeno due coin nella pagina Stato KDF prima di creare un mercato")
                        mode, values, specs, original, step = "form", {}, [], None, 0
                        text = input_value()
                        message = ""
                        submit("wallet", "get", "/v1/strategies/wallet")
                    elif tag == "wallet":
                        balances = result.get("balances", {})
                    elif tag == "opposite":
                        opposite = StrategySpec.from_payload(result["spec"])
                        if values["opposite"] == "personalizza":
                            original = opposite
                            values, step, mode = values_for(opposite), 0, "form"
                            text = input_value()
                        else:
                            specs.append(opposite.payload())
                            submit("preview", "post", "/v1/strategies/preview", {"specs": specs})
                    elif tag == "preview":
                        preview, mode, scroll, text = result, "preview", 0, ""
                        action = 1
                    else:
                        if tag == 'scale':
                            api._pending_scale_request = None
                        rows = result.get("strategies", [])
                        worker_running = result.get('worker_running')
                        ids = [row["id"] for row in rows]
                        selected = selected if selected in ids else (ids[0] if ids else None)
                        mode = "list"
                        if tag != "list":
                            message = "Livelli Scala pubblicati" if tag == "scale" else "Eliminazione completata; storico conservato" if tag == "delete" else "Salvato in pausa" if tag == "save" else "Stato aggiornato"
                        last_refresh = time.monotonic()
                except Exception as exc:
                    if tag == 'capacity':
                        capacity_report, capacity_error, capacity_at = None, str(exc), time.monotonic()
                        continue
                    if tag in {'scale', 'scale_recovery'}:
                        recovery_request = api._pending_scale_request or recovery_request
                        recovery_lines = ['ESITO SCALA DA VERIFICARE', '', str(exc), '',
                                          'Non ripetere Pubblica. R verifica la richiesta già inviata.',
                                          'ID: scala-' + recovery_request['request_id']]
                        mode, scroll = 'scale_recovery', 0
                        if tag == 'scale':
                            submit('scale_recovery', 'get', '/v1/strategies')
                        write_pending = False
                        continue
                    if tag == 'list':
                        worker_running = None
                    message = str(exc)
                    if tag in {"preview", "opposite"}:
                        mode = "form"
                        step = max(0, len(current_fields()) - 1)
                        text = input_value()
                    error_return, mode, scroll = mode, "error", 0
                if future is None:
                    write_pending = False
            if mode == "list" and future is None and time.monotonic() - last_refresh > 3:
                submit("list", "get", "/v1/strategies")
            capacity_payload = None
            if mode == 'form' and not scaling and current_fields()[step][0] in {'quantity_mode', 'auto_percent', 'fixed_sold', 'max_mode', 'max_sold'}:
                candidate = dict(values)
                if current_fields()[step][0] == 'fixed_sold':
                    candidate['fixed_sold'] = text
                if current_fields()[step][0] == 'auto_percent':
                    candidate['auto_percent'] = text
                try:
                    capacity_payload = capacity_spec(candidate, original=original)
                    if future is None and time.monotonic() - last_input_at > .35 and (capacity_payload != capacity_key or time.monotonic() - capacity_at > 10):
                        capacity_key, capacity_report, capacity_error = capacity_payload, None, ''
                        submit('capacity', 'post', '/v1/strategies/capacity', {'spec': capacity_payload})
                except (ValueError, KeyError):
                    capacity_report = None
            screen.erase()
            height, width = screen.getmaxyx()
            _write(screen, 1, 2, "STRATEGIE LOCALI · KDF + CEX", theme.title)
            if mode == 'scale_recovery':
                lines = [part for line in recovery_lines for part in (textwrap.wrap(line, max(10, width - 5)) or [''])]
                capacity = max(1, height - 8)
                scroll = min(scroll, max(0, len(lines) - capacity))
                for i, line in enumerate(lines[scroll:scroll + capacity]):
                    _write(screen, 3 + i, 2, line, theme.section if line.isupper() else 0)
                _footer(screen, '[R] verifica stato   [↑/↓] scorri   [Esc] elenco strategie',
                        'Solo lettura: nessun reinvio e nessun nuovo ID', theme)
            elif mode == "error":
                lines = textwrap.wrap(message, max(10, width - 5))
                capacity = max(1, height - 8)
                scroll = min(scroll, max(0, len(lines) - capacity))
                _write(screen, 3, 2, "Non è stato possibile completare la richiesta", theme.warning)
                for i, line in enumerate(lines[scroll:scroll + capacity]):
                    _write(screen, 5 + i, 2, line)
                _footer(screen, "[↑/↓] scorri errore   [Invio/Esc] torna al modulo", "I dati inseriti sono conservati", theme)
            elif mode == "form":
                form = current_fields()
                step = min(step, len(form) - 1)
                key, label, default = form[step]
                if (scaling or step > 0) and values.get("sold"):
                    context = f"Vendi {values['sold']}"
                    if values.get("bought"):
                        context += f" → compra {values['bought']}"
                    _write(screen, 3, 2, context, theme.section)
                if key == "fixed_price":
                    sold = AssetRoute.parse(values["sold"], automatic_asset(values["sold"]) or values.get("sold_asset"))
                    unit = f"{values['sold']}/{values['bought']}" if sold.asset == "USDT" else f"{values['bought']}/{values['sold']}"
                    if original:
                        unit = f"{original.quote.ticker}/{original.base.ticker}"
                    label = f"Prezzo finale {unit} (premium già incluso)"
                if scaling and key == "opposite_quantity":
                    label = f"Quantità speculare da vendere in {values['bought']}"
                _write(screen, 6, 2, f"Passo {step + 1}/{len(form)}  {label}", theme.selected)
                if key in {"budget_mode", "budget"}:
                    row = balances.get(values.get("sold"), {})
                    balance = row.get("balance", "non disponibile") if row.get("available") else "non disponibile"
                    _write(screen, 4, 2, f"Saldo KDF: {balance} {values.get('sold', '')}", theme.section)
                options = choices_for(key, values, active)
                if options is not None:
                    position = options.index(text) if text in options else 0
                    capacity = max(1, height - 13)
                    start = max(0, position - capacity + 1)
                    for i, option in enumerate(options[start:start + capacity]):
                        _write(screen, 8 + i, 2, f"[{'x' if option == text else ' '}] {option}", theme.selected if option == text else 0)
                else:
                    _write(screen, 8, 2, "> " + text + "_", theme.section)
                if capacity_payload is not None:
                    hint = (capacity_lines(capacity_report) if capacity_report and capacity_payload == capacity_key else
                            [capacity_error or f"Calcolo del massimo fattibile su KDF e {values.get('cex', 'MEXC')}..."])
                    hint = [part for line in hint for part in textwrap.wrap(line, max(10, width - 5))]
                    for i, line in enumerate(hint[:max(0, height - 16)]):
                        _write(screen, 11 + i, 2, line, theme.warning)
                _footer(screen, "[↑/↓] scegli   [Invio] avanti   [Esc] indietro" if options is not None else "[Invio] avanti   [Esc] indietro   [Ctrl+U] svuota campo", "Nessun ordine viene inviato durante la compilazione", theme)
            elif mode == "preview":
                lines = preview_lines(preview)
                limit = max(1, height - 9)
                scroll = min(scroll, max(0, len(lines) - limit))
                for i, line in enumerate(lines[scroll:scroll + limit]):
                    _write(screen, 3 + i, 2, line)
                actions(height - 5, ("Pubblica", "Modifica") if scaling else ("Salva", "Modifica"))
                _footer(screen, "[←/→ o Tab] scegli  [Invio] conferma  [↑/↓] scorri", "[Esc] modifica · Conferma ordini REALI" if scaling else "[Esc] modifica · Salvataggio in pausa; non avvia il mercato", theme)
            elif mode == "confirm":
                _write(screen, 4, 2, f"{'Eliminare' if deleting else 'Avviare'} la strategia {selected}?", theme.warning)
                row = next((r for r in rows if r['id'] == selected), None)
                if row:
                    target = StrategySpec.from_payload(row['spec'])
                    _write(screen, 5, 2, f"Vendi {target.sold.ticker} → compra {target.bought.ticker}")
                _write(screen, 6, 2, "Prima mette in pausa e ritira gli ordini, poi elimina la configurazione." if deleting else "Può pubblicare ordini REALI con i permessi live configurati.")
                actions(8, confirmation_labels())
                if deleting and len(deletion_ids) == 2:
                    other = next(r for r in rows if r['id'] == deletion_ids[1])
                    reverse = StrategySpec.from_payload(other['spec'])
                    _write(screen, 10, 2, f"Entrambe include anche: {reverse.sold.ticker} → {reverse.bought.ticker}")
                    _write(screen, 11, 2, f"ID: {deletion_ids[1]}")
                _footer(screen, "[←/→ o Tab] scegli  [Invio] conferma  [Esc] indietro", "Swap irrisolti bloccano l'eliminazione; lo storico resta conservato" if deleting else "La pubblicazione resta subordinata a fondi, feed e copertura", theme)
            else:
                _write(screen, 3, 2, "[N] nuova  [S] Scala  [E] modifica  [G] avvia  [H] pausa  [D] elimina", theme.section)
                if not rows:
                    _write(screen, 6, 2, "Nessuna strategia avanzata. I target precedenti restano invariati.")
                ids = [row["id"] for row in rows]
                position = ids.index(selected) if selected in ids else 0
                visible = max(1, (height - 11) // 4)
                start = max(0, position - visible + 1)
                for index, row in enumerate(rows[start:start + visible]):
                    spec = StrategySpec.from_payload(row["spec"])
                    y = 5 + index * 4
                    _write(screen, y, 2, f"{'>' if row['id'] == selected else ' '} {row['id']}  {spec.sold.ticker} → {spec.bought.ticker}  [{spec.cex}]  {row['state']}", theme.selected if row["id"] == selected else 0)
                    _write(screen, y + 1, 4, _strategy_repricing_label(row, worker_running), theme.section)
                    _write(screen, y + 2, 4, f"Premium {spec.premium * 100}%; residuo {row['remaining_sold']} {spec.sold.ticker}")
                    _write(screen, y + 3, 4, row.get("detail", ""), theme.warning)
                _footer(screen, "[↑/↓] scegli   [N/G/H] azione   [R] aggiorna", "[Esc] menu principale   [V] comandi precedenti", theme)
            if mode != "error":
                _write(screen, height - 4, 2, "Richiesta in corso; attendi l'esito" if future is not None else message,
                       theme.warning if future is not None else theme.muted)
            screen.refresh()
            key = _normalize_key(screen, screen.getch())
            if key != -1:
                last_input_at = time.monotonic()
            if key == 3:
                raise KeyboardInterrupt
            if future is not None and tag == "list" and mode == "list" and key != -1 and key != curses.KEY_RESIZE:
                # An automatic read must not swallow input. Detach its result;
                # running reads are harmless and never resurrect a closed modal.
                future.cancel()
                future = None
                last_refresh = time.monotonic()
            if future is not None and tag != 'capacity':
                # Writes cannot be cancelled locally: their remote outcome must
                # remain visible. Read-only waits may detach without reopening.
                if key == 27 and not write_pending and mode == 'scale_recovery':
                    future.cancel()
                    future = None
                    mode, scaling = 'list', False
                    message = 'Verifica interrotta, non invio annullato. S riapre lo stato della richiesta.'
                    last_refresh = time.monotonic()
                    continue
                if key == 27 and not write_pending and mode != 'scale_recovery':
                    return "HOME"
                continue
            if mode == 'scale_recovery':
                if key in (ord('r'), ord('R')):
                    submit('scale_recovery', 'get', '/v1/strategies')
                elif key == 27:
                    mode, scroll, scaling = 'list', 0, False
                    message = 'Esito Scala da verificare: S riapre la verifica.' if api._pending_scale_request else 'Richiesta Scala registrata; controllare gli stati dei livelli.'
                    last_refresh = 0
                elif key in (curses.KEY_UP, curses.KEY_DOWN):
                    scroll = max(0, scroll + (-1 if key == curses.KEY_UP else 1))
                continue
            if mode == "error":
                if key in (10, 13, curses.KEY_ENTER, 27):
                    mode, message, scroll = error_return, "", 0
                elif key in (curses.KEY_UP, curses.KEY_DOWN):
                    scroll = max(0, scroll + (-1 if key == curses.KEY_UP else 1))
                continue
            if mode in {"form", "preview", "confirm"}:
                if mode in {"preview", "confirm"}:
                    labels = (("Pubblica", "Modifica") if scaling else ("Salva", "Modifica")) if mode == "preview" else confirmation_labels()
                    if key in (curses.KEY_LEFT, curses.KEY_RIGHT, 9, ord(" ")) or (mode == "confirm" and key in (curses.KEY_UP, curses.KEY_DOWN)):
                        action = (action + (-1 if key in (curses.KEY_LEFT, curses.KEY_UP) else 1)) % len(labels)
                    elif mode == "preview" and key in (curses.KEY_UP, curses.KEY_DOWN):
                        scroll = max(0, scroll + (-1 if key == curses.KEY_UP else 1))
                    elif key == 27 or (key in (10, 13, curses.KEY_ENTER) and action == len(labels) - 1):
                        if mode == "preview":
                            mode = "form"
                            step = len(current_fields()) - 1
                            text = input_value()
                        else:
                            mode = "list"
                    elif key in (10, 13, curses.KEY_ENTER):
                        if mode == "preview":
                            if scaling:
                                api._pending_scale_request = scale_request.copy()
                                recovery_request = scale_request.copy()
                                submit("scale", "post", "/v1/strategies/scale", {**scale_request,
                                       "confirmed_reductions": preview.get("reductions", []),
                                       "confirmed_quantity": preview.get('resolved_quantity'),
                                       "confirmation": "PUBBLICA SCALA " + scale_request["source_id"]}, writing=True)
                            elif editing:
                                submit("save", "post", "/v1/strategies/update", {"spec": specs[0], "confirmation": "AGGIORNA IN PAUSA"}, writing=True)
                            else:
                                submit("save", "post", "/v1/strategies/create", {"specs": specs, "confirmation": "SALVA IN PAUSA"}, writing=True)
                        elif deleting:
                            ids = deletion_ids if len(deletion_ids) == 2 and action == 1 else deletion_ids[:1]
                            submit("delete", "post", "/v1/strategies/delete-group", {"strategy_ids": ids, "confirmation": "PAUSA ED ELIMINA " + ",".join(ids)}, writing=True)
                        else:
                            submit("start", "post", "/v1/strategies/start", {"strategy_id": selected, "confirmation": "AVVIA " + selected}, writing=True)
                    continue
                options = choices_for(current_fields()[step][0], values, active) if mode == "form" else None
                if options is not None and key in (curses.KEY_UP, curses.KEY_DOWN, ord(" ")):
                    if options:
                        pos = options.index(text) if text in options else 0
                        text = options[(pos + (-1 if key == curses.KEY_UP else 1)) % len(options)]
                    continue
                if mode == "preview" and key in (curses.KEY_UP, curses.KEY_DOWN):
                    scroll = max(0, scroll + (-1 if key == curses.KEY_UP else 1))
                elif key == 27:
                    if mode == "form" and step > 0:
                        values[current_fields()[step][0]] = text
                        step -= 1
                        text = input_value()
                    elif mode == "preview":
                        mode = "form"
                        step = len(current_fields()) - 1
                        text = input_value()
                    else:
                        mode = "list"
                elif key in (curses.KEY_BACKSPACE, 127, 8) and options is None:
                    text = text[:-1]
                elif key == 21 and options is None:
                    text = ""
                elif key in (10, 13, curses.KEY_ENTER):
                    try:
                        if mode == "form":
                            field_key = current_fields()[step][0]
                            value = validate_field(field_key, text)
                            if options is not None and value not in options:
                                raise ValueError("Scegli un'opzione dalla lista")
                            if field_key in {"sold", "bought"} and values.get(field_key) != value:
                                values.pop(field_key + "_asset", None)
                            values[field_key] = value
                            if field_key == "budget_mode" and value != "personalizza":
                                values["budget"] = str(budget_amount(values, balances))
                            message = ""
                            form = current_fields()
                            if step + 1 < len(form):
                                step += 1
                                text = input_value()
                            elif scaling:
                                scale_request.update(quantity=values["scale_quantity"], premium=values["premium"],
                                                     opposite=values["opposite"])
                                for name in ("opposite_quantity", "opposite_premium"):
                                    if name in values:
                                        scale_request[name] = values[name]
                                submit("preview", "post", "/v1/strategies/scale-preview", scale_request.copy())
                            else:
                                if values.get("replenish") == "si":
                                    values["budget"] = "1"
                                spec = draft_spec(values, original=original)
                                if editing:
                                    specs = [spec.payload()]
                                elif original:
                                    specs = [specs[0], spec.payload()]
                                else:
                                    specs = [spec.payload()]
                                if not original and values.get("opposite") != "no":
                                    submit("opposite", "post", "/v1/strategies/opposite", {"spec": specs[0]})
                                else:
                                    submit("preview", "post", "/v1/strategies/preview", {"specs": specs})
                    except Exception as exc:
                        message = str(exc)
                elif options is None and 32 <= key <= 126 and len(text) < 100:
                    text += chr(key)
                continue
            if key == 27:
                return "HOME"
            if key in (ord("v"), ord("V")):
                return "QUOTE"
            if key in (curses.KEY_UP, curses.KEY_DOWN) and rows:
                ids = [row["id"] for row in rows]
                selected = ids[(ids.index(selected) + (-1 if key == curses.KEY_UP else 1)) % len(ids)]
            elif key in (ord("n"), ord("N")):
                submit("coins", "get", "/v1/kdf/status")
                editing, scaling = False, False
            elif key in (ord("s"), ord("S")) and selected:
                if getattr(api, '_pending_scale_request', None):
                    recovery_request = api._pending_scale_request
                    mode = 'scale_recovery'
                    submit('scale_recovery', 'get', '/v1/strategies')
                    continue
                row = next(r for r in rows if r["id"] == selected)
                if not row["enabled"] or row["state"] in {"WRITING", "REVIEW_REQUIRED"}:
                    message = "Scala richiede una strategia attiva senza anomalie"
                else:
                    original = StrategySpec.from_payload(row["spec"])
                    mode, values, step, scaling, editing = "form", values_for(original), 0, True, False
                    values["fixed_sold"], values["premium"] = "", ""
                    scale_request = {"source_id": selected, "request_id": uuid.uuid4().hex}
                    text = input_value()
            elif key in (ord("e"), ord("E")) and selected:
                scaling = False
                row = next(r for r in rows if r["id"] == selected)
                if row["enabled"]:
                    message = "Prima premi H: modifica consentita soltanto in pausa"
                else:
                    original = StrategySpec.from_payload(row["spec"])
                    mode, values, specs, step, editing = "form", values_for(original), [], 0, True
                    text = input_value()
                    submit("wallet", "get", "/v1/strategies/wallet")
            elif key in (ord("g"), ord("G")) and selected:
                mode, text = "confirm", ""
                action = 1
                deleting = False
            elif key in (ord("d"), ord("D"), curses.KEY_DC) and selected:
                row = next(r for r in rows if r["id"] == selected)
                if row["state"] in {"WRITING", "REVIEW_REQUIRED"}:
                    message = "Risolvi le anomalie prima di eliminare"
                else:
                    deletion_ids = delete_targets(rows, selected)
                    mode, text, deleting = "confirm", "", True
                    action = len(confirmation_labels()) - 1
            elif key in (ord("h"), ord("H")) and selected:
                submit("pause", "post", "/v1/strategies/pause", {"strategy_id": selected}, writing=True)
            elif key in (ord("r"), ord("R")):
                last_refresh = 0
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        screen.timeout(100)
