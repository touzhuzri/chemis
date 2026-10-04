# Phase 3.x Final Product Acceptance — 苯乙炔 Golden Case

> **3.x FINAL PRODUCT ACCEPTANCE: PASS**
> 日期：2026-10-02 ｜ 基线：1435 passed, 50 skipped, 0 failed
> 最终：**1445 passed, 50 skipped, 0 failed**

一次针对真实失败案例的收口：用户在 Workbench 输入
`研究苯乙炔（phenylacetylene）的基本性质、已有文献和可能的合成路线。` +
`C#Cc1ccccc1`，得到 Identity unresolved / Molecular Analysis 未执行 /
报告只讲缺什么。本文记录真实复现、根因定位、修复与产品级验收。

---

## 1. Root Cause（实际定位，非猜测）

按 §17 用真实 API 路径复现，并解剖了用户那次运行留在
`data/research-jobs.json` 的完整记录。**三层真实问题**：

### RC-1（用户运行的直接原因）：structure_input 在到达 workflow 前丢失
用户那次 job（`7caeb912b132`）的运行记录里：

```text
unresolved_items: ["identity unresolved：未提供 chemical name 或结构输入；未猜测结构。"]
```

workflow 收到的是**空结构**（`structure_input=None`）→ identity 步直接走
"未提供"分支 → 分析跳过、entity 缺失、gaps 含 resolve identity；而文献检索
用问题文本照常执行（15 个 DOI）。症状与运行记录逐条吻合。结构在
Browser → API DTO → Application → Job 链路的前端提交时刻丢失（结构字段
state 在提交时为空）。**因此本次为该链路补了逐层回归与 UI 提交确认**。

### RC-2（可 100% 复现的报告层 bug）：Web 报告渲染丢 identity/session
`ResearchApplication.report()` / `session_detail` 调
`render_research_report_v3(report)` 时**不传 `identity=` 与 `session=`** →
报告 §2 固定渲染 "Identity unresolved / 未附带身份解析结果"，**即使
workflow 实际 RESOLVED 且已建实体**（本机复现：entity=CHEM-00000001、
分析已计算，报告仍说 unresolved）。CLI 路径传了 identity，故 CLI/Web 表现
分裂。这正是"UI 有结构但 report 没结构 / API 有 Entity 但 UI 显示
unresolved"的直接根因。

### RC-3（解剖记录时发现）：重启抹掉 job 的 request 字段
`JobManager._load_registry` 只读 `entry["request"]`，而持久化格式是
**扁平字段** → 服务器重启后所有历史 job 的 question/structure 被清空并
回写（用户那次记录的 `question:''` 就是这样被抹掉的——其文献检索结果
证明运行时 question 是存在的）。

## 2. 修复内容

| # | 修复 | 位置 |
|---|---|---|
| F1 | job 在运行时捕获 identity resolution 与 session，报告渲染携带二者；§2 与 API/UI 一致 | `app/jobs.py`, `app/research.py` |
| F2 | `_load_registry` 重建 request（question/structure_kind/structure_value/document_ids），重启不再丢用户输入 | `app/jobs.py` |
| F3 | SessionDetail 增加 `identity`（identity card §17）与 `analysis`；UI 增加 **Molecular Analysis** 分区；Identity 卡显示 resolved/invalid/未提供 三态与原因 | `app/dto.py`, `web/src/pages/Workspace.tsx` |
| F4 | 无结构提交前 UI 明示确认（防止"以为提交了结构"）；报告无 identity 时措辞为"未提供结构输入" | `web/src/pages/Home.tsx`, `report/research.py` |
| F5 | 文献查询优先使用 identity 层建立的可信标签（candidate/input name），否则用用户问题原文；不使用 LLM 关键词 | `services/research_workflow.py` |
| F6 | CLI 增加 `--save-json`（= `--save` 归一） | `cli.py` |
| F7 | 数据流回归：golden case A 断言 structure 逐层到达；新增 job registry 重启往返测试 | tests |

**未改动**任何 3.x 封存语义（Evidence/Identity/Entity/Reaction/Route/…
零修改；`report/research.py` 仅改一句无解析时的提示文案）。

## 3. 测试

```text
pytest -q: 1445 passed, 50 skipped, 0 failed   （基线 1435 → +10）
```

新增：`tests/integration_offline/test_golden_phenylacetylene.py`（9，
Golden Case A–G + 重启复用 + CLI）、`test_app_layer.py` +1（request 重启
往返）。无删除、无断言降低、无 mock 替代真实 workflow。

## 4. Golden Case 实际结果（苯乙炔 / C#Cc1ccccc1）

