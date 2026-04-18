# LingXi

LingXi 是一个面向比赛环境整理出的开源版多 Agent CTF/攻防辅助框架。

这个公开仓库采用“核心开源 + 扩展私有”的发布策略，只保留可公开的主流程、Agent 编排、Web 控制台、记忆/知识抽象层、LLM 适配层、Docker/Kali 基础运行层、单元测试，以及 `ctf_writeups_kb` 的代码与测试。比赛运行数据、官方工具包、私有 PoC、Sliver 资产和历史 Git 记录均不包含在本仓库中。

## 项目定位

- 保留 LingXi 的核心架构与比赛项目背景。
- 默认提供“可启动、可开发、可测试”的公开版核心路径。
- 论坛、私有 PoC、Sliver 等能力在公开仓库中按“未开源扩展点”处理，默认关闭。
- 许可证为 `GPL-3.0-only`。

## 公开边界

公开仓库包含：

- 主入口与运行时配置：`main.py`、`config.py`、`runtime_env.py`
- Agent 编排与执行：`agent/`
- 工具层与平台适配：`tools/`
- Web Dashboard：`web/`
- 记忆与知识抽象层：`memory/`
- LLM 适配层：`llm/`
- Docker/Kali 基础运行层：`docker/`、`kali_container.py`
- 核心测试：`tests/`
- CTF Writeup 知识库代码与测试：`ctf_writeups_kb/`

公开仓库不包含：

- 任何 `.git` 历史、运行日志、数据库、抓取归档、知识库快照、`*.jsonl` 运行产物
- 任何比赛官方工具包、论坛私有扩展、私有 PoC 包、Sliver client/config 资产
- 任何真实赛事域名、私网地址、令牌、论坛私信状态或环境特定路径

## 核心能力

- 多 Agent 调度与角色协同
- 平台 API 接入与失败回退
- Web Dashboard 与待命模式
- 本地记忆与知识注入
- Docker/Kali 执行环境封装
- 可选的外部知识库服务集成
- `ctf_writeups_kb` 本地检索/API 子项目

## 仓库结构

```text
.
├── agent/                  # Agent 编排、提示词与执行器
├── ctf_writeups_kb/        # 可公开的 CTF Writeup 知识库代码与测试
├── docker/                 # Docker/Kali 运行层
├── llm/                    # LLM Provider 适配
├── memory/                 # 运行记忆与知识抽象层
├── tests/                  # 公开版核心测试
├── tools/                  # 平台、论坛、Kali、Sliver 等工具桥接
├── web/                    # Web Dashboard
├── .env.example            # 公共配置模板
├── QUICKSTART.md           # 快速启动说明
└── SECURITY.md             # 安全说明
```

## 依赖准备

公开版建议把依赖拆成“核心必需”和“本地可选扩展”两层来理解：

| 项 | 是否必需 | 说明 |
| --- | --- | --- |
| Python 3.11+ | 必需 | 公开版默认推荐 `python -m venv .venv` |
| LLM Provider 凭据 | 必需 | 至少准备一组主攻手/顾问可用模型 |
| Docker / Docker Desktop | 可选但推荐 | 用于 `DOCKER_ENABLED=true` 时的 Kali 容器工作流 |
| Kali 容器 | 可选但推荐 | 公开版保留 Docker/Kali 运行层，容器名称默认 `kali-pentest` |
| `mcp` Python 依赖 | 可选 | 公开版核心代码保留 MCP 桥接能力；无私有扩展时不会默认启用 |
| 本地技能目录 `extensions/skills/` | 可选 | 公开版不附带私有技能包；如你本地有扩展，可自行挂接 |
| 论坛扩展 `extensions/forum/` | 可选 | 公开版默认关闭，不附带实现 |
| Level2/专项 PoC 扩展 `extensions/level2-pocs/` | 可选 | 公开版默认关闭，不附带实现 |
| Sliver client / config | 可选 | 公开版只保留扩展接口，不附带资产 |

补充说明：

