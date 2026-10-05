from unittest.mock import patch

import pytest

import app as reporter


@pytest.fixture(autouse=True)
def reset_state():
    reporter.reports_store.clear()
    reporter.last_report_at.clear()


@pytest.fixture
def client():
    return reporter.app.test_client()


def alert(name="HighCPUUsage", instance="10.0.0.5:9100", status="firing"):
    return {
        "status": status,
        "labels": {"alertname": name, "instance": instance, "severity": "warning"},
        "annotations": {"description": "CPU above 80%"},
        "startsAt": "2026-08-26T19:45:04.123456789Z",
    }


def test_markdown_formatting():
    html = reporter.markdown_to_html("## Title\n**bold** *it* `code`")
    assert "<h4>Title</h4>" in html
    assert "<strong>bold</strong>" in html
    assert "<em>it</em>" in html
    assert "<code>code</code>" in html


def test_markdown_strips_tables_and_converts_rules():
    html = reporter.markdown_to_html("| a | b |\n|---|---|\n---")
    assert "|" not in html
    assert "<hr>" in html


def test_markdown_escapes_html():
    html = reporter.markdown_to_html("Invalid user <script>alert(1)</script> from 1.2.3.4")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_parse_time_handles_nanoseconds():
    dt = reporter.parse_time("2026-08-26T19:45:04.123456789Z")
    assert dt.microsecond == 123456
    assert dt.tzinfo is not None


def test_prometheus_uses_epoch_seconds():
    start = reporter.parse_time("2026-08-26T19:35:04Z")
    end = reporter.parse_time("2026-08-26T19:50:04Z")
    with patch.object(reporter.requests, "get") as get:
        get.return_value.json.return_value = {"data": {"result": []}}
        reporter.query_prometheus(start, end)
        params = get.call_args.kwargs["params"]
        assert isinstance(params["start"], float)
        assert params["start"] == start.timestamp()


def test_prometheus_groups_by_instance():
    start = reporter.parse_time("2026-08-26T19:35:04Z")
    end = reporter.parse_time("2026-08-26T19:50:04Z")
    series = [
        {"metric": {"instance": "a:9100"}, "values": [[0, "10"], [30, "30"]]},
        {"metric": {"instance": "b:9100"}, "values": [[0, "5"]]},
    ]
    with patch.object(reporter.requests, "get") as get:
        get.return_value.json.return_value = {"data": {"result": series}}
        metrics = reporter.query_prometheus(start, end)
    assert metrics["cpu_a:9100"] == {"min": 10.0, "avg": 20.0, "max": 30.0}
    assert "disk_b:9100" in metrics


def test_llm_without_key_degrades_gracefully(monkeypatch):
    monkeypatch.setattr(reporter, "OPENROUTER_API_KEY", "")
    assert "unavailable" in reporter.call_llm("x")


def test_llm_error_response_has_no_choices(monkeypatch):
    monkeypatch.setattr(reporter, "OPENROUTER_API_KEY", "test")
    with patch.object(reporter.requests, "post") as post:
        post.return_value.json.return_value = {"error": {"message": "rate limited"}}
        assert "rate limited" in reporter.call_llm("x")


@patch.object(reporter, "send_telegram")
@patch.object(reporter, "call_llm", return_value="## Summary\nAll good")
@patch.object(reporter, "query_loki", return_value=["log line"])
@patch.object(reporter, "query_prometheus", return_value={"cpu_10.0.0.5:9100": {"min": 1.0, "avg": 2.0, "max": 99.5}})
def test_webhook_creates_report(_p, _l, _llm, tg, client):
    r = client.post("/webhook", json={"alerts": [alert()]})
    rid = r.get_json()["created"][0]
    page = client.get(f"/reports/{rid}")
    assert page.status_code == 200
    assert b"99.5%" in page.data
    assert client.get("/reports").status_code == 200
    tg.assert_called_once()


@patch.object(reporter, "send_telegram")
@patch.object(reporter, "call_llm", return_value="x")
@patch.object(reporter, "query_loki", return_value=[])
@patch.object(reporter, "query_prometheus", return_value={})
def test_webhook_cooldown_and_resolved(_p, _l, _llm, _tg, client):
    assert len(client.post("/webhook", json={"alerts": [alert()]}).get_json()["created"]) == 1
    # same alert + instance within 30 minutes is skipped
    assert client.post("/webhook", json={"alerts": [alert()]}).get_json()["created"] == []
    assert client.post("/webhook", json={"alerts": [alert("X", status="resolved")]}).get_json()["created"] == []


def test_unknown_report_404(client):
    assert client.get("/reports/nope").status_code == 404
