from __future__ import annotations

from ui.app_main.aspice_scenario_builder import build_feature_scenario


def test_build_feature_scenario_from_feature_payload() -> None:
    record = {
        "sample_id": "SCN-COMPLIANCE-BATTERY-001",
        "scenario_type": "aspice_compliance_inspection",
        "feature": {
            "id": "FEAT-BATTERY-WARNING-001",
            "name": "Low Battery Warning",
            "description": "The rider shall be warned when the vehicle battery state of charge is low.",
            "feature_owner": "System Engineering",
            "source_evidence": ["EV-CONF-BATTERY-001"],
            "safety_relevance": "TBD",
            "status": "Draft",
        },
        "input_evidence": [
            {
                "source_id": "EV-CONF-BATTERY-001",
                "source_type": "confluence",
                "title": "Battery Warning Feature Description",
            }
        ],
        "input_requirements": [
            {
                "id": "STK-REQ-BAT-001",
                "feature_id": "FEAT-BATTERY-WARNING-001",
                "type": "stakeholder_requirement",
                "statement": "The rider shall be warned when the vehicle battery state of charge is low.",
                "source_evidence": ["EV-CONF-BATTERY-001"],
                "status": "Draft",
            },
            {
                "id": "SYS-REQ-BAT-001",
                "feature_id": "FEAT-BATTERY-WARNING-001",
                "type": "system_requirement",
                "aspice_process": "SYS.2",
                "statement": "The system shall warn the rider when the battery is low.",
                "derived_from": ["STK-REQ-BAT-001"],
                "source_evidence": ["EV-CONF-BATTERY-001"],
                "verification_criteria": "",
                "safety_relevance": "TBD",
                "review_state": "Not reviewed",
                "status": "Draft",
            },
            {
                "id": "SWE-REQ-BAT-001",
                "feature_id": "FEAT-BATTERY-WARNING-001",
                "type": "software_requirement",
                "aspice_process": "SWE.1",
                "statement": "The display software shall activate LOW_BATTERY_TELLTALE when batterySocPercent is below configuredLowBatteryThreshold.",
                "derived_from": ["SYS-REQ-BAT-001"],
                "source_evidence": ["EV-CODE-BATTERY-001"],
                "verification_criteria": "Set batterySocPercent below configuredLowBatteryThreshold and verify the telltale.",
                "safety_relevance": "TBD",
                "review_state": "Not reviewed",
                "status": "Draft",
            },
        ],
        "input_trace_links": [
            {
                "id": "TRACE-BAT-STK-SYS-001",
                "from": "STK-REQ-BAT-001",
                "to": "SYS-REQ-BAT-001",
                "link_type": "derived_by",
            }
        ],
        "input_test_cases": [],
        "expected_inspection_findings": [],
    }

    scenario = build_feature_scenario(record)

    assert scenario["scenario_id"] == "SCN-COMPLIANCE-BATTERY-001"
    assert scenario["feature_id"] == "FEAT-BATTERY-WARNING-001"
    assert scenario["feature_name"] == "Low Battery Warning"
    assert scenario["scenario_type"] == "aspice_compliance_inspection"
    assert scenario["behavior_scenarios"] == []

    process_steps = scenario["process_steps"]
    assert [step["process_step"] for step in process_steps] == [
        "capture_feature_context",
        "capture_stakeholder_requirement",
        "derive_system_requirement",
        "derive_software_requirement",
    ]
    assert process_steps[2]["aspice_process"] == "SYS.2"
    assert process_steps[3]["aspice_process"] == "SWE.1"

    gap_rules = {gap["rule_id"] for gap in scenario["gaps"]}
    assert "RULE-SYS2-HAS-VERIFICATION" in gap_rules
    assert "RULE-SYS2-IS-SPECIFIC" in gap_rules
    assert "RULE-SWE1-HAS-UPSTREAM-SYS2" not in gap_rules

    assert scenario["recommendations"]
    assert "verification criteria" in " ".join(scenario["recommendations"]).lower()


def test_build_feature_scenario_includes_behavior_scenarios_when_present() -> None:
    record = {
        "sample_id": "SCN-FEATURE-BEHAVIOR-001",
        "feature": {
            "id": "FEAT-LOW-BATTERY",
            "name": "Low Battery Warning",
            "behaviors": [
                {
                    "behavior_id": "BEH-001",
                    "name": "Display warning when battery is low",
                    "trigger": "Battery SoC falls below threshold",
                    "preconditions": ["Vehicle is powered on"],
                    "main_flow": ["Detect low state", "Show warning"],
                    "alternative_flows": [],
                    "postconditions": ["Warning remains active"],
                    "verification_idea": "Simulate low SoC and check warning display",
                }
            ],
        },
        "input_evidence": [],
        "input_requirements": [],
        "input_trace_links": [],
        "input_test_cases": [],
        "expected_inspection_findings": [],
    }

    scenario = build_feature_scenario(record)

    assert scenario["behavior_scenarios"] == [
        {
            "behavior_id": "BEH-001",
            "name": "Display warning when battery is low",
            "trigger": "Battery SoC falls below threshold",
            "preconditions": ["Vehicle is powered on"],
            "main_flow": ["Detect low state", "Show warning"],
            "alternative_flows": [],
            "postconditions": ["Warning remains active"],
            "verification_idea": "Simulate low SoC and check warning display",
            "source": "feature.behaviors",
        }
    ]