"""Tool Profile Manager 单元测试"""

import pytest

from nexus.agent.tool_profiles import (
    ProfileName,
    ToolProfile,
    _MINIMAL_TOOLS,
    _CODING_TOOLS,
    _MESSAGING_TOOLS,
)
from nexus.agent.types import ToolDefinition, ToolRiskLevel


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_tool(
    name: str,
    risk: ToolRiskLevel = ToolRiskLevel.LOW,
) -> ToolDefinition:
    async def _noop(**kw):
        return "ok"
    return ToolDefinition(
        name=name,
        description=f"Test tool {name}",
        parameters={"type": "object", "properties": {}},
        handler=_noop,
        risk_level=risk,
    )


@pytest.fixture
def all_tools() -> list[ToolDefinition]:
    """模拟完整工具集"""
    return [
        _make_tool("read_local_file", ToolRiskLevel.LOW),
        _make_tool("write_local_file", ToolRiskLevel.MEDIUM),
        _make_tool("system_run", ToolRiskLevel.MEDIUM),
        _make_tool("background_run", ToolRiskLevel.HIGH),
        _make_tool("delete_page", ToolRiskLevel.HIGH),
        _make_tool("skill_create", ToolRiskLevel.MEDIUM),
        _make_tool("dispatch_subagent", ToolRiskLevel.MEDIUM),
        _make_tool("document_append_block", ToolRiskLevel.LOW),
        _make_tool("compact", ToolRiskLevel.LOW),
        _make_tool("admin_reset", ToolRiskLevel.CRITICAL),
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestProfileFactory:
    """工厂方法测试"""

    def test_minimal_profile(self):
        p = ToolProfile.minimal()
        assert p.name == "minimal"
        assert p.include is not None
        assert p.max_risk_level == ToolRiskLevel.LOW

    def test_coding_profile(self):
        p = ToolProfile.coding()
        assert p.name == "coding"
        assert "system_run" in p.include
        assert "skill_create" in p.include

    def test_messaging_profile(self):
        p = ToolProfile.messaging()
        assert p.name == "messaging"
        assert "dispatch_subagent" in p.include

    def test_full_profile(self):
        p = ToolProfile.full()
        assert p.name == "full"
        assert p.include is None  # 不限制

    def test_custom_profile(self):
        p = ToolProfile.custom(
            include={"a", "b"},
            exclude={"b"},
            max_risk_level=ToolRiskLevel.MEDIUM,
        )
        assert p.name == "custom"
        assert p.include == frozenset({"a", "b"})
        assert "b" in p.exclude

    def test_from_name(self):
        for name in ("minimal", "coding", "messaging", "full"):
            p = ToolProfile.from_name(name)
            assert p.name == name

    def test_from_name_unknown_raises(self):
        with pytest.raises(ValueError, match="未知"):
            ToolProfile.from_name("nonexistent")


class TestProfileFilter:
    """过滤逻辑测试"""

    def test_full_passes_all(self, all_tools):
        p = ToolProfile.full()
        filtered = p.filter(all_tools)
        assert len(filtered) == len(all_tools)

    def test_include_filter(self, all_tools):
        p = ToolProfile.custom(include={"read_local_file", "compact"})
        filtered = p.filter(all_tools)
        names = {t.name for t in filtered}
        assert names == {"read_local_file", "compact"}

    def test_exclude_takes_priority(self, all_tools):
        """exclude 优先于 include"""
        p = ToolProfile.custom(
            include={"read_local_file", "compact"},
            exclude={"compact"},
        )
        filtered = p.filter(all_tools)
        names = {t.name for t in filtered}
        assert names == {"read_local_file"}

    def test_max_risk_level_filter(self, all_tools):
        """max_risk_level 过滤高风险工具"""
        p = ToolProfile.custom(max_risk_level=ToolRiskLevel.MEDIUM)
        filtered = p.filter(all_tools)
        for t in filtered:
            assert t.risk_level in (ToolRiskLevel.LOW, ToolRiskLevel.MEDIUM)

    def test_minimal_blocks_write_tools(self, all_tools):
        p = ToolProfile.minimal()
        filtered = p.filter(all_tools)
        names = {t.name for t in filtered}
        assert "write_local_file" not in names
        assert "system_run" not in names

    def test_coding_includes_execution(self, all_tools):
        p = ToolProfile.coding()
        filtered = p.filter(all_tools)
        names = {t.name for t in filtered}
        assert "system_run" in names
        assert "skill_create" in names


class TestProfileMerge:
    """Profile 合并测试"""

    def test_merge_intersects_include(self):
        p1 = ToolProfile.custom(include={"a", "b", "c"})
        p2 = ToolProfile.custom(include={"b", "c", "d"})
        merged = p1.merge(p2)
        assert merged.include == frozenset({"b", "c"})

    def test_merge_unions_exclude(self):
        p1 = ToolProfile.custom(exclude={"x"})
        p2 = ToolProfile.custom(exclude={"y"})
        merged = p1.merge(p2)
        assert merged.exclude == frozenset({"x", "y"})

    def test_merge_with_full(self):
        """full + custom → custom 的 include"""
        p1 = ToolProfile.full()
        p2 = ToolProfile.custom(include={"a", "b"})
        merged = p1.merge(p2)
        assert merged.include == frozenset({"a", "b"})

    def test_merge_risk_takes_stricter(self):
        p1 = ToolProfile.custom(max_risk_level=ToolRiskLevel.HIGH)
        p2 = ToolProfile.custom(max_risk_level=ToolRiskLevel.LOW)
        merged = p1.merge(p2)
        assert merged.max_risk_level == ToolRiskLevel.LOW


class TestProfileSerialization:
    """序列化测试"""

    def test_to_dict(self):
        p = ToolProfile.minimal()
        d = p.to_dict()
        assert d["name"] == "minimal"
        assert isinstance(d["include"], list)
        assert d["max_risk_level"] == "low"

    def test_to_dict_full(self):
        p = ToolProfile.full()
        d = p.to_dict()
        assert d["include"] is None
        assert d["max_risk_level"] is None


class TestBuiltinProfileConsistency:
    """内置 Profile 一致性检查"""

    def test_coding_is_superset_of_minimal(self):
        assert _MINIMAL_TOOLS.issubset(_CODING_TOOLS)

    def test_messaging_is_superset_of_minimal(self):
        assert _MINIMAL_TOOLS.issubset(_MESSAGING_TOOLS)
