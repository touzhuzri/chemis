# CREA — Chemical Research Workbench

CREA 是一个**化学研究工作台**：它不仅完成一次化学研究，而且记住自己已经
建立的化学知识，在下一次研究中识别已有知识、发现知识缺口、补充新的证据、
保留冲突、更新知识库，并给出可追溯的研究报告。

> **LLM ≠ 事实来源。** LLM 是研究编排者（propose / extract / plan）；
> 化学计算属于 RDKit，外部信息属于 Provider，支持属于 Evidence，
> 持久记忆属于 KnowledgeBase，研究过程由确定性的 ResearchWorkflow 驱动。

## CREA 能做什么

- **化学身份**：SMILES / InChI / 名称 / PubChem CID / 图像 → 确定性身份
  解析（RESOLVED / AMBIGUOUS / NOT_FOUND / UNAVAILABLE，绝不猜）
- **文献与证据**：官方 API（OpenAlex / Crossref / Unpaywall / PubChem）+
  用户本地 PDF；每条证据保留原文引用、locator、provenance
- **反应知识**：实验文本 →（LLM 提议 + 政策校验）→ ReactionRecord →
  Reaction → Evidence 链；状态是证据等级，不是置信度
- **逆合成与验证**：AiZynthFinder 候选路线 ↔ 文献反应确定性比对
  （exact / screening-only，相似≠证明）
- **知识库**：SQLite 持久化（Entity / Mention / Paper / Evidence /
  Reaction / Route / Session），去重靠 identity/key，冲突保留双方出处
- **知识感知研究**：先查已有知识 → 确定性缺口 → 只补需要的新证据 →
  报告区分 Existing / New / Conflict / Gap
- **工作台 UI**：浏览器里观察研究全过程，沿 provenance 链一路追溯

## CREA 不能做什么

- 不把 LLM 输出当事实；不猜缺失的结构、条件或产率
- 不做相似度当证明、不做均值消冲突、不删除科研证据
- 不含用户系统 / 云部署 / 向量检索 / RAG / 图数据库（留给后续阶段）

## 安装

```bash
uv sync            # 或: pip install -e .
```

依赖：RDKit（必需）、FastAPI/uvicorn（工作台）、pypdf（本地 PDF）。

前端已构建在 `web/dist/`（开箱即用）；如需重新构建：

```bash
cd web
npm install
npm run build
```

## 快速开始

```bash
python -m crea.cli serve
```

打开 <http://127.0.0.1:8000/> 即进入工作台（默认仅绑定本机）。

第一次研究：

1. 输入研究问题（例如"研究 1,4-diphenyl-1,3-diyne 的文献合成方法"）
2. 输入结构（SMILES / InChI / Name）
3. 可选：上传自己的 PDF
4. 点击"启动研究"，在 Research Workspace 中观察身份、知识、反应、
   证据、路线、缺口与冲突
5. 研究完成，Report v3 与全部知识自动进入 KnowledgeBase
6. 下一次研究自动复用已有知识，只补缺口

## 无网络演示

```bash
python -m crea.cli demo
```

完整研究链离线演示（identity → knowledge → reaction → evidence →
report），**全部演示数据 `origin = SYNTHETIC`**，需要时加
`--output report.md` 保存报告。

## 命令行研究与知识库

```bash
# 后台研究（写入 data/crea.db，--db 可改）
python -m crea.cli research \
  --question "研究 phenylacetylene" \
  --smiles "C#Cc1ccccc1" \
  --db data/crea.db

# 不启动研究，直接查询已积累的知识
python -m crea.cli kb entity CHEM-00000001 --json
python -m crea.cli kb reactions CHEM-00000001 --db data/crea.db
python -m crea.cli kb audit --db data/crea.db
python -m crea.cli kb export --output knowledge.json --db data/crea.db
```

## 配置 LLM（可选）

没有 LLM 时一切照常运行，实验文本只会诚实报告"无法建立 Reaction"。
配置 `CREA_LLM_API_KEY`（及 `CREA_LLM_BASE_URL` / `CREA_LLM_MODEL`，
见 `crea.env.example`）后，实验抽取由 LLM 提议、政策校验后入库。
LLM 永远只提议，不决定事实。

## 测试

```bash
pytest -q
```

默认完全离线。联网测试需要显式 `CREA_LIVE_TESTS=1`。

## 架构

```text
core → chem → services → repositories → agent → report → app → api → Web UI
```

- `chem/`：RDKit 唯一所在地
- `services/`：identity / entity / mention / reaction / route / evidence /
  workflow / knowledge intelligence
- `repositories/`：Repository Protocol（InMemory + SQLite）
- `app/`：用例、DTO、ResearchJob、事件
- `api/`：FastAPI；前端 `web/`（React + TypeScript + Vite）

详见 `docs/KNOWLEDGE_BASE.md`、`docs/RESEARCH_INTELLIGENCE.md` 与
`docs/PHASE4_COMPLETION_REPORT.md`。
