#!/usr/bin/env python3
"""
Fix Framework OKR - Create Goals + Plans with Proper Lifecycle

This remediates the Framework Modular Architecture OKR by:
1. Creating 5 Goals (one per KR)
2. Creating 5 Plans (one per Goal)
3. Setting donald_knuth + werner_vogels as co-accountables
4. Preparing for deliberation → consensus → sign-off
"""

import json
import uuid
from datetime import datetime


FRAMEWORK_OKR_SPEC = {
    "okr_id": "okr_framework_modular",
    "title": "Executive Agents Framework - Modular Architecture",
    "description": "Relocate custom executive agent code from hermes-agent to proper modular framework",
    "accountable_primary": "donald_knuth",
    "accountable_partner": "werner_vogels",
    "consulted": ["jeff_dean", "margaret_hamilton"],
    "timeline_start": "2026-05-26T00:00:00",
    "timeline_end": "2026-06-15T23:59:59",
    "key_results": [
        {
            "id": "kr_framework_001",
            "title": "Framework Modularization",
            "description": "Extract core orchestration from Hermes, create proper Python package structure",
            "success_criteria": "100% of framework code isolated from platform",
            "owner": "donald_knuth"
        },
        {
            "id": "kr_framework_002",
            "title": "Code Migration",
            "description": "Move 12 files from hermes-agent to framework",
            "success_criteria": "0 breaking changes, all tests pass",
            "owner": "donald_knuth"
        },
        {
            "id": "kr_framework_003",
            "title": "Dependency Management",
            "description": "Framework independent, hermes depends on framework",
            "success_criteria": "Framework installable as standalone package",
            "owner": "werner_vogels"
        },
        {
            "id": "kr_framework_004",
            "title": "Integration Layer",
            "description": "Clean APIs, tool integration, CLI wrappers",
            "success_criteria": "Clean separation with discoverable interfaces",
            "owner": "werner_vogels"
        },
        {
            "id": "kr_framework_005",
            "title": "Testing & Verification",
            "description": "Framework tests pass standalone, integration tests pass",
            "success_criteria": "95%+ coverage, all scenarios green",
            "owner": "donald_knuth"
        }
    ]
}


GOALS = [
    {
        "id": "g_framework_001",
        "parent_okr_id": "okr_framework_modular",
        "parent_kr_id": "kr_framework_001",
        "title": "Framework Modularization",
        "description": "Extract orchestration, establish module boundaries, create Python package",
        "kpis": [
            "100% framework code isolated from hermes",
            "Module boundaries clearly defined",
            "setup.py and pyproject.toml created"
        ],
        "acceptance_tests": [
            "Framework imports work without hermes installed",
            "All modules have clear, documented boundaries",
            "Package successfully builds and installs"
        ],
        "tier": "division",
        "accountable": "donald_knuth",
        "partner": "werner_vogels"
    },
    {
        "id": "g_framework_002",
        "parent_okr_id": "okr_framework_modular",
        "parent_kr_id": "kr_framework_002",
        "title": "Code Migration",
        "description": "Move core implementations from hermes to framework with backward compatibility",
        "kpis": [
            "12 files migrated successfully",
            "Import shims created for backward compatibility",
            "0 breaking changes to hermes"
        ],
        "acceptance_tests": [
            "okr_atomic_creation.py moved and working",
            "kanban_worker_executive_agent_actor.py moved and working",
            "All hermes tests pass with framework imports"
        ],
        "tier": "division",
        "accountable": "donald_knuth",
        "partner": "werner_vogels"
    },
    {
        "id": "g_framework_003",
        "parent_okr_id": "okr_framework_modular",
        "parent_kr_id": "kr_framework_003",
        "title": "Dependency Management",
        "description": "Make framework independent, establish unidirectional dependency",
        "kpis": [
            "Framework has zero hermes dependencies",
            "Hermes depends on framework (unidirectional)",
            "Framework available on PyPI"
        ],
        "acceptance_tests": [
            "Framework pip install succeeds without hermes",
            "Framework imports work in isolation",
            "PyPI package upload successful"
        ],
        "tier": "division",
        "accountable": "werner_vogels",
        "partner": "donald_knuth"
    },
    {
        "id": "g_framework_004",
        "parent_okr_id": "okr_framework_modular",
        "parent_kr_id": "kr_framework_004",
        "title": "Integration Layer",
        "description": "Create clean APIs for hermes integration, document interfaces",
        "kpis": [
            "Public API documented and stable",
            "Hermes integration via clean interfaces only",
            "All integration points tested"
        ],
        "acceptance_tests": [
            "API documentation complete",
            "Hermes tool integration verified",
            "Integration tests pass"
        ],
        "tier": "division",
        "accountable": "werner_vogels",
        "partner": "donald_knuth"
    },
    {
        "id": "g_framework_005",
        "parent_okr_id": "okr_framework_modular",
        "parent_kr_id": "kr_framework_005",
        "title": "Testing & Verification",
        "description": "Comprehensive testing of framework standalone and integrated",
        "kpis": [
            "95%+ code coverage achieved",
            "Framework tests pass standalone",
            "Hermes tests pass with framework",
            "Integration tests verify both work together"
        ],
        "acceptance_tests": [
            "Coverage report shows 95%+",
            "Framework test suite passes without hermes",
            "All scenario tests pass",
            "Hermes test suite still passes"
        ],
        "tier": "division",
        "accountable": "donald_knuth",
        "partner": "margaret_hamilton"
    }
]


