# RAG 混合检索使用指南

## 概述

知识库模块现已支持三种检索模式：

- **vector** - 向量语义检索（默认）
- **keyword** - pg_trgm 关键词检索
- **hybrid** - 混合检索（向量 + 关键词，RRF 融合）

## 快速开始

### 1. 启用 pg_trgm 扩展

运行迁移以启用 pg_trgm 扩展：

```bash
make migrate
```

这会自动执行 `knowledge_0003_pg_trgm` 迁移，为 `chunk.content` 添加 GIN 索引。

### 2. 使用混合检索

```python
import httpx

# 混合检索
response = httpx.post(
    "http://localhost:8000/api/v1/knowledge/{knowledge_id}/search",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "query": "Python 编程语言的特点",
        "search_mode": "hybrid",  # vector, keyword, hybrid
        "top_k": 10,
        "rrf_k": 60,  # RRF 融合参数
        "rerank": False,
    }
)

results = response.json()["items"]
for item in results:
    print(f"文档：{item['document_id']}")
    print(f"相似度：{1 - item['distance']:.4f}")
    if item.get("rrf_score"):
        print(f"RRF 分数：{item['rrf_score']:.4f}")
    print(f"内容：{item['content'][:100]}...")
    print()
```

### 3. 关键词检索

适用于精确匹配和术语查找：

```python
response = httpx.post(
    "http://localhost:8000/api/v1/knowledge/{knowledge_id}/search",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "query": "Python",
        "search_mode": "keyword",
        "top_k": 10,
    }
)
```

## 检索模式对比

| 模式 | 适用场景 | 优势 | 限制 |
|---|---|---|---|
| **vector** | 语义理解、概念查询 | 理解同义词、上下文 | 对精确术语不敏感 |
| **keyword** | 精确匹配、术语查找 | 快速、精确 | 不理解语义 |
| **hybrid** | 通用查询 | 结合两者优势 | 稍慢 |

## RRF 融合

混合检索使用 **Reciprocal Rank Fusion (RRF)** 算法融合向量和关键词检索结果：

```
RRF(d) = Σ 1 / (k + rank_i(d))
```

- `k` 默认为 60，可通过 `rrf_k` 参数调整
- 较小的 `k` 值会增加前排结果的权重
- 较大的 `k` 值会更均匀地考虑所有结果

## 高级用法

### 元数据过滤

所有检索模式都支持元数据过滤：

```python
response = httpx.post(
    "http://localhost:8000/api/v1/knowledge/{knowledge_id}/search",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "query": "机器学习",
        "search_mode": "hybrid",
        "top_k": 10,
        "metadata": {
            "parser": ".pdf",
            "artifact_id": "specific-document-id",
        }
    }
)
```

### 重排器集成

混合检索支持可选的重排器：

```python
# 在 KnowledgeService 中注册重排器
async def custom_reranker(query: str, results: list) -> list:
    """自定义重排逻辑。"""
    # 实现重排算法
    return sorted_results

knowledge_service.register_reranker(custom_reranker)

# API 调用
response = httpx.post(
    "http://localhost:8000/api/v1/knowledge/{knowledge_id}/search",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "query": "Python",
        "search_mode": "hybrid",
        "rerank": True,  # 启用重排
    }
)
```

## 性能优化

### 索引优化

pg_trgm 索引会自动创建，但可以调整参数：

```sql
-- 调整相似度阈值（默认 0.1）
SET pg_trgm.similarity_threshold = 0.3;

-- 查看索引使用情况
EXPLAIN ANALYZE
SELECT content FROM chunk
WHERE content % 'search query'
ORDER BY similarity(content, 'search query') DESC
LIMIT 10;
```

### 并发检索

混合检索自动并发执行向量和关键词检索：

```python
# 内部实现（自动）
vector_task = self.search(...)
keyword_task = self.keyword_search(...)
vector_results, keyword_results = await asyncio.gather(
    vector_task, keyword_task
)
```

## 故障排除

### pg_trgm 扩展未启用

错误：`operator does not exist: text % unknown`

解决：运行迁移 `make migrate`

### 关键词检索无结果

调整相似度阈值：

```python
# 在查询前设置
await session.execute(text("SET LOCAL pg_trgm.similarity_threshold = 0.1"))
```

### RRF 分数异常

检查 `rrf_k` 参数，确保在合理范围（1-100）：

```python
{
    "search_mode": "hybrid",
    "rrf_k": 60,  # 推荐值：40-80
}
```

## 最佳实践

1. **选择合适的检索模式**
   - 语义理解任务 → `vector`
   - 精确术语查找 → `keyword`
   - 通用查询 → `hybrid`

2. **调整 top_k**
   - 混合检索会先取 `top_k * 2` 个结果再融合
   - 最终返回 `top_k` 个结果

3. **使用元数据过滤**
   - 缩小检索范围，提高精度

4. **启用重排器**
   - 对于高质量要求的场景，使用重排器进一步优化结果

## 示例：LangGraph 集成

```python
from app.modules.knowledge.service import KnowledgeService

async def rag_agent(state):
    """使用混合检索的 RAG Agent。"""
    query = state["query"]
    
    # 混合检索
    results = await knowledge_service.hybrid_search(
        principal=state["principal"],
        knowledge_id=state["knowledge_id"],
        query=query,
        top_k=5,
        rerank=True,
    )
    
    # 构建上下文
    context = "\n\n".join([
        f"文档 {i+1}:\n{r['content']}"
        for i, r in enumerate(results)
    ])
    
    state["context"] = context
    return state
```