Deterministic suite（offline fixtures，§14 允许）9/9，以及**真实 live HTTP**
（OpenAlex/Crossref 实检索）：

| 项 | 实测 |
|---|---|
| A Identity | **RESOLVED**；`C#Cc1ccccc1` → InChI `InChI=1S/C8H6/...` → InChIKey `UEXCJVNBTNXOEH-UHFFFAOYSA-N`；Entity **CHEM-00000001** 建立→复用；identity_key=`crea-identity-key-v1:<inchi>`；provenance/created_at 保留；session 引用 entity |
| B Molecular Analysis | **已执行**：`[Computed] 规范 SMILES…；分子式 C8H6；分子量 102.136；环数 1`（标注"计算结果，不是文献证据"） |
| C Knowledge Lookup | 首次：entity new；**第二次：CHEM-00000001 reused，new 全空，KB 中该 identity 只有一个实体**；实体 facet=known，**gaps 中不再出现 resolve identity** |
| D Literature | **真实执行**（live OpenAlex 15 篇，Sonogashira/苯乙炔加成等主题），全部标注 **[Discovery]**；搜索上下文来自确定身份/用户问题原文，非 LLM 关键词；discovery ≠ evidence |
| E Reaction | 仅 metadata → **0 reactions（允许）**，并明确"该文献被发现，但当前没有足够实验文本建立 Reaction。"；有本地 PDF → Document→Mention→Entity→ReactionRecord→Evidence→Reaction 全链真实执行 |
| F Routes | 真实环境 AiZynthFinder 未安装 → 报告明示 `retrosynthesis unavailable`，**route=UNAVAILABLE 而不影响 identity/analysis/literature**（status=PARTIAL 合法）；fixture 路线只以 Candidate 呈现 |
| G Report | [Computed] / [Discovery] / [Evidence] / gaps / conflicts 分区明确；"未附带身份解析结果" 不再出现于 resolved 运行 |

## 5. Web / API / CLI 真实结果

- **Web**：`python -m crea.cli serve` 实启；UI（重建 bundle）→ Research →
  Workspace 各分区一致：Chemical Identity 卡显示 resolved + 结构图 +
  CHEM id；Molecular Analysis 分区有 [Computed] 行；Final Report §2 与
  API identity 一致（同一 session.identity 数据源）。
- **API**（真实 HTTP，非 Python 函数直调）：POST /api/research 回显
  structure；GET job/session/events/report/knowledge/entity/reactions/
  evidence/routes 全部与报告一致；错误结构化（INVALID_STRUCTURE）。
- **CLI**：`python -m crea.cli research --question … --smiles C#Cc1ccccc1
  --db … --save-json …` → exit 0；报告含 `resolution status: resolved`、
  [Computed]、[Discovery]；JSON 与 Web 报告同语义（同一 ResearchWorkflow）；
  第二次 CLI 运行 `knowledge.existing.entities` 含 CHEM-00000001（复用）。
- **重启 SQLite**：停服 → 新进程 → entity/provenance/created_at、
  6 sessions、文献全保留；二次研究复用不重复（suite 与 live 双验证）。

## 6. Remaining Limitations

- 用户浏览器那次结构字段为空的**前端根因无法在无浏览器现场进一步定位**
 （JS 执行/输入时序）；已用 UI 提交确认 + 逐层回显 + 回归测试把该失败
  变成显式可见、可测试的路径。若复发，job 回显将直接显示 structure 是否
  到达。
- 文献为文本 discovery：与目标化合物的**相关性**不作保证（§14 明确不
  过度修复）；相关性筛选留给后续阶段。
- 仅 metadata 时 reactions=0 是诚实结果；reaction 建立需要实验全文/本地
  PDF（产品闭环在 E 项已验证）。
- AiZynthFinder 未配置的环境 routes=UNAVAILABLE（诚实）；配置后自动进入
  候选路线 + 验证链。
- 名称输入（NAME）仍依赖 provider 可用性；SMILES/InChI 本地确定性路径
  不受影响（§三）。

## 7. 验收结论

> **3.x FINAL PRODUCT ACCEPTANCE: PASS**

CREA 现在对 `C#Cc1ccccc1` 的处理是产品级正确的：

```text
明确结构 → RESOLVED → Entity（建立/复用）→ 分析 → 知识查询 → 文献检索
→ 有证据时建 Reaction → 尝试路线（不可用则明示）→ 验证支持等级
→ 保存知识 → 诚实报告 → 下次复用
```

每一步做不到时**明确说明原因**，而不是把已确定的信息一起变成 UNKNOWN。
按要求停在此收口，不进入 Phase 5。
