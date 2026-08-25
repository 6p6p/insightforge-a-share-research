"""v1.2.4 polish 真实闭环验证：approve → completed_with_warnings（可接受）
或 approve 拒绝 → 409 + orchestration failed 投影（blocking 保持）。

用法：python verify_approve_loop.py <company_query> <tag>
"""
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "http://127.0.0.1:8001/api/v1"


def req(method, path, payload=None, timeout=150):
    url = BASE + path
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        body = resp.read().decode()
        return resp.status, json.loads(body) if body else None


def main():
    company = sys.argv[1] if len(sys.argv) > 1 else "三一重工"
    tag = sys.argv[2] if len(sys.argv) > 2 else f"v124v-{int(time.time())}"

    payload = {
        "company_query": company,
        "research_start_date": str(datetime.now(timezone.utc).date() - timedelta(days=365)),
        "research_end_date": str(datetime.now(timezone.utc).date()),
        "modules": ["company_profile", "risk", "financial"],
        "questions": [f"近一年经营与风险概况（{tag}）"],
        "require_plan_approval": False,
    }
    st, task = req("POST", "/tasks", payload, timeout=30)
    print(f"[{tag}] create_task -> {st} task_id={task.get('task_id')}", flush=True)
    task_id = task["task_id"]

    st, orch = req("POST", f"/tasks/{task_id}/orchestrations", timeout=30)
    print(
        f"[{tag}] start orch -> {st} status={orch.get('status')} phase={orch.get('current_phase')}",
        flush=True,
    )

    deadline = time.monotonic() + 1500
    oid = None
    while time.monotonic() < deadline:
        st, orch = req("GET", f"/tasks/{task_id}/orchestrations/current", timeout=150)
        status = orch.get("status")
        phase = orch.get("current_phase")
        oid = orch.get("orchestration_id")
        print(f"[{tag}] poll: status={status} phase={phase}", flush=True)
        if status in (
            "completed",
            "completed_with_warnings",
            "failed",
            "cancelled",
            "waiting_human",
        ):
            break
        time.sleep(15)
    else:
        print(f"[{tag}] TIMEOUT waiting for terminal; aborting", flush=True)
        sys.exit(2)

    if status == "waiting_human" and phase == "awaiting_stage5" and oid:
        print(f"[{tag}] awaiting_stage5 -> POST /actions approve", flush=True)
        try:
            st, after = req("POST", f"/research-orchestrations/{oid}/actions",
                            {"action": "approve", "comment": None}, timeout=150)
            print(f"[{tag}] approve -> {st} status={after.get('status')} "
                  f"phase={after.get('current_phase')}", flush=True)
            if after.get("status") in ("completed", "completed_with_warnings"):
                print(f"[{tag}] PASS: approve closed loop -> {after.get('status')}", flush=True)
                sys.exit(0)
            print(f"[{tag}] FAIL: approve produced {after.get('status')}", flush=True)
            sys.exit(4)
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            try:
                err = json.loads(body).get("error", {})
            except Exception:
                err = {}
            code = err.get("code", "")
            print(f"[{tag}] approve HTTP {e.code} code={code}: {body[:200]}", flush=True)
            if e.code == 409 and code == "research_orchestration_approval_rejected":
                st2, after2 = req("GET", f"/tasks/{task_id}/orchestrations/current", timeout=60)
                print(f"[{tag}] after rejection: status={after2.get('status')} "
                      f"phase={after2.get('current_phase')} "
                      f"err={after2.get('error_code')}", flush=True)
                if after2.get("status") == "failed" and \
                        after2.get("error_code") == "stage5_approval_rejected":
                    print(f"[{tag}] PASS: blocking kept + projection synced to failed", flush=True)
                    sys.exit(0)
                print(f"[{tag}] FAIL: rejection did not project failed", flush=True)
                sys.exit(4)
            print(f"[{tag}] approve unexpected HTTP {e.code}", flush=True)
            sys.exit(5)

    if status == "waiting_human":
        print(f"[{tag}] waiting_human phase={phase} (backflow/manual) — "
              f"P1 验证不适用此路径；P2 轮数语义见 backflow_round="
              f"{orch.get('backflow_round')}", flush=True)
        sys.exit(6)

    print(f"[{tag}] RESULT status={status} phase={phase} "
          f"err={orch.get('error_code')}", flush=True)
    if status == "failed":
        print(f"[{tag}] INFO: failed（真实数据路径异常/backflow 无解），非 P1 approve 闭环验证", flush=True)
        sys.exit(7)
    if status in ("completed", "completed_with_warnings"):
        print(f"[{tag}] PASS: terminal completed", flush=True)
        sys.exit(0)
    sys.exit(3)


if __name__ == "__main__":
    main()
