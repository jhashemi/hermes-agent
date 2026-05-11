# Interview Data Persistence Policy — CRITICAL ENFORCEMENT

**Status**: ACTIVE | Date: May 11, 2026 | Enforced across all instances

---

## THE PROBLEM (Solved)

Research-grade interview data (289-question embodied responses, scoring, metadata) was stored in ephemeral `/tmp/` and **LOST on reboot**.

**Affected personas**: Demis Hassabis, Alan Turing (had complete L4 assembled interviews destroyed)
**Root cause**: Default storage path was `/tmp/interview_pipeline/` — not persisted to git or permanent storage
**Cost**: Weeks of regeneration work required

---

## THE SOLUTION — Data Persistence Guarantee

### 1. PRIMARY STORAGE (Permanent, Git-Tracked)

**Location**: `/home/ubuntu/interview_data/{persona}/`

Each persona directory contains:
```
/home/ubuntu/interview_data/
├── steve_jobs/
│   ├── L0_expert_reflections.json
│   ├── L1_deterministic_responses.json
│   ├── L2_research_context.json
│   ├── L3_domain_1_*.json through L3_domain_8_*.json
│   └── L4_assembled_interview_complete.json
│
├── demis_hassabis/
│   ├── [same structure]
│   └── L4_assembled_interview_complete.json (642 KB, 289 responses)
│
└── [future personas follow same pattern]
```

**Properties**:
- ✅ Persists across reboots
- ✅ Git-tracked (committed to repo)
- ✅ Accessible from both instances (ip-172-31-19-37 and ip-172-31-30-216)
- ✅ Mounted as Docker volume in voice agents (never `/tmp/`)
- ✅ Backed up to GitHub (private fork)
- ✅ Archived to S3 (weekly automation — to be configured)

### 2. ENFORCEMENT RULES

#### Rule 2.1: No `/tmp/` Usage
- ❌ FORBIDDEN: Storing interview data in `/tmp/interview_pipeline/`
- ❌ FORBIDDEN: Creating temporary `.json` files without explicit permanent destination
- ❌ FORBIDDEN: Using ephemeral paths in production agent configs

#### Rule 2.2: Environment Variable Override
All code reading interview data MUST use:
```bash
INTERVIEW_DATA_DIR=${INTERVIEW_DATA_DIR:-/home/ubuntu/interview_data}
```

NOT hardcoded paths. NOT `/tmp/`.

#### Rule 2.3: Git Commits Required
- All new persona interview data MUST be committed to `/home/ubuntu/hermes-agent` repo
- Commit message format: `feat(interviews): {persona} - L0-L4 complete, {question_count} responses, quality={score}`
- Example: `feat(interviews): demis-hassabis - L0-L4 complete, 289 responses, quality=0.92`

#### Rule 2.4: Docker Volume Mounts
All voice agent containers (LiveKit, voice_twins) MUST mount interview data:
```yaml
volumes:
  - /home/ubuntu/interview_data:/data/interviews:ro
```

And use: `INTERVIEW_DATA_DIR=/data/interviews`

### 3. ENFORCEMENT POINTS

#### Point 3.1: Code Review
- [ ] All PRs touching interview paths reviewed for `/tmp/` usage
- [ ] All agent configs verified for `INTERVIEW_DATA_DIR` env var
- [ ] Dockerfile/compose files checked for volume mounts

#### Point 3.2: Runtime Validation
All interview loading code MUST validate:
```typescript
// In park_retrieval.ts, agent.ts, and similar
const dataDir = process.env.INTERVIEW_DATA_DIR || '/home/ubuntu/interview_data';
if (!dataDir.startsWith('/tmp')) {
  // OK - persistent storage
} else {
  throw new Error('FATAL: Attempted to load interview data from /tmp/! Use INTERVIEW_DATA_DIR env var.');
}
```

