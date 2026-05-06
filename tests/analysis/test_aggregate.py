"""
Tests for the tool call aggregator module.

Test Cases from Story 3.2:
1. Count by Name: Returns count for each unique tool name
2. Status Breakdown: Success vs failure counts
3. Duration: Average duration per tool type
4. Empty Input: Returns empty stats, not error
"""

from __future__ import annotations

import pytest

from dev_agent_lens.analysis.aggregate import (
    AggregateStats,
    ToolStats,
    aggregate_tools,
    get_slowest_tools,
    get_top_tools,
)


class TestCountByName:
    """Test Case 1: Returns count for each unique tool name."""

    def test_counts_each_tool_separately(self):
        """Each unique tool name gets its own count."""
        spans = [
            {"name": "Claude_Code_Tool_Read", "span_kind": "TOOL", "status_code": "OK"},
            {"name": "Claude_Code_Tool_Read", "span_kind": "TOOL", "status_code": "OK"},
            {"name": "Claude_Code_Tool_Write", "span_kind": "TOOL", "status_code": "OK"},
            {"name": "Claude_Code_Tool_Bash", "span_kind": "TOOL", "status_code": "OK"},
        ]

        stats = aggregate_tools(spans)

        assert stats.tools["Read"].total_calls == 2
        assert stats.tools["Write"].total_calls == 1
        assert stats.tools["Bash"].total_calls == 1

    def test_strips_claude_code_tool_prefix(self):
        """Tool names have Claude_Code_Tool_ prefix stripped."""
        spans = [
            {"name": "Claude_Code_Tool_Glob", "span_kind": "TOOL", "status_code": "OK"},
        ]

        stats = aggregate_tools(spans)

        assert "Glob" in stats.tools
        assert "Claude_Code_Tool_Glob" not in stats.tools

    def test_total_tool_calls_tracked(self):
        """Total tool calls is sum of all individual tools."""
        spans = [
            {"name": "Claude_Code_Tool_Read", "span_kind": "TOOL", "status_code": "OK"},
            {"name": "Claude_Code_Tool_Write", "span_kind": "TOOL", "status_code": "OK"},
            {"name": "Claude_Code_Tool_Grep", "span_kind": "TOOL", "status_code": "OK"},
        ]

        stats = aggregate_tools(spans)

        assert stats.total_tool_calls == 3


class TestStatusBreakdown:
    """Test Case 2: Success vs failure counts."""

    def test_counts_successes(self):
        """Successful tool calls are counted."""
        spans = [
            {"name": "Claude_Code_Tool_Read", "span_kind": "TOOL", "status_code": "OK"},
            {"name": "Claude_Code_Tool_Read", "span_kind": "TOOL", "status_code": "OK"},
        ]

        stats = aggregate_tools(spans)

        assert stats.tools["Read"].success_count == 2
        assert stats.tools["Read"].failure_count == 0
        assert stats.total_successes == 2

    def test_counts_failures(self):
        """Failed tool calls are counted."""
        spans = [
            {"name": "Claude_Code_Tool_Read", "span_kind": "TOOL", "status_code": "ERROR"},
            {"name": "Claude_Code_Tool_Read", "span_kind": "TOOL", "status_code": "OK"},
        ]

        stats = aggregate_tools(spans)

        assert stats.tools["Read"].success_count == 1
        assert stats.tools["Read"].failure_count == 1
        assert stats.total_failures == 1

    def test_success_rate_calculation(self):
        """Success rate is calculated correctly."""
        spans = [
            {"name": "Claude_Code_Tool_Read", "span_kind": "TOOL", "status_code": "OK"},
            {"name": "Claude_Code_Tool_Read", "span_kind": "TOOL", "status_code": "OK"},
            {"name": "Claude_Code_Tool_Read", "span_kind": "TOOL", "status_code": "ERROR"},
            {"name": "Claude_Code_Tool_Read", "span_kind": "TOOL", "status_code": "ERROR"},
        ]

        stats = aggregate_tools(spans)

        assert stats.tools["Read"].success_rate == 50.0  # 2/4 = 50%

    def test_overall_success_rate(self):
        """Overall success rate across all tools."""
        spans = [
            {"name": "Claude_Code_Tool_Read", "span_kind": "TOOL", "status_code": "OK"},
            {"name": "Claude_Code_Tool_Write", "span_kind": "TOOL", "status_code": "ERROR"},
        ]

        stats = aggregate_tools(spans)

        assert stats.overall_success_rate == 50.0


