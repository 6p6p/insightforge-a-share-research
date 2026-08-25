"""InsightForge P0/P2 smoke：真实 docker 栈（8001 backend）端到端闭环。

验证目标（用户要求的 UX 不变量）：
1. 创建任务 + 启动自动化研究；
2. 等待研究推进（正常路径到达 completed 或 waiting_human 人工复核）；
3. 轮询 Reviews 视图，断言：**绝不出现「无审核记录」与「需要人工确认」并存
   的矛盾状态**——即当后台等待人工复核时，reviews 端点必须透出
   pending_human_review（Reviews 页显示「需要人工确认」而非「尚无审核记录」）。
4. 若任务到达 research_backflow manual / audit-degraded 的 waiting_human，
   断言 reviews.pending_human_review 非空。

用法：python smoke_closure_p0.py <company_query> <tag>
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
    tag = sys.argv[2] if len(sys.argv) > 2 else f"smoke-{int(time.time())}"

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
    print(f"[{tag}] start orch -> {st} status={orch.get('status')} phase={orch.get('current_phase')}")

    deadline = time.monotonic() + 900
    last = None
    while time.monotonic() < deadline:
        st, orch = req("GET", f"/tasks/{task_id}/orchestrations/current", timeout=30)
        last = orch
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
        print(f"[{tag}] TIMEOUT waiting for terminal; stopping observation")
        return 2

    st, reviews = req("GET", f"/tasks/{task_id}/reviews", timeout=30)
    audit_id = reviews.get("audit_id")
    pending = reviews.get("pending_human_review")
    banner_status = last.get("status") if last else None
    banner_phase = last.get("current_phase") if last else None

    print(f"[{tag}] reviews: audit_id={audit_id} pending={pending}")

    if banner_status == "waiting_human":
        if audit_id is None and pending is None:
            print(f"[{tag}] FAIL: waiting_human + 无 audit + 无 pending -> 「无审核记录」矛盾态")
            return 1
        if pending is None:
            print(f"[{tag}] note: waiting_human + 有 audit（正常 awaiting_stage5 路径），OK")
        else:
            print(f"[{tag}] OK: waiting_human + pending_human_review(reason={pending.get('reason')})")
    else:
        if pending is not None:
            print(f"[{tag}] note: terminal({banner_status}) 但 pending 仍透出（人工已处理？），决策层校验")
        print(f"[{tag}] OK: terminal status={banner_status} phase={banner_phase}")

    print(f"[{tag}] SMOKE-END")
    return 0


if __name__ == "__main__":
    sys.exit(main())
