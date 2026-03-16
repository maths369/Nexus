---
name: API Integration Bootstrap
description: 为新的外部 API 集成快速搭建认证、连通性测试和调用脚本。
tags: api, integration, http, webhook
---

# API Integration Bootstrap

在用户要连接新的 API、Webhook 或第三方服务时使用。

## 默认工作流

1. 明确目标 API、认证方式、目标动作。
2. 优先用 `system_run` 做最小连通性测试：
   - `curl`
   - `python -c`
   - 简短的 `httpx` 脚本
3. 一旦确认认证和请求结构可用，再把稳定调用方式沉淀成 Skill 或工具化流程。
4. 对写操作、删除操作、财务或生产数据写入，先提醒风险并二次确认。

## 输出要求

- 给出已验证的请求示例
- 给出所需环境变量/凭据位点
- 给出下一步如何把它沉淀为长期能力