class TestDuration:
    """Test Case 3: Average duration per tool type."""

    def test_calculates_average_duration(self):
        """Average duration is calculated from start/end times."""
        spans = [
            {
                "name": "Claude_Code_Tool_Read",
                "span_kind": "TOOL",
                "status_code": "OK",
                "start_time": "2024-01-01T10:00:00.000",
                "end_time": "2024-01-01T10:00:01.000",  # 1 second
            },
            {
                "name": "Claude_Code_Tool_Read",
                "span_kind": "TOOL",
                "status_code": "OK",
                "start_time": "2024-01-01T10:00:00.000",
                "end_time": "2024-01-01T10:00:03.000",  # 3 seconds
            },
        ]

        stats = aggregate_tools(spans)

        # Average of 1000ms and 3000ms = 2000ms
        assert stats.tools["Read"].average_duration_ms == 2000.0

    def test_total_duration_tracked(self):
        """Total duration is sum of all durations."""
        spans = [
            {
                "name": "Claude_Code_Tool_Read",
                "span_kind": "TOOL",
                "status_code": "OK",
                "start_time": "2024-01-01T10:00:00.000",
                "end_time": "2024-01-01T10:00:01.000",
            },
            {
                "name": "Claude_Code_Tool_Read",
                "span_kind": "TOOL",
                "status_code": "OK",
                "start_time": "2024-01-01T10:00:00.000",
                "end_time": "2024-01-01T10:00:02.000",
            },
        ]

        stats = aggregate_tools(spans)

        assert stats.tools["Read"].total_duration_ms == 3000.0

    def test_handles_missing_timestamps(self):
        """Missing timestamps don't cause errors."""
        spans = [
            {"name": "Claude_Code_Tool_Read", "span_kind": "TOOL", "status_code": "OK"},
        ]

        stats = aggregate_tools(spans)

        assert stats.tools["Read"].average_duration_ms == 0.0


class TestEmptyInput:
    """Test Case 4: Returns empty stats, not error."""

    def test_empty_list_returns_empty_stats(self):
        """Empty span list returns empty stats."""
        stats = aggregate_tools([])

        assert stats.total_tool_calls == 0
        assert stats.total_successes == 0
        assert stats.total_failures == 0
        assert len(stats.tools) == 0

    def test_non_tool_spans_ignored(self):
        """Non-tool spans are not counted."""
        spans = [
            {"name": "litellm_request", "span_kind": "LLM", "status_code": "OK"},
            {"name": "internal_prompt", "span_kind": "", "status_code": "OK"},
        ]

        stats = aggregate_tools(spans)

        assert stats.total_tool_calls == 0


class TestToolStats:
    """Tests for ToolStats dataclass."""

    def test_to_dict(self):
        """to_dict returns serializable dictionary."""
        stats = ToolStats(
            name="Read",
            total_calls=10,
            success_count=8,
            failure_count=2,
            total_duration_ms=5000.0,
            durations=[500.0] * 10,
        )

        result = stats.to_dict()

        assert result["name"] == "Read"
        assert result["total_calls"] == 10
        assert result["success_rate"] == 80.0
        assert result["average_duration_ms"] == 500.0


class TestAggregateStats:
    """Tests for AggregateStats dataclass."""

    def test_to_dict(self):
        """to_dict returns serializable dictionary."""
        stats = AggregateStats(
            total_tool_calls=5,
            total_successes=4,
            total_failures=1,
        )

        result = stats.to_dict()

        assert result["total_tool_calls"] == 5
        assert result["overall_success_rate"] == 80.0


class TestTopTools:
    """Tests for get_top_tools function."""

    def test_returns_top_n_by_call_count(self):
        """Returns tools sorted by call count."""
        spans = [
            {"name": "Claude_Code_Tool_Read", "span_kind": "TOOL", "status_code": "OK"},
            {"name": "Claude_Code_Tool_Read", "span_kind": "TOOL", "status_code": "OK"},
            {"name": "Claude_Code_Tool_Read", "span_kind": "TOOL", "status_code": "OK"},
            {"name": "Claude_Code_Tool_Write", "span_kind": "TOOL", "status_code": "OK"},
            {"name": "Claude_Code_Tool_Write", "span_kind": "TOOL", "status_code": "OK"},
            {"name": "Claude_Code_Tool_Bash", "span_kind": "TOOL", "status_code": "OK"},
        ]

        stats = aggregate_tools(spans)
        top = get_top_tools(stats, n=2)

        assert len(top) == 2
        assert top[0]["name"] == "Read"
        assert top[0]["total_calls"] == 3
        assert top[1]["name"] == "Write"


class TestSlowestTools:
    """Tests for get_slowest_tools function."""

    def test_returns_tools_by_avg_duration(self):
        """Returns tools sorted by average duration."""
        spans = [
            {
                "name": "Claude_Code_Tool_Bash",
                "span_kind": "TOOL",
                "status_code": "OK",
                "start_time": "2024-01-01T10:00:00.000",
                "end_time": "2024-01-01T10:00:10.000",  # 10 seconds
            },
            {
                "name": "Claude_Code_Tool_Read",
                "span_kind": "TOOL",
                "status_code": "OK",
                "start_time": "2024-01-01T10:00:00.000",
                "end_time": "2024-01-01T10:00:01.000",  # 1 second
            },
        ]

        stats = aggregate_tools(spans)
        slowest = get_slowest_tools(stats, n=2)

        assert slowest[0]["name"] == "Bash"
        assert slowest[0]["average_duration_ms"] == 10000.0


# =============================================================================
# ENG2-734: Skill Aggregation Tests
# =============================================================================


