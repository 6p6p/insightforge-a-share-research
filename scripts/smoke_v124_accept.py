"""InsightForge v1.2.4 smoke：真实 docker 栈（8001 backend）人工接受路径验证。

验证目标（v1.2.4 §6 docker real）：
1. 创建任务 + 启动研究（require_plan_approval=False）；
2. 等待到达 waiting_human（research_backflow）或终态；
3. 若到达 waiting_human → GET backflow-review，断言：
   - REPORT 级缺陷存在时：acceptance_barriers 非空（暂不能接受）；
   - 章节级缺陷（section_warning / section_unavailable）：acceptance_barriers 空
     且 impact_scope 非 report_blocking → POST actions accept → 状态变为
     completed_with_warnings（不再 failed）。
4. 正常任务（无缺陷）→ 直接 completed。

用法：python smoke_v124_accept.py <company_query> <tag>
"""
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "http://127.0.0.1:8001/api/v1"


def req(method, path, payload=None, timeout=120):
    url = BASE + path
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        body = resp.read().decode()
        return resp.status, json.loads(body) if body else None


def main():
    company = sys.argv[1] if len(sys.argv) > 1 else "贵州茅台"
    tag = sys.argv[2] if len(sys.argv) > 2 else f"v124-{int(time.time())}"

    payload = {
        "company_query": company,
        "research_start_date": str(datetime.now(timezone.utc).date() - timedelta(days=365)),
        "research_end_date": str(datetime.now(timezone.utc).date()),
        "modules": ["company_profile", "risk", "financial"],
        "questions": [f"近一年经营与风险概况（{tag}）"],
        "require_plan_approval": False,
    }
    st, task = req("POST", "/tasks", payload, timeout=30)
    print(f"[{tag}] create_task -> {st} task_id={task.get('task_id')}")
    task_id = task["task_id"]

    st, orch = req("POST", f"/tasks/{task_id}/orchestrations", timeout=30)
    print(
        f"[{tag}] start orch -> {st} status={orch.get('status')} "
        f"phase={orch.get('current_phase')}"
    )

    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        st, orch = req("GET", f"/tasks/{task_id}/orchestrations/current", timeout=30)
        status = orch.get("status")
        phase = orch.get("current_phase")
        print(f"[{tag}] poll: status={status} phase={phase}")
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
        print(f"[{tag}] TIMEOUT waiting for terminal; aborting")
        sys.exit(2)

    if status != "waiting_human":
        print(f"[{tag}] RESULT status={status} phase={phase} (no human closure needed)")
        sys.exit(0 if status in ("completed", "completed_with_warnings") else 3)

    oid = orch["orchestration_id"]
    phase = orch.get("current_phase")
    print(f"[{tag}] waiting phase={phase}")
    if phase == "awaiting_stage5":
        print(f"[{tag}] L path: approve via /actions (Stage5 人工裁决 -> finalize 按 scope)")
        try:
            st, after = req("POST", f"/research-orchestrations/{oid}/actions",
                {"action": "approve", "comment": None}, timeout=60)
            print(f"[{tag}] approve -> {st} status={after.get('status')} phase={after.get('current_phase')}")
            if after.get("status") in ("completed", "completed_with_warnings"):
                print(f"[{tag}] RESULT accept ok: {after.get('status')}")
                ok = 0
            else:
                print(f"[{tag}] FAIL: approve produced status={after.get('status')}")
                ok = 4
        except urllib.error.HTTPError as e:
            print(f"[{tag}] approve HTTPError {e.code}: {e.read().decode()[:300]}")
            ok = 5
        print(f"[{tag}] FINAL ok={ok}")
        sys.exit(ok)

    st, review = req("GET", f"/research-orchestrations/{oid}/backflow-review", timeout=30)
    barriers = review.get("acceptance_barriers") or []
    scope = review.get("impact_scope")
    print(f"[{tag}] backflow-review: scope={scope} barriers={barriers}")
    print(f"[{tag}] reason={review.get('reason')}")
    ok = 0
    if barriers:
        print(f"[{tag}] EXPECTED blocked (REPORT 级缺陷)：接受应被拒")
        try:
            req("POST", f"/research-orchestrations/{oid}/backflow-review/actions",
                {"action": "accept", "comment": None}, timeout=30)
            print(f"[{tag}] FAIL: accept unexpectedly allowed")
            ok = 3
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            print(f"[{tag}] accept rejected as expected ({e.code}): {body[:200]}")
            try:
                req("POST", f"/research-orchestrations/{oid}/backflow-review/actions",
                    {"action": "cancel", "comment": None}, timeout=30)
                print(f"[{tag}] cancelled (clean terminal)")
            except Exception as ce:
                print(f"[{tag}] cancel failed: {ce}")
            ok = 0
    else:
        print(f"[{tag}] EXPECTED acceptable（章节级/无缺陷）：接受应成功")
        try:
            st, after = req("POST", f"/research-orchestrations/{oid}/backflow-review/actions",
                            {"action": "accept", "comment": None}, timeout=30)
            print(f"[{tag}] accept -> {st} status={after.get('status')} phase={after.get('current_phase')}")
            if after.get("status") in ("completed", "completed_with_warnings"):
                print(f"[{tag}] RESULT accept ok: {after.get('status')}")
                ok = 0
            else:
                print(f"[{tag}] FAIL: accept produced status={after.get('status')}")
                ok = 4
        except urllib.error.HTTPError as e:
            print(f"[{tag}] accept HTTPError {e.code}: {e.read().decode()[:300]}")
            ok = 5
    print(f"[{tag}] FINAL ok={ok}")
    sys.exit(ok)


if __name__ == "__main__":
    main()
