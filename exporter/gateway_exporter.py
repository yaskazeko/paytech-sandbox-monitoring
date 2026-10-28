"""Prometheus exporter for a payment gateway sandbox API (black-box view, stdlib only).

The API key is read from $GW_KEY at startup and never printed. run.sh starts it in a container;
to run it bare: GW_KEY=... python3 -I gateway_exporter.py

Every REFRESH seconds it makes a few read-only calls and serves the results on :PORT/metrics.
It never creates, refunds or cancels payments.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.environ["GW_BASE_URL"].rstrip("/")
KEY = os.environ["GW_KEY"]
PORT = int(os.environ.get("GW_EXPORTER_PORT", "9105"))
REFRESH = int(os.environ.get("GW_REFRESH_SECONDS", "60"))
WINDOW_HOURS = int(os.environ.get("GW_WINDOW_HOURS", "24"))
STUCK_MINUTES = int(os.environ.get("GW_STUCK_MINUTES", "30"))
# Cloudflare in front of the API rejects the default Python-urllib User-Agent (error 1010)
UA = "gateway-exporter/1.0"

_lock = threading.Lock()
_body = "# no data yet\n"


def call(path, auth=True):
    """Return (http_code, parsed_json_or_None, seconds, error_text)."""
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if auth:
        headers["Authorization"] = "Bearer " + KEY
    req = urllib.request.Request(BASE + path, headers=headers)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            code, text = r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        code, text = e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # DNS, TLS, timeout
        return 0, None, time.monotonic() - t0, type(e).__name__
    took = time.monotonic() - t0
    try:
        return code, json.loads(text), took, ""
    except ValueError:
        return code, None, took, ""


def esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class Metrics:
    def __init__(self):
        self.lines = []
        self.seen = set()

    def add(self, name, value, help_, type_="gauge", **labels):
        if name not in self.seen:
            self.lines += [f"# HELP {name} {help_}", f"# TYPE {name} {type_}"]
            self.seen.add(name)
        lab = ",".join(f'{k}="{esc(v)}"' for k, v in sorted(labels.items()))
        self.lines.append(f"{name}{{{lab}}} {value}" if lab else f"{name} {value}")

    def text(self):
        return "\n".join(self.lines) + "\n"


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s):
    # the API returns naive timestamps like 2026-10-27T10:11:37; treated as UTC
    try:
        return datetime.fromisoformat(s.replace("Z", "")).replace(tzinfo=timezone.utc)
    except (AttributeError, ValueError):
        return None


def fetch_window(m, now):
    """Page through payments updated in the window; returns (rows, ok)."""
    since, until = iso(now - timedelta(hours=WINDOW_HOURS)), iso(now + timedelta(minutes=1))
    rows, offset, pages = [], 0, 0
    while pages < 50:
        code, js, took, err = call(f"/api/v1/payments?updated.gte={since}&updated.lt={until}&limit=100&offset={offset}")
        m.add("gateway_api_request_seconds", f"{took:.3f}", "Latency of the last API call", endpoint="payments_list")
        m.add("gateway_api_last_status_code", code, "HTTP status of the last API call (0 = network error)",
              endpoint="payments_list")
        if code != 200 or not isinstance(js, dict):
            return rows, False
        page = js.get("result") or []
        rows += page
        pages += 1
        if not js.get("hasMore") or not page:
            return rows, True
        offset += len(page)
    return rows, True


ORDER_OUTCOME = {"COMPLETED": "completed", "DECLINED": "declined", "CANCELLED": "cancelled"}


def fetch_payment(pid, cache):
    if pid not in cache:
        code, js, _, _ = call(f"/api/v1/payments/{pid}")
        p = js.get("result", js) if code == 200 and isinstance(js, dict) else None
        cache[pid] = p if isinstance(p, dict) else None
    return cache[pid]


def cascade_metrics(m, rows):
    """Deposits by attempt and by order.

    With cascading, the gateway tries the first terminal and, on a decline, routes the payment to the
    next one. Each declined attempt is stored as its own DEPOSIT with parentPaymentId pointing to the
    order's root payment and no referenceId; the root's state is the outcome the customer sees.
    So a decline in the attempt view is normal; a lost order is not.
    """
    deposits = {p["id"]: p for p in rows if p.get("paymentType") == "DEPOSIT" and p.get("id")}
    cache = dict(deposits)

    def root_of(p):
        seen = set()
        while p.get("parentPaymentId") and p["id"] not in seen:
            seen.add(p["id"])
            parent = fetch_payment(p["parentPaymentId"], cache)
            if not parent or parent.get("paymentType") != "DEPOSIT":
                break
            p = parent
        return p

    # attempts: every deposit row, by terminal
    attempts = {}
    for p in deposits.values():
        k = (p.get("shopName") or "unknown", p.get("terminalName") or "none", p.get("state") or "unknown")
        attempts[k] = attempts.get(k, 0) + 1
    for (shop, term, state), n in sorted(attempts.items()):
        m.add("gateway_deposit_attempts", n, "Deposit attempts in the window by terminal and state "
              "(a cascade decline is one attempt)", shop=shop, terminal=term, state=state)
    per_term = {}
    for (_, term, state), n in attempts.items():
        done, decl = per_term.get(term, (0, 0))
        per_term[term] = (done + n * (state == "COMPLETED"), decl + n * (state == "DECLINED"))
    for term, (done, decl) in sorted(per_term.items()):
        if done + decl:
            m.add("gateway_terminal_decline_ratio", f"{decl / (done + decl):.4f}",
                  "DECLINED / (COMPLETED + DECLINED) deposit attempts per terminal", terminal=term)
            m.add("gateway_terminal_finished_attempts", done + decl,
                  "COMPLETED + DECLINED deposit attempts per terminal", terminal=term)

    # orders: group attempts under their root payment
    orders = {}
    for p in deposits.values():
        root = root_of(p)
        orders.setdefault(root["id"], (root, []))[1].append(p)
    for rid, (root, members) in orders.items():
        if rid not in [q["id"] for q in members]:
            members.append(root)  # root fetched from outside the window
    outcome, rescued, no_ref, depth = {}, {}, {}, []
    for root, members in orders.values():
        shop = root.get("shopName") or "unknown"
        state = ORDER_OUTCOME.get(root.get("state"), "pending")
        outcome[(shop, state)] = outcome.get((shop, state), 0) + 1
        declined = sum(1 for q in members if q.get("state") == "DECLINED")
        if state == "completed" and declined:
            rescued[shop] = rescued.get(shop, 0) + 1
        if not root.get("referenceId"):
            no_ref[shop] = no_ref.get(shop, 0) + 1
        if state in ("completed", "declined"):
            depth.append(len(members))
    shops = sorted({s for s, _ in outcome})
    for shop in shops:
        for state in ("completed", "declined", "cancelled", "pending"):
            m.add("gateway_deposit_orders", outcome.get((shop, state), 0),
                  "Deposit orders (root payment plus its cascade attempts) by final outcome",
                  shop=shop, outcome=state)
        m.add("gateway_deposit_orders_rescued", rescued.get(shop, 0),
              "Completed orders that had at least one declined attempt (saved by the cascade)", shop=shop)
        m.add("gateway_deposit_orders_without_reference", no_ref.get(shop, 0),
              "Orders whose root payment has no referenceId (cannot be reconciled with the merchant)", shop=shop)
        done, lost = outcome.get((shop, "completed"), 0), outcome.get((shop, "declined"), 0)
        if done + lost:
            m.add("gateway_deposit_order_success_ratio", f"{done / (done + lost):.4f}",
                  "COMPLETED / (COMPLETED + DECLINED) deposit orders", shop=shop)
    if depth:
        m.add("gateway_deposit_attempts_per_order", f"{sum(depth) / len(depth):.3f}",
              "Average attempts per finished order (1 = no cascade needed)")


def collect():
    m = Metrics()
    now = datetime.now(timezone.utc)

    # 1. Is the edge up at all? No key: a healthy API answers 401; Cloudflare/origin trouble shows as 5xx or 403.
    code, _, took, err = call("/api/v1/payments?limit=1", auth=False)
    m.add("gateway_api_up", 1 if code == 401 else 0,
          "1 if the API answers an unauthenticated call with 401 (edge and origin reachable)")
    m.add("gateway_api_request_seconds", f"{took:.3f}", "Latency of the last API call", endpoint="unauth_probe")
    m.add("gateway_api_last_status_code", code, "HTTP status of the last API call (0 = network error)",
          endpoint="unauth_probe")

    # 2. Does our key work from this IP? Explains 401 (bad key / IP whitelist) vs 200.
    code, js, took, err = call("/api/v1/balances")
    m.add("gateway_api_auth_ok", 1 if code == 200 else 0, "1 if an authenticated call with our API key succeeds")
    m.add("gateway_api_request_seconds", f"{took:.3f}", "Latency of the last API call", endpoint="balances")
    m.add("gateway_api_last_status_code", code, "HTTP status of the last API call (0 = network error)",
          endpoint="balances")
    if code == 200 and isinstance(js, dict):
        for b in js.get("result") or []:
            amt = b.get("amount") if isinstance(b, dict) else None
            if isinstance(amt, (int, float)) and not isinstance(amt, bool):
                # a shop has one PROCESSING and one WITHDRAWAL balance per currency
                m.add("gateway_balance", amt, "Balance amount from /api/v1/balances (major units)",
                      currency=b.get("currency", "unknown"), kind=b.get("type", "unknown"))

    # 3. Business view of the window: counts, declines, stuck payments.
    rows, ok = fetch_window(m, now)
    m.add("gateway_payments_window_ok", 1 if ok else 0, "1 if the payments window was fetched completely")
    m.add("gateway_window_hours", WINDOW_HOURS, "Size of the payments window in hours")
    if ok:
        counts, amounts, stuck = {}, {}, {}
        for p in rows:
            key = (p.get("shopName") or "unknown", p.get("paymentType") or "unknown",
                   p.get("state") or "unknown", p.get("currency") or "unknown")
            counts[key] = counts.get(key, 0) + 1
            amt = p.get("amount")
            if isinstance(amt, (int, float)):
                amounts[key] = amounts.get(key, 0.0) + float(amt)
            created = parse_ts(p.get("created"))
            if p.get("state") == "CHECKOUT" and created and now - created > timedelta(minutes=STUCK_MINUTES):
                sk = (key[0], key[1])
                stuck[sk] = stuck.get(sk, 0) + 1
        for (shop, ptype, state, cur), n in sorted(counts.items()):
            m.add("gateway_payments", n, "Payments updated in the window, by shop, type, state and currency",
                  shop=shop, type=ptype, state=state, currency=cur)
        for (shop, ptype, state, cur), total in sorted(amounts.items()):
            m.add("gateway_payments_amount", f"{total:.2f}", "Sum of amounts in the window (major units)",
                  shop=shop, type=ptype, state=state, currency=cur)
        for (shop, ptype), n in sorted(stuck.items()):
            m.add("gateway_payments_stuck_checkout", n,
                  f"Payments still in CHECKOUT more than {STUCK_MINUTES} min after creation", shop=shop, type=ptype)
        cascade_metrics(m, rows)

    m.add("gateway_exporter_last_refresh_timestamp_seconds", int(time.time()), "Unix time of the last refresh")
    return m.text()


def loop():
    global _body
    while True:
        t0 = time.monotonic()
        try:
            text = collect()
            text += f"gateway_exporter_refresh_seconds {time.monotonic() - t0:.3f}\n"
            text += "gateway_exporter_refresh_ok 1\n"
        except Exception as e:  # keep serving the last good data
            print(f"[gateway-exporter] refresh failed: {type(e).__name__}", flush=True)
            with _lock:
                text = _body.replace("gateway_exporter_refresh_ok 1", "gateway_exporter_refresh_ok 0")
        with _lock:
            _body = text
        time.sleep(max(5, REFRESH - (time.monotonic() - t0)))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        with _lock:
            data = _body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):  # noqa: A002 - silence per-request logging
        pass


if __name__ == "__main__":
    threading.Thread(target=loop, daemon=True).start()
    print(f"[gateway-exporter] serving :{PORT}/metrics, refresh {REFRESH}s, base {BASE}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
