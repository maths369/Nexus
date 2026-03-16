"""
Node Card — 节点身份与能力声明

每个节点在加入 Mesh 时注册一张 Node Card，声明自己的：
1. 身份信息（node_id, node_type, platform）
2. LLM Provider 接入能力
3. 本地能力（tools）
4. 资源约束（CPU, GPU, 内存, 磁盘）
5. 可用性（在线时段, 是否间歇, 最大任务时长）

参考: A2A Protocol 的 Agent Card
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field
from typing import Any

import yaml


class NodeType(str, enum.Enum):
    """节点类型"""
    HUB = "hub"           # 永远在线的中心节点
    EDGE = "edge"         # 边缘节点（如 MacBook），可能间歇离线
    MOBILE = "mobile"     # 移动节点（如 iPhone），轻量交互


@dataclass
class ProviderSpec:
    """节点可用的 LLM Provider"""
    name: str
    model: str
    via: str = "api"                  # "api" | "local"
    context_length: int = 0           # 本地 LLM 时的 context length
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class CapabilitySpec:
    """节点提供的能力"""
    capability_id: str
    description: str
    tools: list[str] = field(default_factory=list)
    requires_user_interaction: bool = False
    exclusive: bool = False            # 只能在此节点执行
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class ResourceSpec:
    """节点资源约束"""
    cpu_cores: int = 0
    memory_gb: float = 0
    gpu: str = ""
    gpu_memory_gb: float = 0
    disk_free_gb: float = 0
    battery_powered: bool = False


@dataclass
class AvailabilitySpec:
    """节点可用性"""
    schedule: str = "24/7"             # 可用时段
    intermittent: bool = False         # 是否可能随时离线
    max_task_duration_seconds: int = 0 # 0 = 无限制
    preferred_role: str = ""           # 偏好角色提示（如 "input_output"）


@dataclass
class NodeCard:
    """
    节点能力声明卡片。

    类比 A2A 的 Agent Card，但面向物理节点。
    """
    node_id: str
    node_type: NodeType
    display_name: str
    platform: str                      # "linux" | "macos" | "ios"
    arch: str = "x86_64"

    providers: list[ProviderSpec] = field(default_factory=list)
    capabilities: list[CapabilitySpec] = field(default_factory=list)
    resources: ResourceSpec = field(default_factory=ResourceSpec)
    availability: AvailabilitySpec = field(default_factory=AvailabilitySpec)

    # 版本号，每次能力变更时递增
    version: int = 1

    def capability_ids(self) -> set[str]:
        """返回所有 capability id"""
        return {cap.capability_id for cap in self.capabilities}

    def tool_names(self) -> set[str]:
        """返回所有 tool 名称"""
        tools: set[str] = set()
        for cap in self.capabilities:
            tools.update(cap.tools)
        return tools

    def find_capability(self, capability_id: str) -> CapabilitySpec | None:
        """查找指定 capability"""
        for cap in self.capabilities:
            if cap.capability_id == capability_id:
                return cap
        return None

    def find_tools_for_capability(self, capability_id: str) -> list[str]:
        """返回指定 capability 的工具列表"""
        cap = self.find_capability(capability_id)
        return list(cap.tools) if cap else []

    def has_local_llm(self) -> bool:
        """是否有本地 LLM"""
        return any(p.via == "local" for p in self.providers)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典"""
        return {
            "node_id": self.node_id,
            "node_type": self.node_type.value,
            "display_name": self.display_name,
            "platform": self.platform,
            "arch": self.arch,
            "providers": [
                {
                    "name": p.name,
                    "model": p.model,
                    "via": p.via,
                    "context_length": p.context_length,
                    "properties": p.properties,
                }
                for p in self.providers
            ],
            "capabilities": [
                {
                    "capability_id": c.capability_id,
                    "description": c.description,
                    "tools": c.tools,
                    "requires_user_interaction": c.requires_user_interaction,
                    "exclusive": c.exclusive,
                    "properties": c.properties,
                }
                for c in self.capabilities
            ],
            "resources": {
                "cpu_cores": self.resources.cpu_cores,
                "memory_gb": self.resources.memory_gb,
                "gpu": self.resources.gpu,
                "gpu_memory_gb": self.resources.gpu_memory_gb,
                "disk_free_gb": self.resources.disk_free_gb,
                "battery_powered": self.resources.battery_powered,
            },
            "availability": {
                "schedule": self.availability.schedule,
                "intermittent": self.availability.intermittent,
                "max_task_duration_seconds": self.availability.max_task_duration_seconds,
                "preferred_role": self.availability.preferred_role,
            },
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NodeCard:
        """从字典反序列化"""
        providers = [
            ProviderSpec(
                name=p.get("name", ""),
                model=p.get("model", ""),
                via=p.get("via", "api"),
                context_length=int(p.get("context_length") or 0),
                properties=dict(p.get("properties") or {}),
            )
            for p in data.get("providers", [])
        ]
        capabilities = [
            CapabilitySpec(
                capability_id=c.get("capability_id", ""),
                description=c.get("description", ""),
                tools=list(c.get("tools") or []),
                requires_user_interaction=bool(c.get("requires_user_interaction")),
                exclusive=bool(c.get("exclusive")),
                properties=dict(c.get("properties") or {}),
            )
            for c in data.get("capabilities", [])
        ]
        res = data.get("resources") or {}
        avail = data.get("availability") or {}
        return cls(
            node_id=data.get("node_id", ""),
            node_type=NodeType(data.get("node_type", "edge")),
            display_name=data.get("display_name", ""),
            platform=data.get("platform", ""),
            arch=data.get("arch", "x86_64"),
            providers=providers,
            capabilities=capabilities,
            resources=ResourceSpec(
                cpu_cores=int(res.get("cpu_cores") or 0),
                memory_gb=float(res.get("memory_gb") or 0),
                gpu=str(res.get("gpu") or ""),
                gpu_memory_gb=float(res.get("gpu_memory_gb") or 0),
                disk_free_gb=float(res.get("disk_free_gb") or 0),
                battery_powered=bool(res.get("battery_powered")),
            ),
            availability=AvailabilitySpec(
                schedule=str(avail.get("schedule") or "24/7"),
                intermittent=bool(avail.get("intermittent")),
                max_task_duration_seconds=int(avail.get("max_task_duration_seconds") or 0),
                preferred_role=str(avail.get("preferred_role") or ""),
            ),
            version=int(data.get("version") or 1),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> NodeCard:
        return cls.from_dict(json.loads(text))

    @classmethod
    def from_yaml(cls, text: str) -> NodeCard:
        return cls.from_dict(yaml.safe_load(text) or {})

    @classmethod
    def from_yaml_file(cls, path: str) -> NodeCard:
        from pathlib import Path
        return cls.from_yaml(Path(path).read_text(encoding="utf-8"))
