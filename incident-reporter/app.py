"""Alertmanager webhook that writes an incident report for each firing alert.

Pulls metrics from Prometheus and logs from Loki around the alert time, asks
an LLM (OpenRouter) for a report, and posts the link to Telegram.
"""
import html
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, abort, jsonify, render_template, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("incident-reporter")

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
LOKI_URL = os.getenv("LOKI_URL", "http://loki:3100")
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:5000").rstrip("/")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
# Pinned because the "openrouter/free" auto-router sometimes picked a safety
# classifier that answered "User Safety: safe" instead of a report.
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemma-4-31b-it:free")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

WINDOW_BEFORE = timedelta(minutes=10)
WINDOW_AFTER = timedelta(minutes=5)
COOLDOWN_SECONDS = 30 * 60
MAX_LOG_LINES = 150

reports_store = {}   # report_id -> report dict (in memory, lost on restart)
last_report_at = {}  # (alertname, instance) -> epoch seconds


def parse_time(ts: str) -> datetime:
    """Alertmanager sends RFC3339 with nanoseconds; trim to microseconds."""
    ts = ts.replace("Z", "+00:00")
    ts = re.sub(r"(\.\d{6})\d+", r"\1", ts)
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def query_prometheus(start: datetime, end: datetime) -> dict:
    """Return min/avg/max of CPU, memory and disk percent for every host.

    Keys look like "cpu_10.0.0.5:9100". All hosts are included so the report
    can compare the alerting host with its peers.

    Timestamps go out as epoch seconds: with naive ISO strings Prometheus
    returned an empty result and no error, so reports came out empty.
    """
    queries = {
        "cpu": '100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[2m])) * 100)',
        "memory": "(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100",
        "disk": '(1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}) * 100',
    }
    results = {}
    for name, q in queries.items():
        try:
            r = requests.get(
                f"{PROMETHEUS_URL}/api/v1/query_range",
                params={"query": q, "start": start.timestamp(), "end": end.timestamp(), "step": "30s"},
                timeout=15,
            )
            r.raise_for_status()
            for series in r.json().get("data", {}).get("result", []):
                values = [float(v[1]) for v in series["values"]]
                if not values:
                    continue
                instance = series.get("metric", {}).get("instance", "unknown")
                results[f"{name}_{instance}"] = {
                    "min": round(min(values), 2),
                    "avg": round(sum(values) / len(values), 2),
                    "max": round(max(values), 2),
                }
        except Exception as e:  # one missing metric should not kill the report
            log.warning("Prometheus query %s failed: %s", name, e)
    return results


def query_loki(start: datetime, end: datetime) -> list:
    """Return system and audit log lines from the window (Loki wants nanoseconds)."""
    try:
        r = requests.get(
            f"{LOKI_URL}/loki/api/v1/query_range",
            params={
                "query": '{job=~"varlogs|auditlogs"}',
                "start": int(start.timestamp() * 1e9),
                "end": int(end.timestamp() * 1e9),
                "limit": MAX_LOG_LINES,
                "direction": "backward",
            },
            timeout=15,
        )
        r.raise_for_status()
        lines = []
        for stream in r.json().get("data", {}).get("result", []):
            lines.extend(v[1] for v in stream["values"])
        return lines[:MAX_LOG_LINES]
    except Exception as e:
        log.warning("Loki query failed: %s", e)
        return []


def build_prompt(alert: dict, metrics: dict, logs: list, start: datetime, end: datetime) -> str:
    labels = alert.get("labels", {})
    ann = alert.get("annotations", {})
    metric_lines = "\n".join(f"- {k}: {v}" for k, v in metrics.items()) or "(no metrics returned)"
    log_block = "\n".join(logs) if logs else "(no logs returned)"
    return f"""You are a senior SRE. Write a concise incident report.

Alert: {labels.get('alertname')} (severity: {labels.get('severity')})
Instance: {labels.get('instance')}
Description: {ann.get('description', '')}
Window: {start.isoformat()} to {end.isoformat()}

Metrics per host (min/avg/max percent over the window):
{metric_lines}

Logs from the window (system and audit):
{log_block}

Use exactly these sections: 1. Incident Summary, 2. Affected Systems,
3. Key Findings, 4. Likely Root Cause, 5. Recommended Actions.
Base conclusions only on the evidence above. If audit logs show the command
that caused the spike, cite it. If evidence is insufficient, say so.
Do not use tables."""