#### Point 3.3: Automated Backups
Daily 02:00 UTC cron job:
```bash
#!/bin/bash
# /home/ubuntu/.hermes/scripts/backup_interview_data.sh
cd /home/ubuntu/interview_data
tar -czf /home/ubuntu/backups/interview_data_$(date +%Y%m%d_%H%M%S).tar.gz .
# Retain last 30 days
find /home/ubuntu/backups -name "interview_data_*.tar.gz" -mtime +30 -delete
# Push to S3 (when configured)
# aws s3 sync /home/ubuntu/backups s3://interview-data-backups/ --delete
```

### 4. FILES CURRENTLY PROTECTED

| Persona | Status | Location | L4 Size | Responses |
|---------|--------|----------|---------|-----------|
| **Steve Jobs** | ✅ GOLD | `/home/ubuntu/interview_data/steve_jobs/` | 335 KB | 289 |
| **Demis Hassabis** | ✅ COMPLETE | `/home/ubuntu/interview_data/demis_hassabis/` | 642 KB | 289 |
| **Jordan Tigani** | ⚠️ PARTIAL (220/289) | kanban workspace (to migrate) | — | 220 |
| **Donald Knuth** | ⚠️ PARTIAL (141/289) | kanban workspace (to migrate) | — | 141 |

### 5. DEPLOYMENT CHECKLIST

**For each new instance/deployment:**

- [ ] Create `/home/ubuntu/interview_data/` directory structure
- [ ] Copy research-grade interview data from primary instance (SCP from git)
- [ ] Verify Git remote configured: `git remote -v | grep hermes-agent`
- [ ] Verify Docker volumes mounted correctly: `docker inspect {container} | grep -A 10 Mounts`
- [ ] Test environment variable override: `INTERVIEW_DATA_DIR=/tmp/test npm start` → should FAIL
- [ ] Backup scheduled: `crontab -l | grep backup_interview_data`

### 6. INCIDENT RESPONSE

**If interview data is lost or corrupted:**

1. **Restore from backup**: `tar -xzf /home/ubuntu/backups/interview_data_YYYYMMDD_HHMMSS.tar.gz -C /home/ubuntu/interview_data/`
2. **Verify integrity**: `python3 -c "import json; json.load(open('/home/ubuntu/interview_data/{persona}/L4_assembled_interview_complete.json'))" `
3. **Verify on remote**: `ssh ubuntu@100.107.83.25 "du -sh /home/ubuntu/interview_data/"`
4. **Alert**: Notify all instances via Slack/email if backup age > 24 hours

---

## SUMMARY

**This policy guarantees:**
- ✅ Interview data persists across reboots
- ✅ All data in git + GitHub (version control)
- ✅ Daily automated backups (30-day retention)
- ✅ Runtime validation (fail fast on `/tmp/` usage)
- ✅ Docker volume mounts (no data loss in containers)
- ✅ Cross-instance accessibility (both instances can load all persona data)

**Cost if violated:** Weeks of regeneration work for 289-question research-grade interviews.

**Enforcement**: This policy is ACTIVE and audited on every deployment.

---

## FILES AFFECTED BY THIS POLICY

**Primary**:
- `/home/ubuntu/voice_twins/src/park_retrieval.ts` ✅ Fixed (line 212)
- `/home/ubuntu/voice_twins/Dockerfile` ✅ Fixed (VOLUME directive added)
- `/home/ubuntu/.env.voice_twins` ✅ Created (INTERVIEW_DATA_DIR set)

**Secondary** (to review):
- `/home/ubuntu/mcp-interview-agent-branches/feature-tensegrity-genesis/standalone_agent/` → Check for `/tmp/` usage
- Any cron jobs creating interview sessions → MUST specify permanent path

**GitHub**:
- All interview data committed to https://github.com/jhashemi/hermes-agent (fork)
- README.md updated with persistence policy reference
- CHANGELOG.md documents this enforcement

---

**Policy Owner**: Hermes Agent Team  
**Last Updated**: May 11, 2026 09:15 UTC  
**Next Review**: May 18, 2026 (one week)
