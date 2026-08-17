"""Production validation: real user path - company name only."""

import json
import sys
import time

import httpx

BASE = "http://127.0.0.1:8003/api/v1"


def run_company(client: httpx.Client, company: str) -> dict:
    # 1) 创建任务：最小输入 = 公司名（模拟前端固定参数）
    resp = client.post(
        f"{BASE}/tasks",
        json={
            "company_query": company,
            "require_plan_approval": False,
        },
    )
    print(f"  [1] create task: {resp.status_code}")
    if resp.status_code != 201:
        return {
            "company": company,
            "result": "create_failed",
            "stage": "create",
            "detail": resp.text[:200],
        }
    task = resp.json()
    task_id = task["task_id"]
    print(
        f"      task_id={task_id} modules={task['modules']} "
        f"dates={task['research_start_date']}~{task['research_end_date']}"
    )

    # 2) 启动自动研究（planner 瞬时 malformed → 422 → 有界重试）
    resp = None
    for attempt in range(6):
        try:
            resp = client.post(f"{BASE}/tasks/{task_id}/orchestrations", timeout=90)
        except httpx.TimeoutException:
            resp = None
        print(
            "  [2] start orchestration "
            f"(attempt {attempt + 1}): {resp.status_code if resp else 'timeout'}"
        )
        if resp is not None and resp.status_code in (201, 200, 202):
            break
        if resp is not None and resp.status_code == 422:
            time.sleep(10)
            continue
        if resp is None:
            time.sleep(10)
            continue
        if resp.status_code != 422:
            break
    if resp is None or resp.status_code not in (201, 200, 202):
        return {
            "company": company,
            "result": "start_failed",
            "stage": "start",
            "detail": (resp.text if resp else "timeout")[:200],
        }
    orch = resp.json()
    print(f"      orchestration_id={orch['orchestration_id']} status={orch['status']}")

    # 3) 轮询：记录每个阶段
    phases: list[str] = []
    last = None
    deadline = time.time() + 60 * 50
    terminal = None
    while time.time() < deadline:
        time.sleep(20)
        try:
            r = client.get(f"{BASE}/tasks/{task_id}/orchestrations/current", timeout=30)
            if r.status_code != 200:
                continue
            st = r.json()
        except httpx.TimeoutException:
            continue
        phase = st.get("current_phase")
        status = st.get("status")
        if phase != last:
            print(
                f"      [{time.strftime('%H:%M:%S')}] {status} | {phase}"
                + (f" | reason={st.get('manual_reason')}" if st.get("manual_reason") else "")
            )
            phases.append(f"{status}:{phase}")
            last = phase
        if status in ("completed", "failed", "cancelled", "waiting_human"):
            terminal = status
            break
    else:
        terminal = "timeout"

    # 4) 结果快照
    result = {"company": company, "result": terminal, "phases": phases}
    r = client.get(f"{BASE}/tasks/{task_id}/report", timeout=30)
    if r.status_code == 200:
        rep = r.json()
        result["sections"] = rep.get("section_count")
    r = client.get(f"{BASE}/tasks/{task_id}/sources?limit=50", timeout=30)
    if r.status_code == 200:
        src = r.json()
        result["sources"] = src.get("total")
        result["source_types"] = sorted({i.get("document_type") for i in src.get("items", [])})
    r = client.get(f"{BASE}/tasks/{task_id}/evidence?limit=1", timeout=30)
    if r.status_code == 200:
        result["evidence"] = r.json().get("total")
    return result


def main() -> None:
    companies = sys.argv[1:] or ["宁德时代", "贵州茅台", "招商银行", "海康威视", "比亚迪"]
    client = httpx.Client(timeout=60)
    results = []
    for company in companies:
        print(f"\n===== {company} =====")
        results.append(run_company(client, company))
    client.close()
    print("\n===== SUMMARY =====")
    for res in results:
        print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
