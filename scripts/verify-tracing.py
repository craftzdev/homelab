#!/usr/bin/env python3
"""Send a synthetic trace from the scraper namespace and verify it via Grafana.

Uses no application credentials or external scraping. The temporary Job is
deleted after the check; the trace/log remain for the configured retention.
"""
import argparse
import json
import os
import subprocess
import time
import uuid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", default=os.environ.get("KUBECONFIG", "_out/kubeconfig"))
    parser.add_argument("--image", default="python:3.12.12-alpine3.22",
                        help="Pinned Python image; a cached Harbor image can avoid public pulls")
    args = parser.parse_args()
    base = ["kubectl", "--kubeconfig", args.kubeconfig, "--request-timeout=20s"]

    def kube(*command, data=None):
        result = subprocess.run(base + list(command), input=data, text=True,
                                capture_output=True, timeout=45)
        if result.returncode:
            raise RuntimeError(result.stderr.strip())
        return result.stdout

    trace_id = uuid.uuid4().hex
    job_name = "otel-smoke-" + trace_id[:8]
    # Only fabricated values are transmitted. The forbidden attribute tests
    # receiver-side redaction; this is not an actual secret.
    program = """
import json,time,urllib.request,socket
for host,port in [
 ("tempo.logging.svc.cluster.local",3200),
 ("tempo.logging.svc.cluster.local",4317),
 ("alloy-otel.logging.svc.cluster.local",12345),
]:
 try:
  connection=socket.create_connection((host,port),timeout=2)
 except OSError:
  continue
 connection.close()
 raise RuntimeError("Unexpected direct access to "+host+":"+str(port))
print(json.dumps({"network_isolation":"passed"}),flush=True)
now=time.time_ns()
payload={"resourceSpans":[{"resource":{"attributes":[
 {"key":"service.name","value":{"stringValue":"tracing-smoke"}},
 {"key":"k8s.namespace.name","value":{"stringValue":"moshitoku-scraper"}},
 {"key":"authorization","value":{"stringValue":"synthetic-must-not-survive"}}]},
 "scopeSpans":[{"scope":{"name":"homelab.verify"},"spans":[{
 "traceId":"TRACE_ID","spanId":"0123456789abcdef","name":"scrape.run","kind":1,
 "startTimeUnixNano":str(now-100000000),"endTimeUnixNano":str(now),
 "attributes":[{"key":"test.id","value":{"stringValue":"TRACE_ID"}},
 {"key":"scraper.site","value":{"stringValue":"synthetic"}},
 {"key":"scraper.items.valid","value":{"intValue":"3"}},
 {"key":"url.full","value":{"stringValue":"synthetic-must-not-survive"}}],
 "status":{"code":1}}]}]}]}
body=json.dumps(payload).encode()
req=urllib.request.Request("http://alloy-otel.logging.svc.cluster.local:4318/v1/traces",
 data=body,headers={"Content-Type":"application/json"})
with urllib.request.urlopen(req,timeout=15) as response:
 print(json.dumps({"trace_id":"TRACE_ID","otlp_status":response.status,
                   "response":response.read().decode()}),flush=True)
""".replace("TRACE_ID", trace_id)
    pod_spec = {
        "restartPolicy": "Never", "automountServiceAccountToken": False,
        "imagePullSecrets": [{"name": "harbor-pull"}],
        "securityContext": {"runAsNonRoot": True, "runAsUser": 1000,
                            "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{
            "name": "verify", "image": args.image,
            "command": ["python3", "-c", program],
            "resources": {"requests": {"cpu": "10m", "memory": "32Mi"},
                          "limits": {"cpu": "100m", "memory": "128Mi"}},
            "securityContext": {"allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]}},
        }],
    }
    manifest = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": job_name, "namespace": "moshitoku-scraper"},
        "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 300,
                 "ttlSecondsAfterFinished": 600,
                 "template": {"metadata": {"labels": {
                     "homelab.craftz.dev/otel-client": "true",
                     "app.kubernetes.io/name": "tracing-smoke"}},
                              "spec": pod_spec}},
    }
    try:
        kube("apply", "-f", "-", data=json.dumps(manifest))
        deadline = time.monotonic() + 310
        while time.monotonic() < deadline:
            job = json.loads(kube("-n", "moshitoku-scraper", "get", "job", job_name, "-o", "json"))
            status = job.get("status", {})
            if status.get("succeeded"):
                break
            if status.get("failed"):
                raise RuntimeError(kube("-n", "moshitoku-scraper", "logs", "job/" + job_name))
            time.sleep(3)
        else:
            raise RuntimeError("Synthetic sender did not finish")
        print(kube("-n", "moshitoku-scraper", "logs", "job/" + job_name), flush=True)
        for _ in range(20):
            try:
                data = kube("-n", "monitoring", "exec", "deploy/kube-prometheus-stack-grafana",
                            "-c", "grafana", "--", "wget", "-qO-", "-T", "10",
                            "--header=Accept: application/json",
                            "http://tempo.logging.svc.cluster.local:3200/api/traces/" + trace_id)
                trace = json.loads(data)
                assert "synthetic-must-not-survive" not in data, "Attribute redaction failed"
                assert "scrape.run" in data and "synthetic" in data, "Expected span missing"
                print(json.dumps({"trace_id": trace_id, "query": "passed",
                                  "redaction": "passed", "trace_keys": list(trace)}), flush=True)
                return
            except (RuntimeError, json.JSONDecodeError):
                time.sleep(3)
        raise RuntimeError("Tempo could not return the synthetic trace through the Grafana Pod")
    finally:
        kube("-n", "moshitoku-scraper", "delete", "job", job_name,
             "--ignore-not-found=true", "--wait=false")


if __name__ == "__main__":
    main()
