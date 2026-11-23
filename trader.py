#!/usr/bin/env python3
"""Meme coin PAPER-TRADING scalper for Eka (Solana, fast in/out).

PAPER ONLY — hard-coded. This program has no wallet, no keys, no signing
and no transaction capability of any kind. It only reads public DexScreener
data over HTTPS (curl subprocess, same egress pattern as fcfs-poller) and
simulates trades against a virtual Rp100,000 capital.

Strategy "pullback-recovery" (v2, from 2026-11-22 — replaces "new-pair
momentum", which lost 9 of 12 trades / -78.7% of paper capital: it bought
AFTER pumps, at local tops, with a TP the winners never reached; see
STRATEGY.md): discover fresh pairs on the chain set in params.json
(chain: "solana" — pivoted from BSC 2026-11-20, BSC discovery was too
thin) via DexScreener token-profiles / token-boosts, then KEEP TRACKING
them as they age. Enter only SURVIVORS (pair age 1-12h, liquidity >=
$100k) that have pulled back 10-30% from the peak price the bot itself
observed, and only once recovery is confirmed (bounce off the pullback
trough + m5 momentum + buy/sell gates + the 90s LP-flow confirmation,
unchanged). One position at a time, exit on TP / SL / trailing /
time-stop / LP-pull.
ALL strategy numbers live in params.json (re-read every discovery cycle),
so tuning = editing params.json, never the code.

Friction model (honest, applied on every simulated fill):
- DEX fee 0.25% per side.
- Slippage per side: 1.5% if pair liquidity >= $50k, else 3%.
- Gas Rp1,000 per transaction (Solana base + priority fee), 2 per round
  trip, deducted in IDR.
- USD->IDR at the fixed rate in params.json (Rp18,300 default).
"""
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path("/home/hatch/workspace/meme-paper")
PARAMS_FILE = BASE / "params.json"
STATE_FILE = BASE / "state.json"
TRADES_FILE = BASE / "trades.jsonl"
LOG_FILE = BASE / "trader.log"
SKILL = Path("/home/hatch/workspace/custom-skills/airdrop-agent")
WIB = ZoneInfo("Asia/Jakarta")

PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
BOOSTS_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/"

DEFAULT_PARAMS = {
    "chain": "solana",
    "poll_discovery_sec": 60,
    "poll_position_sec": 25,
    # --- pullback-recovery universe: survivors only ---
    "min_pair_age_min": 60,
    "max_pair_age_min": 720,
    "max_watch_pairs": 60,
    "min_liquidity_usd": 100000,
    "min_volume_h1_usd": 25000,
    "min_volume_m5_usd": 3000,
    # --- pullback + recovery confirmation ---
    "pullback_min_pct": 10.0,
    "pullback_max_pct": 30.0,
    "recovery_bounce_pct": 3.0,
    "recovery_min_m5_pct": 1.0,
    "min_price_change_h1_pct": -60.0,
    "require_buys_gt_sells_m5": True,
    "require_h1_buys_ge_sells": True,
    # --- exits sized for bounce trades ---
    "take_profit_pct": 18.0,
    "stop_loss_pct": -8.0,
    "trailing_arm_pct": 10.0,
    "trailing_drop_pct": 5.0,
    "time_stop_min": 40,
    "cooldown_min": 5,
    # --- Eka's LP-flow protection (do not weaken) ---
    "lp_confirm_window_sec": 90,
    "lp_max_drop_pct": 3.0,
    "lp_exit_drop_pct": 25.0,
    # --- friction model ---
    "fee_pct_per_side": 0.25,
    "slippage_high_liq_pct": 1.5,
    "slippage_low_liq_pct": 3.0,
    "slippage_liq_threshold_usd": 50000,
    "gas_idr_per_tx": 1000,
    "usd_idr_rate": 18300,
    "start_capital_idr": 100000,
    "position_size_pct": 25.0,
}

EXIT_REASONS = ("take_profit", "stop_loss", "trailing", "time_stop", "lp_pull")


def now_wib() -> datetime:
    return datetime.now(WIB)


