---
name: Office Conversion
description: 处理 PPT/PPTX、DOCX、XLS/XLSX 等 Office 文件的转换、提取和归档。
tags:
- office
- conversion
- ppt
- pdf
- docx
- xlsx
keywords:
- ppt
- pptx
- pdf
- 转换
- 转成
- 转为
- office
- 文档
- 演示文稿
packages:
- python-pptx>=1.0.0
- python-docx>=1.1.0
- openpyxl>=3.1.0
- reportlab>=4.0.0
verify_imports:
- pptx
- docx
- openpyxl
- reportlab
---

# Office Conversion

在处理 Office 文件格式转换时使用这个 Skill。

## 适用任务

- PPT / PPTX 转 PDF
- DOCX 转 PDF / Markdown / 文本
- XLS / XLSX 转 CSV / Markdown 表格
- 从 Office 文件中提取结构化文本并归档到 Vault

## 默认工作流

1. 先确认源文件路径、目标格式、输出路径。
2. 优先用系统里已经存在的成熟转换器，例如 `libreoffice --headless` 或 `soffice`。
3. 如果系统转换器不可用，再用 `system_run` + Python 脚本做受控 fallback：
   - `python-pptx` 读取 PPT 结构
   - `python-docx` 读取 Word
   - `openpyxl` 读取 Excel
   - `reportlab` 生成 PDF
4. 如果 fallback 只能做到“结构化内容导出”而不是“视觉保真转换”，必须明确告诉用户。
5. 输出文件默认写到源文件同目录，并返回绝对路径。

## 执行约束

- 优先保证任务完成，不要先把问题推给用户。
- 如果当前环境缺依赖，先用 `system_run` 补足。
- 如果这个模式会反复出现，可以建议把它升级成长期正式能力。
