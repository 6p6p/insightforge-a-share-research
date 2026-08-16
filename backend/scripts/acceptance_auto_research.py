"""Final autonomous research acceptance script (F5).

输入任意 A 股公司名 → 自动研究 → 完整研究报告（含 provenance）。
用法: python scripts/acceptance_auto_research.py <company_name>
"""
import json
import sys
import time

import httpx

BASE = "http://127.0.0.1:8003/api/v1"


def main() -> None:
    company = sys.argv[1] if len(sys.argv) > 1 else "宁德时代"
    client = httpx.Client(timeout=60)
    today = time.strftime("%Y-%m-%d")
    start = time.strftime("%Y-%m-%d", time.localtime(time.time() - 3 * 365 * 86400))

    # 1) 创建任务：只输入公司名（无问题、基本面模块、AUTO 默认窗口）
    resp = client.post(
        f"{BASE}/tasks",
        json={
            "company_query": company,
            "research_start_date": start,
            "research_end_date": today,
            "modules": ["company_profile", "business", "financial", "risk"],
            "questions": [],
            "require_plan_approval": False,
        },
    )
    print(f"[1] create task: {resp.status_code}")
    resp.raise_for_status()
    task = resp.json()
    task_id = task["task_id"]
    print(f"    task_id={task_id} company_query={company!r} questions={task['questions']}")

    # 2) 启动自动研究（planner LLM 瞬时 malformed → 422 → 有界重试）
    resp = None
    for attempt in range(4):
        resp = client.post(f"{BASE}/tasks/{task_id}/orchestrations")
        print(f"[2] start orchestration (attempt {attempt + 1}): {resp.status_code}")
        if resp.status_code in (201, 200, 202):
            break
        if resp.status_code == 422:
            time.sleep(10)
            continue
        resp.raise_for_status()
    assert resp is not None
    resp.raise_for_status()
    orch = resp.json()
    print(f"    orchestration_id={orch['orchestration_id']} status={orch['status']}")

    # 3) 轮询进度
    last_phase = None
    deadline = time.time() + 60 * 40  # 40 分钟上限
    while time.time() < deadline:
        time.sleep(15)
        resp = client.get(f"{BASE}/tasks/{task_id}/orchestrations/current")
        resp.raise_for_status()
        state = resp.json()
        phase = state["current_phase"]
        status = state["status"]
        if phase != last_phase:
            print(f"[3] progress: status={status} phase={phase}"
                  + (f" reason={state.get('manual_reason')}" if state.get("manual_reason") else ""))
            last_phase = phase
        if status in ("completed", "failed", "cancelled"):
            break
        if status == "waiting_human":
            print("[3] waiting_human (human fallback needed):", phase, state.get("manual_reason"))
            break
    else:
        print("[3] TIMEOUT waiting for completion")
        sys.exit(2)

    # 4) 报告与证据
    resp = client.get(f"{BASE}/tasks/{task_id}/report")
    if resp.status_code == 200:
        report = resp.json()
        print(f"[4] report: report_id={report.get('report_id')} status={report.get('status')}")
        body = report.get("body") or report.get("content") or ""
        print(f"    report body length={len(body)}")
        print("    --- REPORT HEAD ---")
        print(body[:800])
    else:
        print(f"[4] report endpoint: {resp.status_code}")
    resp = client.get(f"{BASE}/tasks/{task_id}/evidence", params={"limit": 5})
    if resp.status_code == 200:
        ev = resp.json()
        sample = json.dumps(ev.get("items", [])[:1], ensure_ascii=False)[:400]
        print(f"[5] evidence: total={ev.get('total')} sample={sample}")
    resp = client.get(f"{BASE}/tasks/{task_id}/sources", params={"limit": 10})
    if resp.status_code == 200:
        src = resp.json()
        print(f"[6] sources: total={src.get('total')}")
        for item in src.get("items", [])[:10]:
            print(
                f"    - {item.get('document_type')} | {item.get('provider_key')}"
                f" | {str(item.get('title', ''))[:50]}"
            )
    client.close()


if __name__ == "__main__":
    main()