- README 和 QUICKSTART 对开源用户统一推荐 `python venv` 路径。
- 仓库维护或我本地做验证时可以使用 `uv` 作为开发加速工具，但这不是公开版默认安装方式。
- 如果不准备 Docker/Kali、MCP 扩展、技能目录，公开版核心依旧可以以待命模式启动并进入 Dashboard。

## 快速开始

详细步骤见 [QUICKSTART.md](./QUICKSTART.md)。

最小可用路径：

1. 使用 `python -m venv .venv` 创建虚拟环境并安装依赖。
2. 从 `.env.example` 复制为 `.env`，至少填入 LLM 相关配置。
3. 保持 `COMPETITION_BASE_URL` 为空时，LingXi 会进入待命模式。
4. 启动 Web 控制台：

```bash
python main.py --web --port 7890
```

默认未配置比赛平台时，程序会进入待命模式并保留 Dashboard，可用于本地开发、配置验证和模块调试。

如果你需要更完整的本地攻防工作流，再按需补上 Docker/Kali、MCP 扩展和本地技能目录。

## 环境变量

公共模板见 [`.env.example`](./.env.example)。下面是最常用的配置项：

| 变量 | 作用 | 公开版默认值 |
| --- | --- | --- |
| `COMPETITION_BASE_URL` | 比赛平台展示入口 | 留空，进入待命模式 |
| `COMPETITION_API_BASE_URL` | 比赛平台 API 入口 | 留空 |
| `AGENT_TOKEN` | 平台统一令牌 | 占位符 |
| `FORUM_ENABLED` | 是否启用论坛扩展链路 | `false` |
| `SERVER_HOST` | 论坛扩展入口 | 留空 |
| `MAIN_LLM_PROVIDER` | 主攻手 Provider | `openai` |
| `ADVISOR_LLM_PROVIDER` | 顾问 Provider | `anthropic` |
| `DOCKER_ENABLED` | 是否启用 Kali Docker 支持 | `true` |
| `SLIVER_ENABLED` | 是否启用 Sliver 扩展 | `false` |
| `SLIVER_AUTO_ENABLE_IF_PRESENT` | 自动启用本地 Sliver 扩展 | `false` |

## ctf_writeups_kb

`ctf_writeups_kb/` 是公开版保留的知识库子项目，负责：

- 采集与整理可公开来源的 CTF Writeup
- 构建轻量索引与向量检索链路
- 提供 CLI / HTTP API 两种访问方式

公开仓库只保留：

- 子项目代码
- 测试
- 静态来源清单 `ctf_writeups_kb/data/source_library_cn.json`

公开仓库不附带：

- `writeups_raw.jsonl`
- `writeups_index.jsonl`
- `milvus.db`
- `qdrant/`
- embedding cache 或其他生成数据

## 未开源扩展点

以下能力在公开版中只保留扩展点，不附带实现资产：

- 论坛赛道官方/私有扩展
- Level2/专项本地 PoC 包
- Sliver client 与 operator 配置
- 其他比赛专用工具或技能包

这些扩展默认关闭。若你在本地维护私有扩展，可以自行挂接到 `extensions/` 目录并通过环境变量启用，但这些内容不应提交回公开仓库。

## 测试

可先运行公开版最小验证：

```bash
python -m unittest tests.test_memory_knowledge
python -m unittest ctf_writeups_kb.tests.test_api_search
```

如需本地平台联调，可结合 `mock_platform_server.py` 做接口验证。

说明：

- 公开版文档默认仍然以 `python venv` 作为安装与测试方式。
- 仓库维护阶段可以使用 `uv` 做本地开发和测试加速，但不把它写成公开版唯一前提。

## 安全与合规

- 不要提交密钥、令牌、日志、数据库、抓取归档或任何比赛运行产物。
- 不要把私有扩展、官方工具包或带环境指纹的信息并入公开仓库。
- 若发现敏感信息或许可证问题，请先阅读 [SECURITY.md](./SECURITY.md)。

## 贡献

提交贡献前请阅读 [CONTRIBUTING.md](./CONTRIBUTING.md)。

## 许可证

本仓库采用 [GPL-3.0-only](./LICENSE)。
