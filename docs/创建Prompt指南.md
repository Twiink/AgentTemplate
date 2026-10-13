# 如何新增 Prompt

本指南演示如何在 AgentTemplate 中创建和管理 Prompt 模板。

## Prompt 存放位置

所有 Prompt 模板放在项目根目录的 `prompts/` 目录下，建议按 Agent 分组：

```
prompts/
├── assistant/
│   ├── system.md
│   └── examples.md
├── reviewer/
│   ├── system.md
│   └── report.md
└── shared/
    └── code_style.md
```

## 基础 Prompt

### 创建简单 Prompt

```markdown
<!-- prompts/assistant/system.md -->
你是一个通用助手，可以帮助用户完成各种任务。

## 能力

- 读写文件
- 访问网页
- 执行搜索

## 原则

1. 理解用户意图
2. 使用合适的工具
3. 给出清晰的回答
```

### 在 Agent 中使用

```python
# src/app/agents/assistant/agent.py
async def build(ctx: BuildContext):
    # 加载 Prompt（无参数）
    system_prompt, version = ctx.prompts.render("assistant/system.md", {})
    
    model = await ctx.models.chat("gpt-4o")
    tools = ctx.tools.langchain(["read_file"])
    
    return create_agent(
        model=model,
        tools=tools,
        system_message=system_prompt,
        checkpointer=ctx.checkpointer,
    )
```

## 带参数的 Prompt

### 使用占位符

```markdown
<!-- prompts/assistant/system.md -->
你是一个{role}，专注于{domain}领域。

## 你的职责

{responsibilities}

## 可用工具

{available_tools}
```

### 渲染参数

```python
async def build(ctx: BuildContext):
    system_prompt, version = ctx.prompts.render(
        "assistant/system.md",
        {
            "role": "技术助手",
            "domain": "软件开发",
            "responsibilities": "帮助开发者编写、调试和优化代码",
            "available_tools": "文件读写、代码执行、搜索",
        }
    )
    # ...
```

## 使用 Front Matter

### 添加元数据

Front Matter 使用 YAML 格式，用 `---` 包围：

```markdown
<!-- prompts/assistant/system.md -->
---
purpose: 通用助手的系统提示
variables:
  role: 角色描述
  domain: 专业领域
  responsibilities: 职责说明
  available_tools: 可用工具列表
recommended_models:
  - gpt-4o
  - claude-3-5-sonnet
temperature: 0.7
max_tokens: 2000
---

你是一个{role}，专注于{domain}领域。

## 你的职责

{responsibilities}

## 可用工具

{available_tools}
```

### Front Matter 字段

- `purpose`: 用途说明（可选）
- `variables`: 变量说明（可选，但推荐声明）
- `recommended_models`: 推荐模型列表（可选）
- `temperature`: 建议温度（可选）
- `max_tokens`: 建议输出长度（可选）

### 访问元数据

```python
system_prompt, version, metadata = ctx.prompts.render(
    "assistant/system.md",
    {"role": "助手", "domain": "开发", ...}
)

# 使用元数据
print(f"用途: {metadata.purpose}")
print(f"推荐模型: {metadata.recommended_models}")
print(f"建议温度: {metadata.temperature}")

# 根据元数据选择模型
if metadata.recommended_models:
    model_alias = metadata.recommended_models[0]
    model = await ctx.models.chat(model_alias)
```

## 复杂 Prompt

### 多部分 Prompt

```markdown
<!-- prompts/reviewer/system.md -->
---
purpose: 代码审查系统提示
variables:
  language: 编程语言
  style_guide: 代码风格指南
recommended_models:
  - claude-3-5-sonnet
  - gpt-4o
temperature: 0.3
---

你是一个专业的{language}代码审查员。

## 审查标准

请按照以下标准审查代码：

1. **正确性**: 代码是否正确实现了功能？
2. **可读性**: 代码是否清晰易懂？
3. **性能**: 是否有性能问题？
4. **安全性**: 是否存在安全隐患？

## 代码风格

遵循以下风格指南：

{style_guide}

## 输出格式

以 JSON 格式输出审查结果：

\`\`\`json
{{
  "issues": [
    {{
      "severity": "high|medium|low",
      "category": "correctness|readability|performance|security|style",
      "line": 42,
      "message": "问题描述",
      "suggestion": "修复建议"
    }}
  ],
  "score": 85,
  "summary": "总体评价"
}}
\`\`\`
```

### 使用共享片段

```markdown
<!-- prompts/shared/code_style.md -->
---
purpose: 通用代码风格指南
---

- 使用有意义的变量名
- 函数长度不超过 50 行
- 及时处理错误
- 添加必要的注释
```

在其他 Prompt 中引用：

```python
# 加载共享片段
style_guide, _ = ctx.prompts.render("shared/code_style.md", {})

# 渲染主 Prompt
system_prompt, version = ctx.prompts.render(
    "reviewer/system.md",
    {
        "language": "Python",
        "style_guide": style_guide,
    }
)
```

## Few-Shot 示例

### 内嵌示例

