"""
Channel Layer — 协议适配与会话路由

职责:
1. 接收 Feishu / Web 消息
2. 通过 Session Router 进行意图分类与 session 绑定
3. 管理上下文窗口（freshness / reset / 截断）
4. 格式化回复并投递
"""