def call_llm(prompt: str) -> str:
    if not OPENROUTER_API_KEY:
        return "AI analysis unavailable: OPENROUTER_API_KEY is not set."
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
            json={"model": OPENROUTER_MODEL, "messages": [{"role": "user", "content": prompt}]},
            timeout=90,
        )
        data = r.json()
        # errors come back as {"error": ...} with no "choices"
        if "choices" not in data or not data["choices"]:
            log.error("Unexpected OpenRouter response: %s", data)
            return f"AI analysis unavailable: {data.get('error', {}).get('message', 'unexpected response')}"
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        log.error("OpenRouter call failed: %s", e)
        return f"AI analysis unavailable: {e}"


def markdown_to_html(text: str) -> str:
    """Minimal markdown rendering for the report page.

    The text is escaped first: it is LLM output built from log lines, and the
    template renders the result unescaped.
    """
    out = []
    for line in text.splitlines():
        if line.strip().startswith("|"):          # drop tables
            continue
        if line and not line.isascii():           # drop emoji
            line = line.encode("ascii", "ignore").decode()
            if not line.strip():
                continue
        line = html.escape(line, quote=False)
        if re.fullmatch(r"\s*-{3,}\s*", line):
            out.append("<hr>")
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            lvl = min(len(m.group(1)) + 2, 6)
            out.append(f"<h{lvl}>{m.group(2)}</h{lvl}>")
            continue
        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        line = re.sub(r"(?<!\*)\*(?!\s)(.+?)\*", r"<em>\1</em>", line)
        line = re.sub(r"`([^`]+)`", r"<code>\1</code>", line)
        out.append(f"<p>{line}</p>" if line.strip() else "<br>")
    return "\n".join(out)


def send_telegram(text: str) -> None:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log.warning("Telegram send failed: %s", e)


@app.route("/")
def index():
    return render_template("index.html", count=len(reports_store))


@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.get_json(silent=True) or {}
    created = []
    for alert in payload.get("alerts", []):
        if alert.get("status") != "firing":
            continue
        labels = alert.get("labels", {})
        key = (labels.get("alertname"), labels.get("instance"))
        now = time.time()
        if now - last_report_at.get(key, 0) < COOLDOWN_SECONDS:
            log.info("Cooldown active for %s, skipping", key)
            continue
        last_report_at[key] = now

        started = parse_time(alert.get("startsAt", datetime.now(timezone.utc).isoformat()))
        start, end = started - WINDOW_BEFORE, started + WINDOW_AFTER
        end = min(end, datetime.now(timezone.utc))

        metrics = query_prometheus(start, end)
        logs = query_loki(start, end)
        analysis = call_llm(build_prompt(alert, metrics, logs, start, end))

        report_id = uuid.uuid4().hex[:8]
        reports_store[report_id] = {
            "id": report_id,
            "alertname": labels.get("alertname"),
            "instance": labels.get("instance"),
            "severity": labels.get("severity"),
            "window_start": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "window_end": end.strftime("%Y-%m-%dT%H:%M:%S"),
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "metrics": metrics,
            "analysis_html": markdown_to_html(analysis),
            "logs": logs,
        }
        send_telegram(
            f"\U0001F6A8 <b>Auto Incident Report Generated</b>\n"
            f"<b>Alert:</b> {html.escape(str(labels.get('alertname')))}\n"
            f"<b>Instance:</b> {html.escape(str(labels.get('instance')))}\n"
            f"<b>Report:</b> {APP_BASE_URL}/reports/{report_id}"
        )
        created.append(report_id)
    return jsonify({"created": created}), 200


@app.route("/reports")
def reports_list():
    items = sorted(reports_store.values(), key=lambda r: r["generated_at"], reverse=True)
    return render_template("reports_list.html", reports=items)


@app.route("/reports/<report_id>")
def report_detail(report_id):
    report = reports_store.get(report_id)
    if not report:
        abort(404)
    return render_template("report.html", r=report)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
