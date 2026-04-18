# 各大 CTF WP 知识库

> 公开仓库只附带源码、测试与静态来源清单 `data/source_library_cn.json`。
> `writeups_raw.jsonl`、`writeups_index.jsonl`、`milvus.db`、`qdrant/` 等文件均为本地生成产物，不随仓库发布。

面向 Web / Pwn / Crypto / Misc / Reverse / Forensics / OSINT 的本地知识库：
支持 `CTFTime + 中文 curated 来源库` 批量采集、精简索引入库、Milvus/Qdrant 双后端，以及在线/离线两种检索问答模式。

## 架构

```text
CTFTime / 来源库 / manifest
        ↓
  writeups_raw.jsonl (原始归档层)
        ↓
  normalize + dedupe + slim chunks
        ↓
writeups_index.jsonl (轻量索引快照)
        ↓
Milvus Lite / Qdrant
        ↓
在线 LLM / 本地 LLM / 抽取式离线回答
        ↓
CLI / HTTP API
```

## 关键特性

- 默认在线 embedding 使用 SiliconFlow `Qwen/Qwen3-Embedding-8B`，显式 `dimensions=4096`
- 支持 `VECTOR_BACKEND=milvus|qdrant`
- Qdrant 首版采用混合分桶：`web/pwn/crypto/misc` 独立，其他类别进入 `shared`
- 入库时自动去重、精简 payload、限制单篇 writeup chunk 数量
- `CTF_WRITEUPS_OFFLINE_MODE=true` 时禁用远程 embedding/远程 LLM/联网 crawl
- 离线问答支持双模式：
  没有本地 LLM 时输出抽取式证据总结
  有本地 OpenAI-compatible LLM 时自动升级为生成式回答

## 环境变量

常用配置：

```bash
export VECTOR_BACKEND=milvus
export MILVUS_DB_PATH=./data/milvus.db
export QDRANT_PATH=./data/qdrant
export RAW_JSONL=./data/writeups_raw.jsonl
export INDEX_JSONL=./data/writeups_index.jsonl

export EMBED_API_BASE_URL=https://api.siliconflow.cn
export EMBED_MODEL=Qwen/Qwen3-Embedding-8B
export EMBED_ENCODING_FORMAT=float
export EMBED_DIMENSIONS=4096
export EMBED_API_KEY=your-siliconflow-key

export LOCAL_EMBED_MODEL_PATH=/path/to/local/qwen3-embedding
export CTF_WRITEUPS_OFFLINE_MODE=false
export OFFLINE_ANSWER_MODE=auto
export LOCAL_LLM_BASE_URL=http://127.0.0.1:11434/v1
export LOCAL_LLM_MODEL=qwen2.5:7b-instruct
```

兼容性说明：
- 旧环境变量 `TOU_OFFLINE_MODE` / `TOU_LLM_ROLE` 仍可继续使用
- 新部署建议统一切换到 `CTF_WRITEUPS_*` 前缀

## 快速开始

默认建议使用项目内 `.venv`：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

`uv run main.py <cmd>` 仍然可以继续作为本地开发捷径，但 GitHub 文档和比赛环境默认以 `.venv` 为准。

### 1. 采集 writeup

```bash
python main.py crawl --pages 20 --source-presets cn-curated
```

也可以混合使用来源库与 manifest：

```bash
python main.py crawl \
  --source-presets cn-curated \
  --source-manifest ./data/custom_sources.jsonl
```

### 2. 导入当前向量后端

```bash
python main.py ingest
```

这一步会同时生成：

- `data/writeups_raw.jsonl`：原始归档层
- `data/writeups_index.jsonl`：本地轻量索引快照

这些文件在公开仓库中默认不存在，需要你在本地执行采集/导入后生成。

### 3. 检查在线/离线依赖

```bash
python main.py doctor
```

### 4. 交互式问答

```bash
python main.py chat
```

### 5. 启动 API

```bash
python main.py serve --host 127.0.0.1 --port 8000
```

接口：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/health` | 返回当前向量后端与 embedding 状态 |
| GET  | `/search?q=ssti&category=web&difficulty=hard&year=2024` | 带过滤的语义检索 |
| POST | `/chat` body: `{"message": "..."}` | 在线/离线知识库问答 |

## 离线模式

离线查询前建议准备：

1. 已有 `writeups_raw.jsonl` 与 `writeups_index.jsonl`
2. 已入库的本地 Milvus/Qdrant 数据
3. 预下载的本地 embedding 模型，并设置 `LOCAL_EMBED_MODEL_PATH`
4. 可选：本地 OpenAI-compatible LLM 服务

启用离线模式：

```bash
export CTF_WRITEUPS_OFFLINE_MODE=true
python main.py doctor
python main.py chat
```

## 项目结构

```text
.
├── data/
│   ├── source_library_cn.json
│   ├── writeups_raw.jsonl      # 本地生成，不随公开仓库发布
│   ├── writeups_index.jsonl    # 本地生成，不随公开仓库发布
│   ├── milvus.db               # 本地生成，不随公开仓库发布
│   └── qdrant/                 # 本地生成，不随公开仓库发布
├── src/ctf_kb/
│   ├── api/
│   ├── crawler/
│   ├── llm/
│   ├── rag/
│   ├── vector/
│   ├── cli.py
│   ├── config.py
│   └── models.py
└── tests/
```