class TestSkillAggregation:
    """Tests for skill usage tracking in aggregate_tools (ENG2-734)."""

    def test_skill_call_count(self):
        """Tracks total skill call count."""
        import json

        spans = [
            {
                "name": "Claude_Code_Tool_Skill",
                "span_kind": "TOOL",
                "status_code": "OK",
                "raw_attributes": {
                    "attributes": {
                        "input": {
                            "value": json.dumps({
                                "type": "tool_use",
                                "name": "Skill",
                                "input": {"skill": "testbed"}
                            })
                        }
                    }
                }
            },
            {
                "name": "Claude_Code_Tool_Skill",
                "span_kind": "TOOL",
                "status_code": "OK",
                "raw_attributes": {
                    "attributes": {
                        "input": {
                            "value": json.dumps({
                                "type": "tool_use",
                                "name": "Skill",
                                "input": {"skill": "draft-project"}
                            })
                        }
                    }
                }
            },
            {"name": "Claude_Code_Tool_Read", "span_kind": "TOOL", "status_code": "OK"},
        ]

        stats = aggregate_tools(spans)

        assert stats.skill_call_count == 2

    def test_skills_used_list(self):
        """Tracks unique skills used."""
        import json

        spans = [
            {
                "name": "Claude_Code_Tool_Skill",
                "span_kind": "TOOL",
                "status_code": "OK",
                "raw_attributes": {
                    "attributes": {
                        "input": {
                            "value": json.dumps({
                                "type": "tool_use",
                                "name": "Skill",
                                "input": {"skill": "testbed"}
                            })
                        }
                    }
                }
            },
            {
                "name": "Claude_Code_Tool_Skill",
                "span_kind": "TOOL",
                "status_code": "OK",
                "raw_attributes": {
                    "attributes": {
                        "input": {
                            "value": json.dumps({
                                "type": "tool_use",
                                "name": "Skill",
                                "input": {"skill": "testbed"}  # Duplicate
                            })
                        }
                    }
                }
            },
            {
                "name": "Claude_Code_Tool_Skill",
                "span_kind": "TOOL",
                "status_code": "OK",
                "raw_attributes": {
                    "attributes": {
                        "input": {
                            "value": json.dumps({
                                "type": "tool_use",
                                "name": "Skill",
                                "input": {"skill": "draft-project"}
                            })
                        }
                    }
                }
            },
        ]

        stats = aggregate_tools(spans)

        assert len(stats.skills_used) == 2
        assert set(stats.skills_used) == {"testbed", "draft-project"}

    def test_skill_breakdown(self):
        """Tracks call count per skill."""
        import json

        spans = [
            {
                "name": "Claude_Code_Tool_Skill",
                "span_kind": "TOOL",
                "status_code": "OK",
                "raw_attributes": {
                    "attributes": {
                        "input": {
                            "value": json.dumps({
                                "type": "tool_use",
                                "name": "Skill",
                                "input": {"skill": "testbed"}
                            })
                        }
                    }
                }
            },
            {
                "name": "Claude_Code_Tool_Skill",
                "span_kind": "TOOL",
                "status_code": "OK",
                "raw_attributes": {
                    "attributes": {
                        "input": {
                            "value": json.dumps({
                                "type": "tool_use",
                                "name": "Skill",
                                "input": {"skill": "testbed"}
                            })
                        }
                    }
                }
            },
            {
                "name": "Claude_Code_Tool_Skill",
                "span_kind": "TOOL",
                "status_code": "OK",
                "raw_attributes": {
                    "attributes": {
                        "input": {
                            "value": json.dumps({
                                "type": "tool_use",
                                "name": "Skill",
                                "input": {"skill": "draft-project"}
                            })
                        }
                    }
                }
            },
        ]

        stats = aggregate_tools(spans)

        assert stats.skill_breakdown == {"testbed": 2, "draft-project": 1}

    def test_no_skills_empty_stats(self):
        """No skill spans results in empty skill stats."""
        spans = [
            {"name": "Claude_Code_Tool_Read", "span_kind": "TOOL", "status_code": "OK"},
            {"name": "Claude_Code_Tool_Bash", "span_kind": "TOOL", "status_code": "OK"},
        ]

        stats = aggregate_tools(spans)

        assert stats.skill_call_count == 0
        assert stats.skills_used == []
        assert stats.skill_breakdown == {}

    def test_skill_stats_in_to_dict(self):
        """Skill stats are included in to_dict() output."""
        import json

        spans = [
            {
                "name": "Claude_Code_Tool_Skill",
                "span_kind": "TOOL",
                "status_code": "OK",
                "raw_attributes": {
                    "attributes": {
                        "input": {
                            "value": json.dumps({
                                "type": "tool_use",
                                "name": "Skill",
                                "input": {"skill": "testbed"}
                            })
                        }
                    }
                }
            },
        ]

        stats = aggregate_tools(spans)
        output = stats.to_dict()

        assert "skills_used" in output
        assert "skill_call_count" in output
        assert "skill_breakdown" in output
        assert output["skill_call_count"] == 1
        assert output["skills_used"] == ["testbed"]
        assert output["skill_breakdown"] == {"testbed": 1}
