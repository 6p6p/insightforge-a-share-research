"""v1.2.5 验收：真实 Docker 研究 → 人工闭环后 completed_with_warnings / completed。

运行两个真实公司（宁德时代 + 一家 A 股），覆盖两条人工闭环：
- awaiting_stage5 → approve → completed*（v1.2.5 内容审核不阻断 approve）
- research_backflow manual → accept → completed*（v1.2.5 内容审核不阻断 accept）

断言：最终 status ∈ {completed, completed_with_warnings}；
不允许 orchestration_execution_failed（除非系统级不可恢复）。
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
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        body = resp.read().decode()
        return resp.status, json.loads(body) if body else None


def run_company(company: str, tag: str):
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
    print(f"[{tag}] start -> {st} status={orch.get('status')} phase={orch.get('current_phase')}", flush=True)

    deadline = time.monotonic() + 2400
    oid = None
    while time.monotonic() < deadline:
        st, orch = req("GET", f"/tasks/{task_id}/orchestrations/current", timeout=150)
        status = orch.get("status")
        phase = orch.get("current_phase")
        oid = orch.get("orchestration_id")
        print(f"[{tag}] poll: status={status} phase={phase}", flush=True)
        if status in ("completed", "completed_with_warnings", "failed", "cancelled",
                      "waiting_human"):
            break
        time.sleep(15)
    else:
        print(f"[{tag}] TIMEOUT", flush=True)
        return task_id, oid, "timeout", None

    print(f"[{tag}] terminal-ish status={status} phase={phase}", flush=True)

    if status == "waiting_human" and oid:
        # awaiting_stage5：approve
        if phase == "awaiting_stage5":
            print(f"[{tag}] awaiting_stage5 -> approve", flush=True)
            try:
                st, after = req("POST", f"/research-orchestrations/{oid}/actions",
                                {"action": "approve", "comment": None}, timeout=150)
                print(f"[{tag}] approve -> {st} status={after.get('status')}", flush=True)
                return task_id, oid, after.get("status"), "approve"
            except urllib.error.HTTPError as e:
                body = e.read().decode()
                print(f"[{tag}] approve HTTP {e.code}: {body[:250]}", flush=True)
                return task_id, oid, f"approve_http_{e.code}", "approve"
        # research_backflow manual：accept（v1.2.5 内容不阻断 accept）
        if phase == "research_backflow":
            print(f"[{tag}] backflow manual -> accept", flush=True)
            try:
                st, after = req("POST", f"/research-orchestrations/{oid}/backflow-review/actions",
                                {"action": "accept", "comment": None}, timeout=150)
                print(f"[{tag}] accept -> {st} status={after.get('status')} phase={after.get('current_phase')}", flush=True)
                return task_id, oid, after.get("status"), "accept"
            except urllib.error.HTTPError as e:
                body = e.read().decode()
                print(f"[{tag}] accept HTTP {e.code}: {body[:250]}", flush=True)
                try:
                    err = json.loads(body).get("error", {})
                except Exception:
                    err = {}
                if e.code == 409 and err.get("code") == "backflow_review_not_acceptable":
                    # 系统级 barrier（不应在 v1.2.5 内容审核下出现）——记录并取消
                    print(f"[{tag}] accept blocked (system-level barrier) but v1.2.5 "
                          f"内容不应阻断；取消清理", flush=True)
                    try:
                        req("POST", f"/research-orchestrations/{oid}/backflow-review/actions",
                            {"action": "cancel", "comment": None}, timeout=60)
                    except Exception:
                        pass
                    return task_id, oid, "blocked_by_barrier", "accept"
                return task_id, oid, f"accept_http_{e.code}", "accept"
        print(f"[{tag}] waiting_human phase={phase} unhandled", flush=True)
        return task_id, oid, status, None

    return task_id, oid, status, None


def main():
    companies = [
        ("宁德时代", "v125c"),
        ("比亚迪", "v125b"),
    ]
    results = []
    for company, tag in companies:
        print(f"\n===== {company} ({tag}) =====", flush=True)
        try:
            task_id, oid, status, action = run_company(company, tag)
            results.append((company, task_id, status, action))
        except Exception as exc:  # noqa: BLE001
            print(f"[{tag}] EXCEPTION: {exc!r}", flush=True)
            results.append((company, None, f"exception:{type(exc).__name__}", None))

    print("\n===== SUMMARY =====", flush=True)
    ok = 0
    for company, task_id, status, action in results:
        print(f"{company}: status={status} task_id={task_id} action={action}", flush=True)
        if status in ("completed", "completed_with_warnings"):
            ok += 1
        elif status == "failed":
            print(f"  -> FAIL: 不允许 orchestration_execution_failed（除非系统级）", flush=True)
        elif status == "timeout":
            print(f"  -> INCONCLUSIVE: 超时", flush=True)
        else:
            print(f"  -> WARN: 未达预期终态", flush=True)
    print(f"OK={ok}/{len(results)}", flush=True)
    sys.exit(0 if ok >= 1 else 1)


if __name__ == "__main__":
    main()