PLANS = [
    {
        "id": "plan_framework_001",
        "parent_goal_id": "g_framework_001",
        "parent_okr_id": "okr_framework_modular",
        "title": "Framework Modularization Plan",
        "description": "Extract orchestration code and establish module structure",
        "steps": [
            {
                "title": "Design module structure and boundaries",
                "hours": 8,
                "owner": "donald_knuth"
            },
            {
                "title": "Create framework repo structure",
                "hours": 6,
                "owner": "donald_knuth"
            },
            {
                "title": "Create orchestration module (okr, goal, plan)",
                "hours": 12,
                "owner": "donald_knuth"
            },
            {
                "title": "Create agents module",
                "hours": 12,
                "owner": "donald_knuth"
            },
            {
                "title": "Create storage module",
                "hours": 8,
                "owner": "werner_vogels"
            },
            {
                "title": "Create domain module",
                "hours": 10,
                "owner": "werner_vogels"
            },
            {
                "title": "Create setup.py and pyproject.toml",
                "hours": 6,
                "owner": "donald_knuth"
            },
            {
                "title": "Set up framework-only tests",
                "hours": 8,
                "owner": "donald_knuth"
            }
        ],
        "estimated_hours": 70,
        "status": "proposed",
        "accountable": "donald_knuth",
        "partner": "werner_vogels"
    },
    {
        "id": "plan_framework_002",
        "parent_goal_id": "g_framework_002",
        "parent_okr_id": "okr_framework_modular",
        "title": "Code Migration Plan",
        "description": "Move implementations from hermes to framework",
        "steps": [
            {
                "title": "Move okr_atomic_creation.py",
                "hours": 6,
                "owner": "donald_knuth"
            },
            {
                "title": "Move kanban_worker_executive_agent_actor.py",
                "hours": 6,
                "owner": "donald_knuth"
            },
            {
                "title": "Move domain models",
                "hours": 8,
                "owner": "werner_vogels"
            },
            {
                "title": "Move storage backends",
                "hours": 8,
                "owner": "werner_vogels"
            },
            {
                "title": "Create import shims for backward compatibility",
                "hours": 6,
                "owner": "donald_knuth"
            },
            {
                "title": "Update hermes imports",
                "hours": 8,
                "owner": "donald_knuth"
            },
            {
                "title": "Migrate cron jobs to reference framework",
                "hours": 6,
                "owner": "werner_vogels"
            },
            {
                "title": "Migrate CLI commands to use framework API",
                "hours": 8,
                "owner": "werner_vogels"
            },
            {
                "title": "Run full hermes test suite",
                "hours": 4,
                "owner": "donald_knuth"
            }
        ],
        "estimated_hours": 60,
        "status": "proposed",
        "accountable": "donald_knuth",
        "partner": "werner_vogels"
    },
    {
        "id": "plan_framework_003",
        "parent_goal_id": "g_framework_003",
        "parent_okr_id": "okr_framework_modular",
        "title": "Dependency Management Plan",
        "description": "Establish framework independence and package distribution",
        "steps": [
            {
                "title": "Remove framework dependencies from hermes/requirements.txt",
                "hours": 4,
                "owner": "werner_vogels"
            },
            {
                "title": "Add framework as local/editable dependency in hermes",
                "hours": 6,
                "owner": "werner_vogels"
            },
            {
                "title": "Test PyPI packaging (build, upload, install)",
                "hours": 8,
                "owner": "werner_vogels"
            },
            {
                "title": "Create semantic versioning strategy",
                "hours": 6,
                "owner": "donald_knuth"
            },
            {
                "title": "Document framework upgrade path",
                "hours": 6,
                "owner": "donald_knuth"
            }
        ],
        "estimated_hours": 30,
        "status": "proposed",
        "accountable": "werner_vogels",
        "partner": "donald_knuth"
    },
    {
        "id": "plan_framework_004",
        "parent_goal_id": "g_framework_004",
        "parent_okr_id": "okr_framework_modular",
        "title": "Integration Layer Plan",
        "description": "Create clean interfaces and documentation",
        "steps": [
            {
                "title": "Define public API surface for hermes integration",
                "hours": 8,
                "owner": "werner_vogels"
            },
            {
                "title": "Document all integration points",
                "hours": 10,
                "owner": "werner_vogels"
            },
            {
                "title": "Create hermes tool integration wrappers",
                "hours": 12,
                "owner": "werner_vogels"
            },
            {
                "title": "Create CLI wrapper functions",
                "hours": 8,
                "owner": "donald_knuth"
            },
            {
                "title": "Test all integration scenarios",
                "hours": 12,
                "owner": "donald_knuth"
            }
        ],
        "estimated_hours": 50,
        "status": "proposed",
        "accountable": "werner_vogels",
        "partner": "donald_knuth"
    },
    {
        "id": "plan_framework_005",
        "parent_goal_id": "g_framework_005",
        "parent_okr_id": "okr_framework_modular",
        "title": "Testing & Verification Plan",
        "description": "Achieve comprehensive test coverage and validation",
        "steps": [
            {
                "title": "Set up framework test suite (pytest with coverage)",
                "hours": 8,
                "owner": "margaret_hamilton"
            },
            {
                "title": "Write framework unit tests",
                "hours": 20,
                "owner": "margaret_hamilton"
            },
            {
                "title": "Write framework integration tests",
                "hours": 16,
                "owner": "margaret_hamilton"
            },
            {
                "title": "Verify framework tests pass standalone",
                "hours": 4,
                "owner": "donald_knuth"
            },
            {
                "title": "Run hermes test suite with framework dependency",
                "hours": 6,
                "owner": "donald_knuth"
            },
            {
                "title": "Analyze coverage, identify gaps",
                "hours": 6,
                "owner": "margaret_hamilton"
            }
        ],
        "estimated_hours": 60,
        "status": "proposed",
        "accountable": "donald_knuth",
        "partner": "margaret_hamilton"
    }
]


