#!/usr/bin/env python3
"""Read API server recovery signals through Grafana; never modify the cluster."""

import base64
import datetime
import json
import math
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def main():
    credential = subprocess.run(
        ["security", "find-generic-password", "-s", "dev.craftz.homelab.grafana-admin", "-a", "admin", "-w"],
        capture_output=True, text=True, timeout=15, check=False,
    )
    if credential.returncode or not credential.stdout.strip():
        raise RuntimeError("Grafana credential is unavailable in Keychain")
    authorization = base64.b64encode(("admin:" + credential.stdout.strip()).encode()).decode()

    def api(path):
        req = urllib.request.Request(
            "https://grafana.tailb6c7d.ts.net/api/datasources/proxy/uid/prometheus/api/v1/" + path,
            headers={"Authorization": "Basic " + authorization},
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.load(response)
        if result.get("status") != "success":
            raise RuntimeError("Prometheus did not return a successful response")
        return result["data"]

    def query(expr):
        rows = api("query?" + urllib.parse.urlencode({"query": expr}))["result"]
        return [{"labels": r["metric"], "value": float(r["value"][1]) if math.isfinite(float(r["value"][1])) else None} for r in rows]

    queries = {
        "apiserver_up": 'up{job="apiserver"}',
        "scrape_age_seconds": 'time() - timestamp(up{job="apiserver"})',
        "slo_5xx_per_second": 'sum(rate(apiserver_request_total{job="apiserver",code=~"5..",verb=~"GET|LIST|POST|PUT|PATCH|DELETE",subresource!~"proxy|attach|log|exec|portforward"}[5m]))',
        "etcd_p99_seconds": 'histogram_quantile(0.99,sum by(instance,le)(rate(etcd_request_duration_seconds_bucket{job="apiserver"}[5m])))',
        "burn_rates": 'sum by(__name__)({__name__=~"apiserver_request:burnrate.*"})',
    }
    result = {"checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    result.update({name: query(expr) for name, expr in queries.items()})
    rules = [r for g in api("rules")["groups"] for r in g["rules"] if r["name"] == "KubeAPIErrorBudgetBurn"]
    result["rules"] = [{k: r.get(k) for k in ["state", "labels", "health", "lastEvaluation"]} for r in rules]
    expected = {"172.16.40.11:6443", "172.16.40.12:6443", "172.16.40.13:6443"}
    up = result["apiserver_up"]
    fresh = result["scrape_age_seconds"]
    rules_fresh = len(rules) == 4 and all(
        r.get("health") == "ok"
        and 0 <= time.time() - datetime.datetime.fromisoformat(
            re.sub(r"(\.\d{6})\d+", r"\1", r["lastEvaluation"]).replace("Z", "+00:00")
        ).timestamp() < 120
        for r in rules
    )
    result["telemetry_healthy"] = (
        {r["labels"]["instance"] for r in up} == expected and len(up) == 3
        and all(r["value"] == 1 for r in up)
        and len(fresh) == 3 and all(r["value"] is not None and 0 <= r["value"] < 120 for r in fresh)
        and rules_fresh
    )
    result["alerts_cleared"] = result["telemetry_healthy"] and all(r["state"] == "inactive" for r in rules)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return result


if __name__ == "__main__":
    try:
        # Exit codes: 0 = telemetry verified healthy, 1 = queried successfully but
        # the API servers are not verified healthy, 2 = could not determine.
        # A wrapper that gates only on "did it run" must not treat 1 as success.
        # alerts_cleared is deliberately not part of the exit code: the burn-rate
        # alerts stay firing while past failures age out of the window.
        sys.exit(0 if main()["telemetry_healthy"] else 1)
    except urllib.error.HTTPError as exc:
        print(json.dumps({"error": "Grafana HTTP " + str(exc.code)}), file=sys.stderr)
        sys.exit(2)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        # Do not include request objects, headers, or subprocess output in errors.
        print(json.dumps({"error": type(exc).__name__, "detail": "API health check could not complete"}), file=sys.stderr)
        sys.exit(2)
