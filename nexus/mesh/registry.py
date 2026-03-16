"""
Mesh Registry — 分布式能力注册表

运行在 Hub 节点上，管理所有节点的 Node Card 和能力索引。

职责：
1. 接收节点注册/注销
2. 维护节点健康状态（心跳）
3. 提供能力查询：给定需求，返回可用节点列表
4. 广播能力变更通知
5. 节点超时检测
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .node_card import CapabilitySpec, NodeCard, NodeType
from .transport import MeshTransport, MeshMessage, MessageType

logger = logging.getLogger(__name__)


@dataclass
class NodeStatus:
    """节点实时状态"""
    node_id: str
    online: bool = False
    last_heartbeat: float = 0.0        # Unix timestamp
    current_load: float = 0.0          # 0.0 ~ 1.0
    active_tasks: int = 0
    battery_level: float | None = None # 移动设备电量 0-100


@dataclass
class CapabilityEntry:
    """能力索引条目"""
    capability_id: str
    node_id: str
    description: str
    tools: list[str]
    requires_user_interaction: bool = False
    exclusive: bool = False


class MeshRegistry:
    """
    中心化能力注册表。

    以 node_id 为键存储 NodeCard 和 NodeStatus。
    维护 capability_id -> [CapabilityEntry] 的倒排索引，用于快速查询。
    """

    def __init__(
        self,
        transport: MeshTransport | None = None,
        *,
        heartbeat_timeout_seconds: float = 90.0,
    ) -> None:
        self._transport = transport
        self._heartbeat_timeout = heartbeat_timeout_seconds

        # 核心数据
        self._nodes: dict[str, NodeCard] = {}
        self._status: dict[str, NodeStatus] = {}

        # 能力倒排索引: capability_id -> [CapabilityEntry]
        self._capability_index: dict[str, list[CapabilityEntry]] = {}

        # 工具倒排索引: tool_name -> [(node_id, capability_id)]
        self._tool_index: dict[str, list[tuple[str, str]]] = {}

        # 后台任务
        self._timeout_task: asyncio.Task[None] | None = None

        # 事件回调
        self._on_node_online_callbacks: list[Any] = []
        self._on_node_offline_callbacks: list[Any] = []

    # ------------------------------------------------------------------
    # 节点管理
    # ------------------------------------------------------------------

    async def register_node(self, card: NodeCard) -> None:
        """
        注册一个节点。

        1. 存储 NodeCard
        2. 更新能力索引
        3. 标记为在线
        4. 广播通知
        """
        node_id = card.node_id
        old_card = self._nodes.get(node_id)

        self._nodes[node_id] = card
        self._status[node_id] = NodeStatus(
            node_id=node_id,
            online=True,
            last_heartbeat=time.time(),
        )

        # 重建该节点的索引
        self._rebuild_node_index(node_id, card)

        logger.info(
            "Node registered: %s (%s) type=%s capabilities=%s",
            card.display_name,
            node_id,
            card.node_type.value,
            sorted(card.capability_ids()),
        )

        # 触发回调
        for callback in self._on_node_online_callbacks:
            try:
                await callback(node_id, card)
            except Exception:
                logger.warning("on_node_online callback failed", exc_info=True)

        # 广播
        if self._transport is not None:
            msg = self._transport.make_message(
                MessageType.BROADCAST,
                "nexus/broadcast/node_online",
                {
                    "node_id": node_id,
                    "display_name": card.display_name,
                    "capabilities": sorted(card.capability_ids()),
                },
            )
            await self._transport.publish(msg.topic, msg)

    async def deregister_node(self, node_id: str) -> None:
        """注销节点"""
        card = self._nodes.pop(node_id, None)
        status = self._status.pop(node_id, None)

        # 清理索引
        self._remove_node_from_index(node_id)

        if card:
            logger.info("Node deregistered: %s (%s)", card.display_name, node_id)

            for callback in self._on_node_offline_callbacks:
                try:
                    await callback(node_id, card)
                except Exception:
                    logger.warning("on_node_offline callback failed", exc_info=True)

            if self._transport is not None:
                msg = self._transport.make_message(
                    MessageType.BROADCAST,
                    "nexus/broadcast/node_offline",
                    {"node_id": node_id, "display_name": card.display_name},
                )
                await self._transport.publish(msg.topic, msg)

    async def heartbeat(
        self,
        node_id: str,
        *,
        current_load: float = 0.0,
        active_tasks: int = 0,
        battery_level: float | None = None,
    ) -> bool:
        """
        处理心跳。

        返回 True 如果节点已注册，否则 False（节点应重新注册）。
        """
        status = self._status.get(node_id)
        if status is None:
            return False

        was_offline = not status.online
        status.online = True
        status.last_heartbeat = time.time()
        status.current_load = current_load
        status.active_tasks = active_tasks
        status.battery_level = battery_level

        if was_offline:
            card = self._nodes.get(node_id)
            logger.info("Node back online: %s", node_id)
            if card:
                for callback in self._on_node_online_callbacks:
                    try:
                        await callback(node_id, card)
                    except Exception:
                        logger.warning("on_node_online callback failed", exc_info=True)

        return True

    async def update_capabilities(
        self,
        node_id: str,
        capabilities: list[CapabilitySpec],
    ) -> bool:
        """
        更新节点能力列表。

        用于节点在运行时获得新能力（如安装了新 Skill）。
        """
        card = self._nodes.get(node_id)
        if card is None:
            return False

        old_cap_ids = card.capability_ids()
        card.capabilities = capabilities
        card.version += 1
        new_cap_ids = card.capability_ids()

        # 重建索引
        self._rebuild_node_index(node_id, card)

        added = new_cap_ids - old_cap_ids
        removed = old_cap_ids - new_cap_ids

        if added or removed:
            logger.info(
                "Capabilities updated for %s: added=%s removed=%s",
                node_id,
                sorted(added),
                sorted(removed),
            )
            if self._transport is not None:
                msg = self._transport.make_message(
                    MessageType.CAPABILITY_UPDATE,
                    "nexus/broadcast/capability_update",
                    {
                        "node_id": node_id,
                        "added": sorted(added),
                        "removed": sorted(removed),
                        "version": card.version,
                    },
                )
                await self._transport.publish(msg.topic, msg)

        return True

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_node(self, node_id: str) -> NodeCard | None:
        """获取节点 Card"""
        return self._nodes.get(node_id)

    def get_node_status(self, node_id: str) -> NodeStatus | None:
        """获取节点状态"""
        return self._status.get(node_id)

    def list_nodes(
        self,
        *,
        online_only: bool = False,
        node_type: NodeType | None = None,
    ) -> list[NodeCard]:
        """列出节点"""
        results = []
        for node_id, card in self._nodes.items():
            if online_only:
                status = self._status.get(node_id)
                if not status or not status.online:
                    continue
            if node_type is not None and card.node_type != node_type:
                continue
            results.append(card)
        return results

    def list_online_node_ids(self) -> list[str]:
        """列出所有在线节点 ID"""
        return [
            node_id
            for node_id, status in self._status.items()
            if status.online
        ]

    def query_capability(
        self,
        capability_id: str,
        *,
        online_only: bool = True,
        exclude_nodes: list[str] | None = None,
    ) -> list[CapabilityEntry]:
        """
        查询提供某能力的所有节点，按优先级排序。

        优先级：
        1. 在线节点优先
        2. Hub 节点优先（总是在线）
        3. 负载低的优先
        4. 非电池供电的优先
        """
        entries = self._capability_index.get(capability_id, [])
        excluded = set(exclude_nodes or [])

        candidates: list[tuple[tuple[int, ...], CapabilityEntry]] = []
        for entry in entries:
            if entry.node_id in excluded:
                continue
            status = self._status.get(entry.node_id)
            card = self._nodes.get(entry.node_id)
            if online_only and (not status or not status.online):
                continue

            # 排序键: (is_offline, is_battery, load_pct, not_hub)
            is_offline = 0 if (status and status.online) else 1
            is_battery = 1 if (card and card.resources.battery_powered) else 0
            load_pct = int((status.current_load if status else 0) * 100)
            not_hub = 0 if (card and card.node_type == NodeType.HUB) else 1

            candidates.append(((is_offline, not_hub, is_battery, load_pct), entry))

        candidates.sort(key=lambda x: x[0])
        return [entry for _, entry in candidates]

    def query_tool(
        self,
        tool_name: str,
        *,
        online_only: bool = True,
    ) -> list[tuple[str, str]]:
        """
        查询提供某工具的节点列表。

        返回 [(node_id, capability_id), ...]
        """
        entries = self._tool_index.get(tool_name, [])
        if not online_only:
            return list(entries)
        return [
            (node_id, cap_id)
            for node_id, cap_id in entries
            if self._is_online(node_id)
        ]

    def resolve_tools(
        self,
        required_tools: list[str],
        *,
        online_only: bool = True,
        prefer_node: str | None = None,
    ) -> dict[str, str]:
        """
        给定一组工具需求，返回 {tool_name: node_id} 的分配方案。

        策略：
        1. 如果 prefer_node 提供了该工具，优先分配
        2. 否则按 query_tool 的优先级分配
        3. 无法分配的工具返回空字符串
        """
        result: dict[str, str] = {}
        for tool_name in required_tools:
            candidates = self.query_tool(tool_name, online_only=online_only)
            if not candidates:
                result[tool_name] = ""
                continue
            if prefer_node:
                for node_id, cap_id in candidates:
                    if node_id == prefer_node:
                        result[tool_name] = node_id
                        break
                else:
                    result[tool_name] = candidates[0][0]
            else:
                result[tool_name] = candidates[0][0]
        return result

    def build_mesh_context(self, *, online_only: bool = True) -> str:
        """
        构建 Mesh 状态的文本描述，用于注入 LLM 的 System Prompt。

        输出人类可读的节点和能力列表。
        """
        nodes = self.list_nodes(online_only=online_only)
        if not nodes:
            return "当前网络中没有可用节点。"

        lines = [f"当前网络中有 {len(nodes)} 个{'在线' if online_only else ''}节点：", ""]
        for card in nodes:
            status = self._status.get(card.node_id)
            online_str = "在线" if (status and status.online) else "离线"
            load_str = f"负载 {status.current_load:.0%}" if status else "负载未知"
            tasks_str = f"{status.active_tasks} 个活跃任务" if status else ""

            lines.append(f"### {card.display_name} ({card.node_id})")
            lines.append(f"- 类型: {card.node_type.value} | 平台: {card.platform}/{card.arch}")
            lines.append(f"- 状态: {online_str} | {load_str} | {tasks_str}")

            if card.resources.gpu:
                lines.append(f"- GPU: {card.resources.gpu} ({card.resources.gpu_memory_gb}GB)")
            if card.availability.intermittent:
                lines.append(f"- 注意: 此节点可能随时离线")
            if card.availability.max_task_duration_seconds > 0:
                max_min = card.availability.max_task_duration_seconds // 60
                lines.append(f"- 最大任务时长: {max_min} 分钟")

            if card.capabilities:
                lines.append("- 能力:")
                for cap in card.capabilities:
                    interaction = " [需要用户交互]" if cap.requires_user_interaction else ""
                    exclusive = " [仅限此节点]" if cap.exclusive else ""
                    lines.append(f"  - {cap.capability_id}: {cap.description}{interaction}{exclusive}")
                    if cap.tools:
                        lines.append(f"    工具: {', '.join(cap.tools)}")

            if card.providers:
                lines.append("- LLM:")
                for p in card.providers:
                    via_str = "本地" if p.via == "local" else "API"
                    lines.append(f"  - {p.name} ({p.model}) [{via_str}]")

            lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 超时检测
    # ------------------------------------------------------------------

    async def start_timeout_monitor(self) -> None:
        """启动心跳超时监控"""
        if self._timeout_task is not None:
            return
        self._timeout_task = asyncio.create_task(self._timeout_loop())

    async def stop_timeout_monitor(self) -> None:
        """停止心跳超时监控"""
        if self._timeout_task is not None:
            self._timeout_task.cancel()
            try:
                await self._timeout_task
            except asyncio.CancelledError:
                pass
            self._timeout_task = None

    async def check_timeouts(self) -> list[str]:
        """
        检查并标记超时节点。

        返回新标记为离线的节点 ID 列表。
        """
        now = time.time()
        timed_out: list[str] = []

        for node_id, status in self._status.items():
            if not status.online:
                continue
            card = self._nodes.get(node_id)
            if card is not None and card.node_type == NodeType.HUB:
                continue
            if now - status.last_heartbeat > self._heartbeat_timeout:
                status.online = False
                timed_out.append(node_id)
                logger.warning(
                    "Node timed out: %s (last heartbeat %.1fs ago)",
                    node_id,
                    now - status.last_heartbeat,
                )

                if card:
                    for callback in self._on_node_offline_callbacks:
                        try:
                            await callback(node_id, card)
                        except Exception:
                            logger.warning("on_node_offline callback failed", exc_info=True)

        return timed_out

    async def _timeout_loop(self) -> None:
        """后台超时检测循环"""
        check_interval = max(10.0, self._heartbeat_timeout / 3)
        while True:
            try:
                await asyncio.sleep(check_interval)
                await self.check_timeouts()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning("Timeout check failed", exc_info=True)

    # ------------------------------------------------------------------
    # Transport 集成
    # ------------------------------------------------------------------

    async def setup_transport_handlers(self) -> None:
        """
        注册 Transport 消息处理器。

        Hub 节点调用此方法来自动处理来自其他节点的注册/心跳/能力更新。
        """
        if self._transport is None:
            return

        await self._transport.subscribe(
            "nexus/nodes/+/card",
            self._handle_node_card,
        )
        await self._transport.subscribe(
            "nexus/nodes/+/heartbeat",
            self._handle_heartbeat,
        )
        await self._transport.subscribe(
            "nexus/nodes/+/offline",
            self._handle_node_offline,
        )

    async def _handle_node_card(self, topic: str, message: MeshMessage) -> None:
        """处理节点注册消息"""
        card = NodeCard.from_dict(message.payload)
        await self.register_node(card)

    async def _handle_heartbeat(self, topic: str, message: MeshMessage) -> None:
        """处理心跳消息"""
        node_id = message.source_node
        ok = await self.heartbeat(
            node_id,
            current_load=float(message.payload.get("current_load", 0)),
            active_tasks=int(message.payload.get("active_tasks", 0)),
            battery_level=message.payload.get("battery_level"),
        )
        if not ok:
            logger.info(
                "Heartbeat from unknown node %s, requesting re-registration",
                node_id,
            )

    async def _handle_node_offline(self, topic: str, message: MeshMessage) -> None:
        """处理节点下线消息"""
        node_id = message.source_node
        status = self._status.get(node_id)
        if status:
            status.online = False
            logger.info("Node reported offline: %s", node_id)

            card = self._nodes.get(node_id)
            if card:
                for callback in self._on_node_offline_callbacks:
                    try:
                        await callback(node_id, card)
                    except Exception:
                        logger.warning("on_node_offline callback failed", exc_info=True)

    # ------------------------------------------------------------------
    # 事件回调注册
    # ------------------------------------------------------------------

    def on_node_online(self, callback: Any) -> None:
        """注册节点上线回调: async def callback(node_id, card)"""
        self._on_node_online_callbacks.append(callback)

    def on_node_offline(self, callback: Any) -> None:
        """注册节点离线回调: async def callback(node_id, card)"""
        self._on_node_offline_callbacks.append(callback)

    # ------------------------------------------------------------------
    # 内部索引管理
    # ------------------------------------------------------------------

    def _rebuild_node_index(self, node_id: str, card: NodeCard) -> None:
        """重建某节点的能力和工具索引"""
        self._remove_node_from_index(node_id)

        for cap in card.capabilities:
            entry = CapabilityEntry(
                capability_id=cap.capability_id,
                node_id=node_id,
                description=cap.description,
                tools=list(cap.tools),
                requires_user_interaction=cap.requires_user_interaction,
                exclusive=cap.exclusive,
            )
            if cap.capability_id not in self._capability_index:
                self._capability_index[cap.capability_id] = []
            self._capability_index[cap.capability_id].append(entry)

            for tool_name in cap.tools:
                if tool_name not in self._tool_index:
                    self._tool_index[tool_name] = []
                self._tool_index[tool_name].append((node_id, cap.capability_id))

    def _remove_node_from_index(self, node_id: str) -> None:
        """从索引中移除某节点的所有条目"""
        for cap_id in list(self._capability_index.keys()):
            self._capability_index[cap_id] = [
                e for e in self._capability_index[cap_id]
                if e.node_id != node_id
            ]
            if not self._capability_index[cap_id]:
                del self._capability_index[cap_id]

        for tool_name in list(self._tool_index.keys()):
            self._tool_index[tool_name] = [
                (nid, cid) for nid, cid in self._tool_index[tool_name]
                if nid != node_id
            ]
            if not self._tool_index[tool_name]:
                del self._tool_index[tool_name]

    def _is_online(self, node_id: str) -> bool:
        status = self._status.get(node_id)
        return bool(status and status.online)
