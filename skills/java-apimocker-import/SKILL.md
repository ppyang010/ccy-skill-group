---
name: java-apimocker-import
description: 解析 Java Spring Controller 接口并导入 api-mocker，支持接口路径反向定位代码。触发场景：用户要求将本地 Java 接口导入 api-mocker；或输入 @GetMapping("/xxx")、/xxx 让你定位对应 Controller 代码。仅在 exam-ms-edge、exam-ms-edge-app、exam-ms-todmanage 中定位。
---

# Java API Mocker Import

用于两类任务：
1. `locate`：根据接口路径定位 Controller 代码位置，并同时解析入参/出参为 Swagger 2.0 格式。
2. `import`：解析 Controller 接口后上传到 `open/api/autoImport/:groupId`，上传体使用 `isOrigin=false`、`apis=接口地址`、`json=Swagger JSON 字符串`。

## 固定扫描范围

仅扫描以下模块中的 `*Controller.java`：
- `exam-ms-edge`
- `exam-ms-edge-app`
- `exam-ms-todmanage`

## 输入格式

`--input` 支持两种形式：
- 注解形式：`@GetMapping("/admin/exam-past/sprint/catalogue/list")`
- 路径形式：`/admin/exam-past/sprint/catalogue/list`

脚本会自动标准化：去空格、补全前导 `/`、压缩重复斜杠。

## 配置文件

默认配置文件：`/Users/ccy/.agents/skills/java-apimocker-import/config.json`

字段：
- `userToken`: 用户导入 token（第一次录入，后续复用）
- `projects`: 项目配置数组，支持多项目
  - `name`: 项目名称（用于 `--project` 选择）
  - `groupId`: 上传 URL 中的 groupId
  - `projectToken`: api-mocker 分组 token
  - `uploadBaseUrl`: 例如 `https://f2e.dxy.net/mock`
  - `uploadPathTemplate`: 例如 `/open/api/autoImport/:groupId`
- `defaultProject`: 默认项目名（可选）

## 使用方式

```bash
# 代码定位
python3 /Users/ccy/.agents/skills/java-apimocker-import/scripts/import_java_apis.py \
  --mode locate \
  --input '@GetMapping("/admin/exam-past/sprint/catalogue/list")'

# 导入（全量）
python3 /Users/ccy/.agents/skills/java-apimocker-import/scripts/import_java_apis.py \
  --mode import \
  --project exam-tod \
  --import-type auto

# 导入（仅单一路径）
python3 /Users/ccy/.agents/skills/java-apimocker-import/scripts/import_java_apis.py \
  --mode import \
  --project exam-tod \
  --input '/admin/exam-past/sprint/catalogue/list' \
  --import-type 2

# 预览（不上传）
python3 /Users/ccy/.agents/skills/java-apimocker-import/scripts/import_java_apis.py \
  --mode import \
  --project exam-tod \
  --dry-run
```

## 重要行为

- 只返回 Controller 命中，不返回 Feign/Service/Client。
- `locate` 输出中会包含：
  - 每个命中接口的 `swagger`（对象）和 `swaggerJson`（字符串）
  - 所有命中接口汇总后的 `swagger`（对象）和 `swaggerJson`（字符串）
- 方法级 `@RequestMapping` 未声明 `method` 会被跳过，并输出告警。
- `import` 上传 payload 关键字段固定为：
  - `isOrigin: false`
  - `apis: "/你的接口路径"`（多接口时使用逗号拼接）
  - `json: "<swagger-json-string>"`
- `import` 执行方式：脚本内部会构建等价 `curl` 请求并执行上传。
- `importType=auto`：
  - 若你已用 `mock-api-mcp` 判断是否存在，可通过 `--api-exists true|false` 传入，自动映射到 `2/0`。
  - 若无法判断，会要求手动输入 `0` 或 `2`。
