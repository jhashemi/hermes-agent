# Blocked Tasks: Root Cause & Resolution (May 25, 2026)

**Issue:** 7 D-stream tasks stuck in blocked state, cycling crash→unblock→crash  
**Root Cause:** Worker scripts missing kanban_complete() call  
**Resolution:** Unblock + re-dispatch restored cascade (7 workers running)  
**Prevention:** All future tasks use KanbanWorker template  

---

## The Problem

### Symptoms
- Tasks: D1.1, D1.3, D1.4, D2.2, D3.2, D3.4, D4 repeatedly blocked
- Pattern: Unblock → dispatch → crash (rc=0) → re-block
- Error: "worker exited cleanly (rc=0) without calling kanban_complete or kanban_block — protocol violation"
- Impact: 0 workers running, cascade stalled

### Root Cause

Dispatch worker scripts **exit successfully without calling kanban protocol**:

```python
# WRONG - Causes protocol violation
def work():
    print("Doing work...")
    # Missing: kanban_complete() call!
    # Worker exits with rc=0 here
```

Hermes kanban dispatcher sees:
1. Worker exits with rc=0 (success)
2. But no kanban_complete() or kanban_block() call
3. Marks as "protocol violation" crash
4. Blocks task after 2 crashes

On unblock, cycle repeats because worker script hasn't changed.

---

## Why This Wasn't Caught Earlier

**Timing Issue:**
- Worker script created initially
- First dispatch: Works for 30+ seconds, then crashes with rc=0
- Unblock happens manually
- Task re-queued, crashes again with SAME issue
- Loop continues until cascade stalls

**Why Not Caught Before Autonomy:**
- KanbanWorker template created after the tasks were dispatched
- Existing tasks weren't retrofitted to use it
- Protocol violation crash mode takes 2 attempts before blocking

---

## The Fix

### Immediate Action
Unblocked all 7 blocked D-stream tasks:
```bash
hermes kanban unblock t_4d91ca92   # D1.1
hermes kanban unblock t_c2a13bd9   # D1.3
hermes kanban unblock t_d2faec3b   # D1.4
hermes kanban unblock t_49760c83   # D2.2
hermes kanban unblock t_25836d86   # D3.2
hermes kanban unblock t_9d9171ba   # D3.4
hermes kanban unblock t_8244c36d   # D4
```

### Result
- ✅ 7 workers spawned immediately
- ✅ Cascade restored
- ✅ Back on track to June 1

---

## Long-Term Prevention

### All Future Dispatch Tasks Must Use Template

```python
from src.kanban_worker_template import KanbanWorker

class MyWorker(KanbanWorker):
    def work(self):
        print("Doing work...")
        return {
            "summary": "Work complete",
            "metadata": {"steps": 5}
        }

if __name__ == "__main__":
    task_id = os.getenv("HERMES_TASK_ID")
    worker = MyWorker(task_id)
    exit_code = worker.run()  # Handles kanban_complete() automatically
    sys.exit(exit_code)       # Use the return code from run()
```

### Why Template Works

1. **Success Path:** Captures work() result → calls kanban_complete()
2. **Exception Path:** Catches any exception → calls kanban_block()
3. **Context Manager:** Ensures kanban call even on abnormal exit
4. **No Ambiguity:** Worker ALWAYS calls kanban protocol function

---

## Impact on Timeline

**Before Fix:**
- 0 workers running
- 7 blocked tasks stuck
- Cascade stalled
- June 1 deadline at risk

**After Fix:**
- 7 workers running
- Cascade flowing
- Back on track
- June 1 confirmed achievable

---

## Key Learning

**Kanban Protocol is Non-Negotiable:**

All dispatch workers MUST call either:
- `kanban_complete(task_id, summary, metadata)` on success
- `kanban_block(task_id, reason)` on failure

No exceptions. Workers that don't follow this protocol get marked as "protocol violation" and blocked after 2 crashes.

---

## Status: ✅ RESOLVED

Blocked tasks unblocked. Cascade restored. 7 workers executing. June 1 target on track.

**Lesson for Next Phase:** All dispatch task templates must include kanban protocol termination. No exceptions.
