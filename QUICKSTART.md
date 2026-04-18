# Quick Start

本说明针对公开版 LingXi 的最小可用路径，不依赖论坛私有扩展、本地 PoC 包或 Sliver 资产。

## 1. 创建环境

公开版默认推荐 `python venv`。维护者本地可以用 `uv` 做加速，但对外文档仍以 `venv` 为标准路径。

PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

bash:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## 2. 配置 `.env`

从模板复制：

```bash
cp .env.example .env
```

公开版建议先保持下面几项：

- `COMPETITION_BASE_URL=` 留空
- `COMPETITION_API_BASE_URL=` 留空
- `FORUM_ENABLED=false`
- `SLIVER_ENABLED=false`

至少补齐一组可用的 LLM 配置后再启动。

## 3. 启动公开版核心路径

```bash
python main.py --web --port 7890
```

如果没有配置比赛平台地址，LingXi 会进入待命模式，Dashboard 仍然可用。

## 4. 本地验证

```bash
python -m unittest tests.test_memory_knowledge
python -m unittest ctf_writeups_kb.tests.test_api_search
```

## 5. 可选联调

- 需要本地平台接口演练时，可使用 `mock_platform_server.py`
- 需要论坛、专项 PoC、Sliver 等能力时，请以私有扩展方式本地挂接，不要提交到公开仓库
