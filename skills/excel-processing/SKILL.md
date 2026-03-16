---
name: Excel Processing
description: 处理 Excel 工作簿，必要时先启用 excel_processing capability，再列出工作表并导出 CSV。
tags: excel, csv, spreadsheet, capability
---

# Excel Processing

当用户要求读取、检查或转换 Excel 文件时，按下面流程执行。

## 目标
- 理解工作簿里有哪些 sheet
- 把指定 sheet 转成 CSV
- 在 capability 缺失时先启用 capability，而不是直接放弃

## 执行顺序
1. 先调用 `capability_status(capability_id="excel_processing")`
2. 如果未启用，调用 `capability_enable(capability_id="excel_processing")`
3. 调用 `excel_list_sheets(excel_path=...)` 识别工作表
4. 如用户未指定 sheet，先澄清；否则调用 `excel_to_csv(...)`
5. 明确返回输出 CSV 路径

## 约束
- 不要假设任意 Python package 都可安装；只使用 `capability_enable`
- 不要直接建议用户手动安装 pandas/openpyxl，除非 capability_enable 明确失败
- 如果文件路径不清晰，先确认路径

## 输出
- 工作表列表
- 导出成功/失败状态
- CSV 输出路径

