<div align="center">

```
  ██╗     ██╗███╗   ██╗ ██████╗       ██╗  ██╗██╗
  ██║     ██║████╗  ██║██╔════╝       ╚██╗██╔╝██║
  ██║     ██║██╔██╗ ██║██║  ███╗█████╗ ╚███╔╝ ██║
  ██║     ██║██║╚██╗██║██║   ██║╚════╝ ██╔██╗ ██║
  ███████╗██║██║ ╚████║╚██████╔╝      ██╔╝ ██╗██║
  ╚══════╝╚═╝╚═╝  ╚═══╝ ╚═════╝       ╚═╝  ╚═╝╚═╝
```

**LingXi — 自主渗透测试智能体（公开整理版）**

_Autonomous Penetration Testing Intelligence_

面向比赛/训练场景整理出的 LLM 驱动多 Agent 渗透辅助框架

</div>

---

## 📋 目录

- [项目简介](#项目简介)
- [系统架构](#-系统架构)
- [核心特性](#-核心特性)
- [运行模式](#运行模式)
- [项目结构](#-项目结构)
- [快速开始](#快速开始)
- [关键配置项](#关键配置项)
- [各大 CTF WP 知识库](#各大-ctf-wp-知识库)
- [扩展能力说明](#扩展能力说明)
- [Web Dashboard](#web-dashboard)
- [部署建议](#部署建议)
- [验证建议](#验证建议)
- [参考项目与致谢](#参考项目与致谢)
- [许可证](#许可证)

---

## 项目简介

LingXi 是一个从比赛项目中整理出来的公开版自主渗透测试 Agent。它围绕“自动拉题 / 侦察 / 工具执行 / 策略纠偏 / Flag 提交 / 经验沉淀”的闭环设计，保留了可公开的核心架构：

- 主入口与调度流程
- Agent 编排与提示词体系
- Web Dashboard
- 记忆 / 知识抽象层
- LLM Provider 适配
- Docker/Kali 基础运行层
- `ctf_writeups_kb` 代码与测试

公开仓库采用“核心开源 + 扩展私有”的边界策略：

- 保留 LingXi 名称与比赛项目背景
- 不公开历史 Git 记录
- 不附带论坛官方工具包、私有 PoC、Sliver 资产、运行日志、数据库、抓取归档或知识库快照
- 缺失私有扩展时，公开版默认以“待命模式 + 可观察 + 可开发 + 可测试”的核心路径工作

当前默认主执行链路已经演进为 `agent/sdk_runner.py` 中的 SDK Runner 体系，因此本文档也以这一现状描述架构与运行方式。

---

## 🏗️ 系统架构

LingXi 延续 Planner / Executor / Reflector 的认知分层，但当前公开版更适合从“入口层 → 调度层 → 解题执行层 → 工具/MCP 层 → 记忆知识层 → 可观察层”来理解：

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│                            LingXi Public Architecture                        │
├──────────────────────────────────────────────────────────────────────────────┤
│  入口层                                                                      │
│  main.py / config.py / runtime_env.py / .env                                │
│  CLI: default / --main / --all / --web / --*-only                           │
├──────────────────────────────────────────────────────────────────────────────┤
│  调度层                                                                      │
│  agent/scheduler.py / background workers / instance lifecycle               │
│                                                                              │
│  ┌─────────────────────┬──────────────────────┬───────────────────────────┐ │
│  │ Platform Runtime    │ Optional Extensions  │ Async Workers             │ │
│  │ task dispatch       │ forum / sliver / poc │ knowledge writeback       │ │
│  │ instance lifecycle  │ loaded only if local │ memory sync               │ │
│  └──────────┬──────────┴──────────┬───────────┴──────────────┬────────────┘ │
│             │                     │                          │              │
├─────────────▼─────────────────────▼──────────────────────────▼──────────────┤
│  解题 / 执行层                                                               │
│  agent/sdk_runner.py / agent/prompts.py / agent/reflector.py                │
│  main agent / advisor / reflector / tool guard                              │
├──────────────────────────────────────────────────────────────────────────────┤
│  工具 / MCP 层                                                                │
│  tools/shell.py / python_exec.py / platform_api.py / forum_api.py           │
│  recon / kali_mcp / sliver_mcp / level2_cve_poc / mcp bridge               │
├──────────────────────────────────────────────────────────────────────────────┤
│  记忆 / 知识层                                                                │
│  memory/store.py / knowledge_store.py / knowledge_gateway.py                │
│  knowledge_service.py / ctf_writeups_kb/                                    │
├──────────────────────────────────────────────────────────────────────────────┤
│  可观察层                                                                      │
│  Rich Console / Web Dashboard / SSE / log file / reports                    │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 双 Agent 协作模型

| 角色 | 职责 | 绑定工具 | 模型 |
| --- | --- | --- | --- |
| **Main Agent（主攻手）** | 直接执行侦察、验证、利用、提交与收敛 | 题型相关工具、Kali、可选 MCP | 可配置 |
| **Advisor Agent（顾问）** | 在卡点、失败累积或高价值分支上做一次性纠偏与策略建议 | 无直接攻击工具，偏策略分析 | 可配置 |
| **Reflector（反思器）** | 对失败路径做分层归因，帮助主链路转向 | 无 | 复用顾问链路 |

---

## ✨ 核心特性

### 🎯 智能执行能力

- 自动化主链路：拉题、侦察、执行、提交、复盘形成闭环
- 多模式运行：支持主战场、双开、仅 Web、仅独立子模式
- 自动侦察：端口、HTTP 指纹、页面上下文、错误回显等基础线索提纯
- 多执行手段协同：Shell、Python、Kali、平台 API、可选 MCP 扩展
- 进度感知：支持基于分数/Flag 状态做任务级收敛

### 🧠 分层知识系统

- 运行记忆层：同题 / 同 scope 的热路径回放
- 结构化经验层：高价值成功链路与失败归因写回
- 外部知识层：`ctf_writeups_kb/` 提供可控检索与 API 服务
- 门控策略：本地经验优先于外部参考

### 🛡️ 鲁棒性

- Flag 快路径提交
- 危险 Shell / Python 行为约束
- 重复低价值路径拦截
- API 退避与恢复
- 写回线程安全
- 缺少私有扩展时优雅降级

### 📊 可观察性

- Rich Console
- FastAPI + SSE Web Dashboard
- 知识中心页面
- 调度状态、日志轨迹、任务控制可视化

### 🔄 Provider 与容灾

- 支持多 Provider 角色拆分
- 主攻手 / 顾问链路隔离
- 支持备用端点切换
- 支持可选论坛独立模型链路

---

## 运行模式

主入口是根目录的 `main.py`：

```bash
python main.py
```

支持以下模式：

| 命令 | 说明 |
| --- | --- |
| `python main.py` | 默认主链路 |
| `python main.py --main` | 显式主链路模式 |
| `python main.py --all` | 主链路 + 可选扩展双开预留 |
| `python main.py --web` | 主链路 + Web Dashboard |
| `python main.py --web-only` | 仅启动 Web Dashboard |
| `python main.py --main-only` | 仅启动主链路独立进程 |
| `python main.py --forum-only` | 仅启动论坛扩展链路，需本地扩展 |
| `python main.py --web --port 7890` | 指定 Dashboard 端口 |

说明：

- 未配置 `COMPETITION_BASE_URL` / `COMPETITION_API_BASE_URL` 时，程序会进入待命模式
- 待命模式下 Dashboard 依然可用，适合本地开发、配置校验与文档化演示
- 论坛、PoC、Sliver 等路径默认关闭，只有你本地挂接扩展后才会启用

---

## 📦 项目结构

```text
LingXi/
├── main.py                    # 🚀 主入口
├── config.py                  # ⚙️ 全局配置管理
├── runtime_env.py             # 🐍 Python 解释器与 .venv 统一解析
├── start_daemon.sh            # 🕒 后台守护启动脚本
├── requirements.txt           # 📦 运行依赖
├── .env.example               # 🔑 公共配置模板
├── README.md                  # 📖 本文档
│
├── agent/                     # 🧠 Agent 核心
│   ├── scheduler.py           #   调度与实例生命周期
│   ├── sdk_runner.py          #   当前默认执行层
│   ├── prompts.py             #   系统提示词
│   ├── reflector.py           #   失败归因与反思
│   ├── skills.py              #   技能与扩展装配
│   └── console.py             #   Rich 输出
│
├── tools/                     # 🔧 工具层
│   ├── shell.py               #   Shell 执行器
│   ├── python_exec.py         #   Python 执行器
│   ├── platform_api.py        #   平台 API 客户端
│   ├── forum_api.py           #   论坛扩展桥接
│   ├── level2_cve_poc.py      #   本地 PoC 扩展桥接
│   ├── kali_mcp.py            #   Kali MCP 桥接
│   ├── sliver_mcp.py          #   Sliver MCP 桥接
│   └── recon.py               #   自动侦察
│
├── llm/                       # 🤖 LLM Provider 适配
├── memory/                    # 💾 记忆与知识系统
├── web/                       # 🌐 Web Dashboard
├── ctf_writeups_kb/           # 📚 CTF Writeup 检索子项目
├── docker/                    # 🐳 Kali 容器与执行环境
├── tests/                     # ✅ 主项目测试
│
├── extensions/                # 🧩 本地可选扩展挂载点（公开仓库默认不附带）
│   ├── forum/                 #   论坛扩展
│   ├── level2-pocs/           #   私有 PoC 扩展
│   ├── skills/                #   本地技能目录
│   └── additional-skills/     #   额外技能目录
│
├── SECURITY.md                # 🔒 安全说明
└── CONTRIBUTING.md            # 🤝 贡献说明
```

---

## 快速开始

### 1. 环境准备

建议环境：

- Python `3.11+`
- Docker / Docker Desktop（可选但推荐）
- 可运行 Kali 容器的本地或服务器环境（可选）

安装依赖：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Windows PowerShell：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

说明：

- 开源仓库默认推荐 `python venv`，这是对外文档的标准路径
- 我本地维护或验证时可以使用 `uv` 做加速，但这不是公开版前置条件
- `requirements.txt` 已包含 `mcp` 依赖；即使没有本地扩展，实现层也能正常导入并保持默认关闭

### 2. 依赖说明

公开版建议把依赖拆成“核心必需”和“本地可选扩展”两层理解：

| 项 | 是否必需 | 说明 |
| --- | --- | --- |
| Python 3.11+ | 必需 | 公开版主推荐 `venv` |
| 一组可用 LLM 配置 | 必需 | 至少主攻手/顾问能正常响应 |
| Docker / Docker Desktop | 可选但推荐 | 用于 `DOCKER_ENABLED=true` 时的 Kali 工作流 |
| Kali 容器 | 可选但推荐 | 公开版保留 Docker/Kali 执行层 |
| MCP 依赖 | 可选 | 代码内保留 MCP 桥接，但不强制启用任何私有服务 |
| 本地技能目录 `extensions/skills/` | 可选 | 公开仓库不附带私有技能包 |
| 论坛扩展 `extensions/forum/` | 可选 | 缺省关闭，不附带实现 |
| PoC 扩展 `extensions/level2-pocs/` | 可选 | 缺省关闭，不附带实现 |
| Sliver client / config | 可选 | 只保留桥接接口，不附带资产 |

### 3. 配置环境变量

复制模板：

```bash
cp .env.example .env
```

公开版最小可运行配置示例：

```env
# 平台地址留空时进入待命模式
COMPETITION_BASE_URL=
COMPETITION_API_BASE_URL=

AGENT_TOKEN=replace-with-agent-token

MAIN_LLM_PROVIDER=openai
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=replace-with-openai-key
OPENAI_MODEL=gpt-4o-mini

ADVISOR_LLM_PROVIDER=anthropic
ANTHROPIC_BASE_URL=https://api.anthropic.com
ANTHROPIC_API_KEY=replace-with-anthropic-key
ANTHROPIC_MODEL=claude-3-5-sonnet-latest

DOCKER_ENABLED=true
FORUM_ENABLED=false
SLIVER_ENABLED=false
SLIVER_AUTO_ENABLE_IF_PRESENT=false
```

### 4. 启动 Docker / Kali（可选）

如果你希望启用 Docker/Kali 工作流：

```bash
cd docker
docker compose up -d
```

如果你只想验证公开版主程序、Web、记忆和知识层，这一步可以暂时跳过。

### 5. 启动 LingXi

```bash
# 待命模式 + Web
python main.py --web --port 7890

# 默认主链路
python main.py

# 独立 Web
python main.py --web-only --port 7890
```

---

## 关键配置项

下面这些变量最常用：

| 变量 | 说明 | 公开版默认/备注 |
| --- | --- | --- |
| `COMPETITION_BASE_URL` | 平台展示入口 | 默认留空，进入待命模式 |
| `COMPETITION_API_BASE_URL` | 平台 API 入口 | 默认留空 |
| `AGENT_TOKEN` | 平台统一认证令牌 | 推荐显式配置 |
| `FORUM_ENABLED` | 是否启用论坛扩展链路 | 默认 `false` |
| `SERVER_HOST` | 论坛扩展入口 | 默认留空 |
| `SERVER_HOST_FALLBACK` | 论坛扩展回退入口 | 可选 |
| `DOCKER_ENABLED` | 是否启用 Kali Docker 执行环境 | 默认 `true` |
| `DOCKER_CONTAINER_NAME` | Kali 容器名 | 默认 `kali-pentest` |
| `MAIN_LLM_PROVIDER` | 主攻手 Provider | 默认 `openai` |
| `ADVISOR_LLM_PROVIDER` | 顾问 Provider | 默认 `anthropic` |
| `MAX_ATTEMPTS` | 单题最大尝试数 | 默认 `70` |
| `MAX_CONCURRENT_TASKS` | 主链路并发任务数 | 默认 `8` |
| `SINGLE_TASK_TIMEOUT` | 单题超时秒数 | 默认 `1800` |
| `RETRY_BACKOFF_SECONDS` | 失败退避秒数 | 默认 `60` |
| `KNOWLEDGE_SERVICE_ENABLED` | 是否允许拉起知识服务 | 默认 `false` |
| `SLIVER_ENABLED` | 是否启用 Sliver 扩展 | 默认 `false` |
| `SLIVER_CLIENT_PATH` | Sliver client 路径 | 默认 `./bin/sliver-client` |
| `SLIVER_CLIENT_CONFIG` | Sliver 配置目录 | 默认 `./sliver-config` |
| `SLIVER_CLIENT_ROOT_DIR` | Sliver 工作目录 | 默认 `./sliver-workdir` |

更完整的配置请看：

- 根项目配置模板：`./.env.example`
- 知识库子模块说明：`./ctf_writeups_kb/README.md`

---

## 各大 CTF WP 知识库

`ctf_writeups_kb/` 是公开版保留的外部知识库子项目，职责是：

- 采集和整理可公开来源的 CTF Writeup
- 构建 embedding / 检索 / 轻量索引链路
- 提供 CLI / HTTP API 两种访问方式
- 为顾问链路提供受控外部参考

典型数据流：

```text
公开来源 / 来源清单 / manifest
        ↓
writeups_raw.jsonl（本地生成）
        ↓
normalize / dedupe / chunk
        ↓
writeups_index.jsonl（本地生成）
        ↓
Milvus Lite / Qdrant（本地生成）
        ↓
KnowledgeService / Gateway
        ↓
Advisor / API / CLI
```

注意：

- 公开仓库只附带代码、测试和 `data/source_library_cn.json`
- `writeups_raw.jsonl`、`writeups_index.jsonl`、`milvus.db`、`qdrant/` 都是本地生成产物
- 不会随公共仓库一起发布

---

## 扩展能力说明

公开版保留扩展接口，但不附带私有实现。主要包括：

### 1. Docker / Kali

- 公开版保留 `docker/`、`kali_container.py`、`tools/kali_mcp.py`
- 适用于本地 Kali 容器中的工具执行与受控工作流
- 没有 Docker 时不影响待命模式、Dashboard、记忆和知识层

### 2. MCP

- `requirements.txt` 已包含 `mcp`
- `tools/forum_api.py`、`tools/sliver_mcp.py`、`tools/kali_mcp.py` 仍保留桥接层
- 如果没有本地扩展或目标服务，桥接层默认不会强制启用

### 3. 技能目录

公开版支持本地挂接以下技能/扩展目录：

- `extensions/skills/`
- `extensions/additional-skills/`
- `extensions/forum/`
- `extensions/level2-pocs/`

说明：

- 这些目录在公开仓库中默认不存在或为空
- 如果你在本地有私有技能包或专项扩展，可以自行挂接
- 不应把这些私有扩展再提交回公开仓库

### 4. 论坛 / PoC / Sliver

- 论坛赛道能力在公开版中是“可选论坛扩展”
- Level2/专项 PoC 能力在公开版中是“本地 PoC 扩展”
- Sliver 能力在公开版中是“可选控制面扩展”
- 三者默认关闭，缺失时不应阻断公开版基础运行

---

## Web Dashboard

Web 面板是可选组件，不是主链路必需。

启动方式：

```bash
python main.py --web
python main.py --web --port 7890
python main.py --web-only --port 7890
```

主要用途：

- 观察任务、赛区状态和调度结果
- 查看实时日志流与执行轨迹
- 手动创建 / 暂停 / 恢复 / 中止任务
- 浏览记忆与知识中心

---

## 部署建议

### 方式一：直接运行

```bash
git clone https://github.com/adrian803/LingXi.git
cd LingXi
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
python main.py --web
```

### 方式二：带 Docker/Kali 运行

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
docker compose -f docker/docker-compose.yml up -d
python main.py --web
```

### 方式三：后台守护

```bash
bash start_daemon.sh --web
bash start_daemon.sh --status
bash start_daemon.sh --logs
```

说明：

- 公开版不附带比赛环境专用部署脚本
- 公开版推荐先跑通 `venv + 待命模式 + Dashboard`
- 之后再按需补 Docker/Kali、MCP、技能目录或私有扩展

---

## 验证建议

上线前建议至少做这些检查：

```bash
python -m py_compile main.py agent/sdk_runner.py agent/skills.py tools/forum_api.py tools/level2_cve_poc.py web/server.py
python -m unittest tests.test_memory_knowledge
python -m unittest tests.test_forum_host_fallback
python -m unittest tests.test_level2_poc_tool
python -m unittest ctf_writeups_kb.tests.test_api_search
```

公开版文档默认仍然使用 `python venv` 作为安装与测试方式；维护者本地可以使用 `uv` 做开发加速，但不把 `uv` 作为开源用户的必选前提。

---

## 参考项目与致谢

LingXi 的公开版实现参考了以下项目或技术栈：

- Claude Code SDK
- LangChain
- FastAPI
- Rich
- MCP
- pymilvus / Qdrant

---

## 许可证

本仓库采用 [GPL-3.0-only](./LICENSE)。

---

<div align="center">

_心有灵犀一点通 — 以 AI 之智，破万千屏障_

</div>
