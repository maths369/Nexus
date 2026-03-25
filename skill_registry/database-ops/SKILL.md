---
name: Database Operations
description: 为新的数据库读取、模式检查、样本查询和导出任务提供受控工作流。
tags:
- database
- sql
- postgres
- mysql
- sqlite
keywords:
- 数据库
- sql
- postgres
- mysql
- sqlite
- 查询
- 导出
packages:
- sqlalchemy>=2.0.0
- psycopg[binary]>=3.2.0
- pymysql>=1.1.0
verify_imports:
- sqlalchemy
- psycopg
- pymysql
---

# Database Operations

在用户要连接数据库、读取表结构、导出样本数据或做分析前置检查时使用。

## 默认工作流

1. 先确认数据库类型、连接方式、只读/读写边界。
2. 优先进行只读探查：
   - 列 schema
   - 列表名
   - 样本查询
3. 用 `system_run` 生成最小脚本验证连通性，避免一开始就写复杂程序。
4. 除非用户明确允许，否则默认只读，不做 DDL / DML 变更。

## 输出要求

- 给出已验证的连接方式
- 给出表结构摘要
- 给出最小可复用查询脚本