```markdown
<!-- prompts/assistant/system.md -->
你是一个代码生成助手。

## 示例

用户: 写一个计算斐波那契数列的函数
助手:
\`\`\`python
def fibonacci(n: int) -> int:
    if n <= 1:
        return n
    return fibonacci(n-1) + fibonacci(n-2)
\`\`\`

用户: 添加缓存优化
助手:
\`\`\`python
from functools import lru_cache

@lru_cache(maxsize=None)
def fibonacci(n: int) -> int:
    if n <= 1:
        return n
    return fibonacci(n-1) + fibonacci(n-2)
\`\`\`

## 现在开始

{task}
```

### 动态示例选择

```python
# 根据任务类型选择示例
def select_examples(task_type: str) -> str:
    examples_map = {
        "code_generation": "写一个排序函数...",
        "debugging": "找出这段代码的错误...",
        "optimization": "优化这段代码的性能...",
    }
    return examples_map.get(task_type, "")


async def build(ctx: BuildContext):
    task_type = "code_generation"  # 从输入或配置获取
    examples = select_examples(task_type)
    
    system_prompt, _ = ctx.prompts.render(
        "assistant/system.md",
        {"examples": examples}
    )
    # ...
```

## Prompt 版本管理

### 版本号生成

Prompt 版本基于完整内容（包括 front matter）的 SHA-256 哈希生成：

```python
system_prompt, version, metadata = ctx.prompts.render(...)
print(f"Prompt 版本: {version}")  # 前 16 位哈希
```

### 版本记录

版本自动写入运行上下文，并记录到数据库：

```python
# 运行完成后，可以查询使用的 Prompt 版本
run = await session.get(Run, run_id)
print(run.prompt_versions)
# {"assistant/system.md": "a1b2c3d4e5f6g7h8"}
```

### 版本追踪

在 trace 中也会记录 Prompt 版本，用于调试和审计。

## Prompt 最佳实践

### 1. 清晰的角色定义

```markdown
你是一个{role}。你的目标是{goal}。
```

### 2. 明确的指令

```markdown
## 你应该做的

- 仔细分析用户需求
- 选择合适的工具
- 给出详细的解释

## 你不应该做的

- 不要假设未说明的信息
- 不要执行危险操作
- 不要泄露系统信息
```

### 3. 结构化输出

```markdown
请按以下格式输出：

\`\`\`json
{
  "analysis": "分析结果",
  "action": "建议操作",
  "confidence": 0.95
}
\`\`\`
```

### 4. 使用元数据

声明 `variables` 让 Prompt 更易维护：

```yaml
---
variables:
  task: 任务描述
  context: 上下文信息
  constraints: 约束条件
---
```

### 5. 测试不同模型

使用 `recommended_models` 建议最适合的模型：

```yaml
---
recommended_models:
  - claude-3-5-sonnet  # 推理能力强
  - gpt-4o             # 速度快
---
```

## 调试 Prompt

### 查看渲染结果

```python
system_prompt, version, metadata = ctx.prompts.render(
    "assistant/system.md",
    {"role": "助手", "domain": "开发"}
)

print("=== Prompt ===")
print(system_prompt)
print(f"\n=== Version: {version} ===")
print(f"=== Metadata: {metadata.raw} ===")
```

### 验证参数

```python
# PromptLoader 会自动验证参数与占位符匹配
try:
    ctx.prompts.render("assistant/system.md", {"wrong_param": "value"})
except AppError as e:
    print(f"参数错误: {e.message}")
```

### 使用开发环境测试

```bash
# 启动开发服务器
make dev

# 调用 Agent 测试 Prompt
curl -X POST http://localhost:8000/api/v1/threads/test/runs/wait \
  -H "Authorization: Bearer $(cat .data/developer.key)" \
  -d '{"agent_id": "assistant", "input": {"messages": [...]}}'
```

## 多语言 Prompt

### 根据用户语言选择

```python
# prompts/assistant/system.en.md
# prompts/assistant/system.zh.md

async def build(ctx: BuildContext):
    # 从配置或请求中获取语言
    lang = "zh"  # 或 "en"
    
    system_prompt, _ = ctx.prompts.render(
        f"assistant/system.{lang}.md",
        {"role": "assistant"}
    )
    # ...
```

## 常见问题

### Prompt 文件未找到

检查：
1. 文件路径是否正确（相对于 `prompts/` 目录）
2. 文件扩展名是否正确（通常是 `.md`）
3. 文件是否存在

### 参数不匹配

错误通常是：
- 提供的参数与占位符不一致
- Front matter 声明的 `variables` 与实际占位符不符

解决：
1. 检查 Prompt 中的 `{placeholder}`
2. 确保所有占位符都提供了值
3. 不要提供额外的参数

### Front Matter 解析失败

检查：
1. YAML 语法是否正确
2. `---` 是否在文件开头
3. 缩进是否正确（使用空格，不用 Tab）

### 版本变化频繁

Prompt 的任何修改都会改变版本号。这是预期行为，用于追踪 Prompt 变更。

## 下一步

- [新增 Agent](创建Agent指南.md)
- [BuildContext API](开发指南.md#运行上下文)
- [Prompt 加载器](开发指南.md#配置与资源)