def print_remediation_plan():
    print("=" * 80)
    print("FRAMEWORK OKR REMEDIATION PLAN")
    print("=" * 80)
    print()
    print("OBJECTIVE:", FRAMEWORK_OKR_SPEC["title"])
    print("ACCOUNTABLES:", f"{FRAMEWORK_OKR_SPEC['accountable_primary']} + {FRAMEWORK_OKR_SPEC['accountable_partner']}")
    print()
    print("GOALS CREATED (5):")
    for goal in GOALS:
        print(f"  ✓ {goal['title']}")
    print()
    print("PLANS CREATED (5):")
    for plan in PLANS:
        print(f"  ✓ {plan['title']}")
    print(f"     Total hours: {plan['estimated_hours']}")
    print()
    print("TOTAL EFFORT: {} hours".format(sum(p['estimated_hours'] for p in PLANS)))
    print()
    print("NEXT STEPS:")
    print("1. ✅ Goals + Plans created")
    print("2. ⏳ Deliberation: donald_knuth + werner_vogels reason through feasibility")
    print("3. ⏳ Consensus: jeff_dean + margaret_hamilton consult and approve")
    print("4. ⏳ Sign-off: Accountables mark in_progress")
    print("5. ⏳ Kanban conversion: Plans decomposed to tasks")
    print()
    print("STATUS: Ready for deliberation phase")
    print("=" * 80)


if __name__ == "__main__":
    print_remediation_plan()
    
    # Output JSON for import into kanban
    output = {
        "okr": FRAMEWORK_OKR_SPEC,
        "goals": GOALS,
        "plans": PLANS,
        "total_goals": len(GOALS),
        "total_plans": len(PLANS),
        "total_hours": sum(p['estimated_hours'] for p in PLANS),
        "status": "ready_for_deliberation"
    }
    
    with open("/tmp/framework_okr_remediation.json", "w") as f:
        json.dump(output, f, indent=2)
    
    print("\n📋 Remediation data saved to: /tmp/framework_okr_remediation.json")
