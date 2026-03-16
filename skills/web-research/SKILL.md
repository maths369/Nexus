---
name: web-research
description: 通过联网搜索和浏览器工具进行外部信息查询、网页核实和事实补充。
tags: web, search, browser, research
---

# Web Research

在用户要求联网查询、外部检索、网页核实、最新信息补充时，优先使用这个 skill。

## 目标

用受控的联网检索方式获取外部信息，而不是凭模型记忆猜测。

## 默认流程

1. 优先调用 `search_web`
2. 如果搜索结果不够清楚，再用：
   - `browser_navigate`
   - `browser_extract_text`
   - `browser_screenshot`
3. 最终回答要明确区分：
   - 搜索得到的信息
   - 你的归纳总结

## 推荐调用

### A. 普通联网搜索
```text
search_web(
  query="<查询词>",
  engine="bing",
  max_chars=3000
)
```

### B. 需要打开具体页面继续核实
```text
browser_navigate(url="<页面 URL>")
browser_extract_text()
```

## 适用场景

1. 查询实时信息
2. 验证官网/产品页内容
3. 对某个外部网页做信息抽取

## 注意事项

1. 外部搜索结果可能有噪音，必要时继续打开具体页面核实
2. 如果信息可能随时间变化，应明确说明是联网查询结果
3. 如果浏览器工具不可用，不要假装查过网