def log(msg: str) -> None:
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 500_000:
            LOG_FILE.write_text("")
        with LOG_FILE.open("a") as f:
            f.write(f"{now_wib().isoformat(timespec='seconds')} {msg}\n")
    except Exception:
        pass


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


def load_params() -> dict:
    p = dict(DEFAULT_PARAMS)
    disk = load_json(PARAMS_FILE, {})
    if isinstance(disk, dict):
        p.update(disk)
    return p


def fetch_json(url: str):
    out = subprocess.run(
        ["curl", "-s", "--max-time", "25", url],
        capture_output=True, text=True, timeout=40,
    )
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError(f"fetch failed rc={out.returncode} {url[:80]}")
    return json.loads(out.stdout)


def telegram(text: str) -> None:
    """Same mechanism as fcfs-poller: aa.notify via the airdrop-agent venv."""
    code = f"from aa import notify; notify.send({text!r})"
    r = subprocess.run(
        [str(SKILL / ".venv/bin/python"), "-c", code],
        cwd=str(SKILL), capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        log(f"telegram failed: {r.stderr.strip()[:160]}")
    else:
        log("telegram sent ok")


def fmt_idr(x: float) -> str:
    return f"Rp{x:,.0f}".replace(",", ".")


def empty_stats() -> dict:
    return {
        "trades": 0, "wins": 0, "losses": 0, "total_pnl_idr": 0.0,
        "by_exit_reason": {r: {"trades": 0, "wins": 0, "losses": 0, "pnl_idr": 0.0}
                           for r in EXIT_REASONS},
        "by_day": {},
    }


def fresh_state(params: dict) -> dict:
    return {
        "paper": True,
        "started_at": now_wib().isoformat(timespec="seconds"),
        "capital_idr": float(params["start_capital_idr"]),
        "open_position": None,
        "last_exit_ts": None,
        "seen_tokens": [],
        "watch_pairs": {},   # pairAddress -> tokenAddress (candidates in age window)
        "pair_track": {},    # pairAddress -> {peak_price, trough_price, first_ts} observed history
        "done_pairs": [],    # pairAddresses entered or aged out — never re-added
        "stats": empty_stats(),
    }


def slippage_pct(params: dict, liquidity_usd: float) -> float:
    if liquidity_usd >= params["slippage_liq_threshold_usd"]:
        return params["slippage_high_liq_pct"]
    return params["slippage_low_liq_pct"]


def pair_age_min(pair: dict, now_ts: float) -> float:
    created = pair.get("pairCreatedAt")
    if not created:
        return -1
    return (now_ts * 1000 - created) / 60000.0


def check_entry(pair: dict, params: dict, now_ts: float,
                track: dict | None = None) -> tuple[bool, str]:
    """Pullback-recovery gates. Return (passes, reason); reason explains
    the first failed filter. `track` is the bot's own observed price
    history for this pair (state.pair_track): peak_price = highest price
    seen since tracking began, trough_price = lowest price seen since
    the price first fell pullback_min_pct below that peak."""
    if pair.get("chainId") != params["chain"]:
        return False, f"not-{params['chain']}"
    age = pair_age_min(pair, now_ts)
    if age < params["min_pair_age_min"]:
        return False, f"too-young {age:.1f}m (survivors only)"
    if age > params["max_pair_age_min"]:
        return False, f"too-old {age:.1f}m"
    liq = (pair.get("liquidity") or {}).get("usd") or 0
    if liq < params["min_liquidity_usd"]:
        return False, f"liq ${liq:,.0f} < min"
    price = float(pair.get("priceUsd") or 0)
    if price <= 0:
        return False, "no price"
    if not track or not track.get("peak_price"):
        return False, "no observed price history yet"
    peak = float(track["peak_price"])
    drawdown = (price / peak - 1) * 100 if peak > 0 else 0.0
    if drawdown > -abs(params["pullback_min_pct"]):
        return False, f"no pullback yet ({drawdown:.1f}% from observed peak)"
    if drawdown < -abs(params["pullback_max_pct"]):
        return False, f"pullback too deep ({drawdown:.1f}% from peak) - possible death spiral"
    trough = float(track.get("trough_price") or 0)
    if trough <= 0:
        return False, "pullback trough not recorded yet"
    bounce = (price / trough - 1) * 100 if trough > 0 else 0.0
    if bounce < params["recovery_bounce_pct"]:
        return False, f"recovery bounce {bounce:.1f}% < min (still falling or flat)"
    pc = pair.get("priceChange") or {}
    pc_m5 = pc.get("m5") or 0
    if pc_m5 < params["recovery_min_m5_pct"]:
        return False, f"pcM5 {pc_m5}% < recovery min"
    pc_h1 = pc.get("h1") or 0
    if pc_h1 < params["min_price_change_h1_pct"]:
        return False, f"pcH1 {pc_h1}% - freefall, not a pullback"
    txns = (pair.get("txns") or {}).get("m5") or {}
    if params["require_buys_gt_sells_m5"] and not (txns.get("buys", 0) > txns.get("sells", 0)):
        return False, f"buys {txns.get('buys',0)} <= sells {txns.get('sells',0)}"
    txns_h1 = (pair.get("txns") or {}).get("h1") or {}
    if params.get("require_h1_buys_ge_sells") and not (txns_h1.get("buys", 0) >= txns_h1.get("sells", 0)):
        return False, f"h1 buys {txns_h1.get('buys',0)} < sells {txns_h1.get('sells',0)}"
    vol = pair.get("volume") or {}
    vol_h1 = vol.get("h1") or 0
    if vol_h1 < params["min_volume_h1_usd"]:
        return False, f"volH1 ${vol_h1:,.0f} < min"
    vol_m5 = vol.get("m5") or 0
    if vol_m5 < params["min_volume_m5_usd"]:
        return False, f"volM5 ${vol_m5:,.0f} < min"
    return True, (f"pullback {drawdown:.1f}% from peak, bounce {bounce:.1f}% "
                  f"off trough, all gates pass")


def discover_tokens(state: dict, chain: str) -> list[str]:
    """New token addresses on `chain` from profiles + boosts endpoints."""
    seen = set(state.get("seen_tokens", []))
    fresh = []
    for url in (PROFILES_URL, BOOSTS_URL):
        try:
            items = fetch_json(url)
        except Exception as e:
            log(f"discovery fetch error {url.split('/')[3]}: {str(e)[:100]}")
            continue
        if not isinstance(items, list):
            continue
        for it in items:
            if it.get("chainId") != chain:
                continue
            addr = it.get("tokenAddress")
            if addr and addr.lower() not in seen:
                seen.add(addr.lower())
                fresh.append(addr)
    state["seen_tokens"] = sorted(seen)[-3000:]
    return fresh


def fetch_pairs(token_addr: str, chain: str) -> list[dict]:
    try:
        d = fetch_json(TOKENS_URL + token_addr)
    except Exception as e:
        log(f"pairs fetch error {token_addr[:10]}: {str(e)[:80]}")
        return []
    return [p for p in (d.get("pairs") or []) if p.get("chainId") == chain]


def discovery_cycle(state: dict, params: dict, now_ts: float) -> None:
    """Refresh the watch list, prune aged-out pairs, try to enter."""
    chain = params["chain"]
    fresh_tokens = discover_tokens(state, chain)
    watch = state.setdefault("watch_pairs", {})
    done = set(state.get("done_pairs", []))
    added = 0
    for addr in fresh_tokens[:20]:  # cap fetches per cycle
        for pair in fetch_pairs(addr, chain):
            pa = pair.get("pairAddress")
            if not pa or pa in watch or pa in done:
                continue
            if pair_age_min(pair, now_ts) <= params["max_pair_age_min"]:
                watch[pa] = addr
                added += 1
    # Prune pairs that aged out of the window.
    still = {}
    for pa, addr in watch.items():
        pairs = fetch_pairs(addr, chain)
        match = next((p for p in pairs if p.get("pairAddress") == pa), None)
        if match is None:
            done.add(pa)
            continue
        if pair_age_min(match, now_ts) > params["max_pair_age_min"]:
            done.add(pa)
            log(f"watch prune (aged out): {match.get('baseToken', {}).get('symbol')} {pa[:10]}")
            continue
        still[pa] = (addr, match)
    # Cap the watch list: tracking pairs for up to 12h costs one API call
    # per watched token per cycle, so keep only the deepest-liquidity
    # pairs (also the best survivor candidates) when over the cap.
    max_watch = int(params.get("max_watch_pairs", 60))
    if len(still) > max_watch:
        ranked = sorted(
            still.items(),
            key=lambda kv: (kv[1][1].get("liquidity") or {}).get("usd") or 0,
            reverse=True)
        for pa, _ in ranked[max_watch:]:
            done.add(pa)
        still = dict(ranked[:max_watch])
        log(f"watch cap {max_watch}: trimmed to deepest-liquidity pairs")
    state["done_pairs"] = sorted(done)[-2000:]
    state["watch_pairs"] = {pa: addr for pa, (addr, _) in still.items()}
    # Maintain observed price history (peak / pullback trough) per pair —
    # this is what check_entry measures the pullback against.
    tracks = state.setdefault("pair_track", {})
    for pa, (addr, pair) in still.items():
        price = float(pair.get("priceUsd") or 0)
        if price <= 0:
            continue
        tr = tracks.get(pa)
        if tr is None:
            tracks[pa] = {"peak_price": price, "trough_price": None,
                          "first_ts": now_ts}
            continue
        if price > float(tr.get("peak_price") or 0):
            tr["peak_price"] = price
            tr["trough_price"] = None  # new high: any old pullback is over
        else:
            peak = float(tr.get("peak_price") or price)
            if price <= peak * (1 - abs(params["pullback_min_pct"]) / 100.0):
                cur = tr.get("trough_price")
                tr["trough_price"] = price if cur is None else min(float(cur), price)
    for pa in list(tracks):
        if pa not in still:
            tracks.pop(pa, None)
    log(f"discovery: {len(fresh_tokens)} new tokens, {added} new watch pairs, "
        f"{len(still)} watched, {len(done)} done")

    if state.get("open_position"):
        return
    last_exit = state.get("last_exit_ts")
    if last_exit and (now_ts - last_exit) < params["cooldown_min"] * 60:
        return
    if "lp_watch" in state:
        state["lp_watch"] = {pa: r for pa, r in state["lp_watch"].items() if pa in still}
    # Evaluate candidates, deepest liquidity first (survivor quality).
    cands = sorted(still.values(),
                   key=lambda t: (t[1].get("liquidity") or {}).get("usd") or 0,
                   reverse=True)
    tracks = state.get("pair_track", {})
    for addr, pair in cands:
        ok, reason = check_entry(pair, params, now_ts,
                                 tracks.get(pair.get("pairAddress")))
        sym = (pair.get("baseToken") or {}).get("symbol", "?")
        if not ok:
            log(f"filter reject {sym} {pair.get('pairAddress', '')[:10]}: {reason}")
            continue
        log(f"filter PASS {sym}: {reason}")
        pa = pair["pairAddress"]
        liq_now = (pair.get("liquidity") or {}).get("usd") or 0
        lpw = state.setdefault("lp_watch", {})
        rec = lpw.get(pa)
        if rec is None:
            lpw[pa] = {"first_ts": now_ts, "first_liq": liq_now}
            log(f"lp-confirm start {sym}: liq ${liq_now:,.0f} - watching LP flow before entry")
            continue
        waited = now_ts - rec["first_ts"]
        if waited < params.get("lp_confirm_window_sec", 90):
            continue
        drop_pct = (liq_now / rec["first_liq"] - 1) * 100 if rec["first_liq"] else 0
        if drop_pct < -abs(params.get("lp_max_drop_pct", 3.0)):
            log(f"lp-confirm FAIL {sym}: liquidity {drop_pct:.1f}% (LP outflow) - skip")
            lpw.pop(pa, None)
            done.add(pa)
            state["done_pairs"] = sorted(done)[-2000:]
            continue
        log(f"lp-confirm OK {sym}: liquidity {drop_pct:+.1f}% over {waited:.0f}s - entering")
        lpw.pop(pa, None)
        enter_position(state, params, pair, now_ts, tracks.get(pa))
        state["watch_pairs"].pop(pa, None)
        done.add(pa)
        state["done_pairs"] = sorted(done)[-2000:]
        break


def enter_position(state: dict, params: dict, pair: dict, now_ts: float,
                   track: dict | None = None) -> None:
    price = float(pair.get("priceUsd") or 0)
    if price <= 0:
        return
    liq = (pair.get("liquidity") or {}).get("usd") or 0
    slip = slippage_pct(params, liq) / 100.0
    fee = params["fee_pct_per_side"] / 100.0
    rate = params["usd_idr_rate"]
    size_idr = float(state["capital_idr"]) * float(params.get("position_size_pct", 100.0)) / 100.0
    fill_price = price * (1 + slip)  # slippage works against us
    tokens = (size_idr / rate) * (1 - fee) / fill_price
    base = pair.get("baseToken") or {}
    state["open_position"] = {
        "symbol": base.get("symbol", "?"),
        "token_address": base.get("address"),
        "pair_address": pair.get("pairAddress"),
        "dex": pair.get("dexId"),
        "url": pair.get("url"),
        "entry_ts": now_ts,
        "entry_time_wib": now_wib().isoformat(timespec="seconds"),
        "entry_price_usd": price,
        "entry_fill_usd": fill_price,
        "peak_price_usd": price,
        "last_price_usd": price,
        "tokens": tokens,
        "size_idr": size_idr,
        "liquidity_usd_at_entry": liq,
        "h1_buys_at_entry": ((pair.get("txns") or {}).get("h1") or {}).get("buys", 0),
        "h1_sells_at_entry": ((pair.get("txns") or {}).get("h1") or {}).get("sells", 0),
        "pair_age_min_at_entry": round(pair_age_min(pair, now_ts), 1),
        "drawdown_from_peak_pct_at_entry": (
            round((price / float(track["peak_price"]) - 1) * 100, 1)
            if track and track.get("peak_price") else None),
        "bounce_from_trough_pct_at_entry": (
            round((price / float(track["trough_price"]) - 1) * 100, 1)
            if track and track.get("trough_price") else None),
    }
    save_json(STATE_FILE, state)
    log(f"ENTRY (paper) {base.get('symbol')} @ ${price:.8f} fill ${fill_price:.8f} "
        f"size {fmt_idr(size_idr)} liq ${liq:,.0f} pair {pair.get('pairAddress')}")


def close_position(state: dict, params: dict, reason: str, now_ts: float) -> None:
    pos = state["open_position"]
    price = float(pos.get("last_price_usd") or pos["entry_price_usd"])
    liq = float(pos.get("last_liquidity_usd") or pos.get("liquidity_usd_at_entry") or 0)
    slip = slippage_pct(params, liq) / 100.0
    fee = params["fee_pct_per_side"] / 100.0
    rate = params["usd_idr_rate"]
    fill_price = price * (1 - slip)  # slippage works against us
    proceeds_idr = pos["tokens"] * fill_price * rate * (1 - fee)
    gas_total = 2 * params["gas_idr_per_tx"]
    gross = proceeds_idr - pos["size_idr"]
    net = gross - gas_total
    capital_after = float(state["capital_idr"]) + net
    hold_sec = int(now_ts - pos["entry_ts"])

    state["capital_idr"] = capital_after
    state["open_position"] = None
    state["last_exit_ts"] = now_ts

    st = state.setdefault("stats", empty_stats())
    st["trades"] += 1
    win = net > 0
    st["wins" if win else "losses"] += 1
    st["total_pnl_idr"] += net
    er = st["by_exit_reason"].setdefault(
        reason, {"trades": 0, "wins": 0, "losses": 0, "pnl_idr": 0.0})
    er["trades"] += 1
    er["wins" if win else "losses"] += 1
    er["pnl_idr"] += net
    day = now_wib().date().isoformat()
    dy = st["by_day"].setdefault(day, {"trades": 0, "wins": 0, "losses": 0, "pnl_idr": 0.0})
    dy["trades"] += 1
    dy["wins" if win else "losses"] += 1
    dy["pnl_idr"] += net

    # Self-evaluation bookkeeping (Eka's mandate 2026-11-22, see
    # STRATEGY.md): every block of 10 closed trades under the current
    # strategy is scored — win rate < 40% OR drawdown from the strategy's
    # peak capital > 25% marks the strategy FAILED, flags the state and
    # reports it, so the next strategy iteration starts without waiting.
    eval_fail = None
    strat = state.get("strategy")
    if isinstance(strat, dict):
        strat["trades_since_start"] = int(strat.get("trades_since_start", 0)) + 1
        if win:
            strat["wins_since_start"] = int(strat.get("wins_since_start", 0)) + 1
        if capital_after > float(strat.get("peak_capital_idr") or 0):
            strat["peak_capital_idr"] = capital_after
        if strat["trades_since_start"] >= 10:
            block_trades = strat["trades_since_start"]
            block_wins = int(strat.get("wins_since_start", 0))
            wr = block_wins / block_trades * 100 if block_trades else 0.0
            peak = float(strat.get("peak_capital_idr") or capital_after)
            dd = (capital_after / peak - 1) * 100 if peak > 0 else 0.0
            failed = wr < 40.0 or dd <= -25.0
            strat["evals_done"] = int(strat.get("evals_done", 0)) + 1
            strat["last_eval"] = {
                "at_wib": now_wib().isoformat(timespec="seconds"),
                "block_trades": block_trades,
                "block_wins": block_wins,
                "win_rate_pct": round(wr, 1),
                "drawdown_from_peak_pct": round(dd, 1),
                "verdict": "FAIL" if failed else "PASS",
            }
            strat["trades_since_start"] = 0
            strat["wins_since_start"] = 0
            log(f"strategy eval #{strat['evals_done']} "
                f"({strat.get('name')}): WR {wr:.1f}% DD-from-peak {dd:.1f}% "
                f"-> {'FAIL' if failed else 'PASS'}")
            if failed:
                eval_fail = (strat.get("name"), wr, dd)

    rec = {
        "open_wib": pos["entry_time_wib"],
        "close_wib": now_wib().isoformat(timespec="seconds"),
        "strategy": (state.get("strategy") or {}).get("name"),
        "symbol": pos["symbol"],
        "token_address": pos["token_address"],
        "pair_address": pos["pair_address"],
        "entry_price_usd": pos["entry_price_usd"],
        "exit_price_usd": price,
        "hold_sec": hold_sec,
        "exit_reason": reason,
        "gross_pnl_idr": round(gross, 2),
        "gas_idr": gas_total,
        "net_pnl_idr": round(net, 2),
        "capital_after_idr": round(capital_after, 2),
    }
    with TRADES_FILE.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    save_json(STATE_FILE, state)
    log(f"EXIT (paper) {pos['symbol']} reason={reason} hold={hold_sec}s "
        f"net {fmt_idr(net)} capital {fmt_idr(capital_after)}")
    telegram(
        f"🧪 MEME PAPER — trade selesai\n"
        f"{pos['symbol']} keluar: {reason}, tahan {hold_sec // 60}m{hold_sec % 60}s\n"
        f"PnL bersih: {fmt_idr(net)} (kotor {fmt_idr(gross)}, gas {fmt_idr(gas_total)})\n"
        f"Modal virtual sekarang: {fmt_idr(capital_after)} "
        f"(total {st['trades']} trade, {st['wins']}W/{st['losses']}L)\n"
        f"{pos.get('url') or ''}"
    )
    if eval_fail:
        sname, wr, dd = eval_fail
        telegram(
            f"⚠️ MEME PAPER — EVALUASI STRATEGI: GAGAL\n"
            f"Strategi '{sname}' tidak lolos blok evaluasi 10 trade: "
            f"win rate {wr:.1f}% (batas 40%), drawdown dari puncak {dd:.1f}% "
            f"(batas -25%).\n"
            f"Sesuai mandat Eka (22 Nov): gaya strategi ini harus diganti — "
            f"iterasi strategi berikutnya disiapkan dan dilaporkan."
        )


def position_cycle(state: dict, params: dict, now_ts: float) -> None:
    pos = state.get("open_position")
    if not pos:
        return
    pairs = fetch_pairs(pos["token_address"], params["chain"])
    pair = next((p for p in pairs if p.get("pairAddress") == pos["pair_address"]), None)
    if pair is not None:
        price = float(pair.get("priceUsd") or 0)
        if price > 0:
            pos["last_price_usd"] = price
            pos["last_liquidity_usd"] = (pair.get("liquidity") or {}).get("usd") or 0
            if price > pos["peak_price_usd"]:
                pos["peak_price_usd"] = price
    price = float(pos.get("last_price_usd") or pos["entry_price_usd"])
    entry = pos["entry_price_usd"]
    peak = pos["peak_price_usd"]
    gain = (price / entry - 1) * 100 if entry else 0
    hold_min = (now_ts - pos["entry_ts"]) / 60.0

    reason = None
    liq_entry = float(pos.get("liquidity_usd_at_entry") or 0)
    liq_now = float(pos.get("last_liquidity_usd") or 0)
    if liq_entry > 0 and liq_now > 0 and \
            liq_now <= liq_entry * (1 - params.get("lp_exit_drop_pct", 25.0) / 100.0):
        reason = "lp_pull"
    elif gain <= params["stop_loss_pct"]:
        reason = "stop_loss"
    elif gain >= params["take_profit_pct"]:
        reason = "take_profit"
    elif (peak / entry - 1) * 100 >= params["trailing_arm_pct"] and \
            price <= peak * (1 - params["trailing_drop_pct"] / 100.0):
        reason = "trailing"
    elif hold_min >= params["time_stop_min"]:
        reason = "time_stop"
    if reason:
        close_position(state, params, reason, now_ts)
    else:
        save_json(STATE_FILE, state)


def main() -> None:
    params = load_params()
    state = load_json(STATE_FILE, None)
    if not isinstance(state, dict) or "capital_idr" not in state:
        state = fresh_state(params)
        save_json(STATE_FILE, state)
    log(f"meme-paper trader started (PAPER ONLY) capital {fmt_idr(state['capital_idr'])}")
    telegram(
        f"🧪 MEME PAPER mulai jalan (mode PAPER — bukan uang asli)\n"
        f"Modal virtual: {fmt_idr(state['capital_idr'])}\n"
        f"Strategi: pullback-recovery {params['chain'].capitalize()} — pair survivor "
        f"{params['min_pair_age_min']:.0f}-{params['max_pair_age_min'] / 60:.0f} jam, pullback "
        f"{params['pullback_min_pct']:.0f}-{params['pullback_max_pct']:.0f}% dari puncak + konfirmasi pulih, "
        f"TP +{params['take_profit_pct']:.0f}% / SL {params['stop_loss_pct']:.0f}% / "
        f"trailing / time-stop {params['time_stop_min']:.0f}m\n"
        f"Evaluasi mandiri tiap 10 trade (lihat STRATEGY.md) — laporan per trade masuk ke sini."
    )
    last_disc, last_pos = 0.0, 0.0
    while True:
        try:
            params = load_params()  # tuning loop: params re-read, no restart needed
            now_ts = time.time()
            if now_ts - last_disc >= params["poll_discovery_sec"]:
                last_disc = now_ts
                discovery_cycle(state, params, now_ts)
                save_json(STATE_FILE, state)
            if state.get("open_position") and now_ts - last_pos >= params["poll_position_sec"]:
                last_pos = now_ts
                position_cycle(state, params, now_ts)
        except Exception as e:  # keep the loop alive on any failure
            log(f"cycle error: {type(e).__name__}: {str(e)[:160]}")
        time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
