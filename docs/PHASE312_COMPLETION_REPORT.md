# Phase 3.12 Completion Report — Research Intelligence Completion

> **Final status: Phase 3.12: PASS**
> **3.x FINAL = PASS**

## 1. Phase Goal

把 3.0–3.11 的 Chemical Intelligence / Research Intelligence / Evidence /
Reaction / Route / Knowledge Base 收束成完整、可持续研究的核心系统：不仅
完成一次化学研究，而且记住已建立的化学知识，下一次研究识别已有知识、发现
缺口、只补充需要的新证据、保留冲突、更新知识库、给出可追溯报告。纵向闭环：

```text
Research #1 → 知识 → 持久化
Research #2 → 读已有 → 识别缺口 → 只补需要 → 知识库增长
Research #3 → 继续复用
```

## 2. 当前基线（开始前已确认）

```text
pytest -q: 1361 passed, 50 skipped, 0 failed   (Phase 3.11 收尾状态)
```

## 3. 开始前架构审计（§四）

封板边界复核：Identity / Mention / Evidence / Reaction / RouteValidation /
RouteEvidence / ReactionBuilder / AiZynthFinder 语义全部零改动；Repository
Protocol / InMemory 保留；LLM 只 propose（§六）链路
`LLM proposal → validator → domain object → repository` 完整。本阶段全部改动
为 additive：新模型、新服务、新工具、Report v3（v2 输出字节级不变）、
workflow 的 knowledge-aware 扩展（knowledge_base=None 时行为与 3.11 完全
一致）。无 ORM / 图数据库 / embedding / 向量库 / 新 LLM provider / agent
loop 重写（§89-§97 明令，均未触碰）。

## 4. Knowledge-aware Research（§七/§十三-§十六）——本阶段核心

`ResearchWorkflow.run` 新增 **Step 1.5 知识评估**（phase
`assessing_knowledge`）：

```text
Identity → KnowledgeBase lookup → KnowledgeCoverage → KnowledgeGap
        → ResearchPlan（确定性）→ 只执行缺口需要的步骤
```

- 正式 literature discovery 前查询 Entity/Paper/Reaction/Evidence/Route/
  Session（§十三）；已有 `RXN-…` 不再重建（registry merge + 复用）。
- **§三十九**：知识已覆盖且用户未要求最新文献 → `search_literature`
  **不执行**，step 记录 skip 原因"知识已覆盖且未要求最新文献（§39）"。
  路线已有 → `retrosynthesize` 不重复，读回既有路线进报告。
- **§十五**：已有知识=曾经记录的来源（含 provenance/retrieved_at），报告
  明示"不代表当前最新"，不作为当前事实。
- `knowledge_base=None` 时 coverage 全 UNKNOWN → 计划=完整流程 → 行为与
  既有完全一致（§三十六，全部既有测试零改动通过）。

## 5. KnowledgeState / Coverage / Gap（§八/§十二）

`core/models/knowledge.py`：`KnowledgeState`（UNKNOWN/PARTIALLY_KNOWN/
KNOWN，状态不是分数，永不输出"置信度 42%"）、`KnowledgeFacet`（entity/
paper/reaction/experimental_evidence/route/route_evidence）、
`KnowledgeCoverage`、`KnowledgeGap`。缺口给可读句子，例如（§十二 原句）：

```text
已有反应结构信息，但尚未找到可验证的实验条件证据。
```

## 6. ResearchPlan（§十七/§十八）

`ResearchPlan`/`ResearchPlanStep` + `services/research_intelligence.py`
的 `build_research_plan`：**deterministic planner**，步骤从缺口推导
（gap→suggested tool），无 planner framework、无 LLM 造计划。LLM 可提补充
动作，执行仍由 ToolRegistry/Evidence/Validation/Workflow 控制。
**§四十**："最新/最近/2026/recent/latest" 由问题自身文字确定性判定
（`wants_latest_literature`），命中则仍检索新文献。

## 7. Evidence Synthesis 与 Conflict（§十九-§二十四）

`synthesize_fields`：各记录的条件值（temperature/time/pressure/
concentration/yield/atmosphere/workup/purification）并置，每个观测携带
自己的 evidence/paper/record/locator（§二十四）。**CONFLICT ≠ INVALID**
（§二十三）：两来源都通过验证、只是不一致 → `FieldConflict`，保留双方值与
出处；**不平均、不选赢家**。无记录的反应不产生条件（§二十七 永不猜）。

## 8. ReactionRecord ↔ Reaction 最终统一（§二十五-§二十八）

审计并明确：`ReactionRecord` = 实验抽取结果/intermediate；`Reaction` =
知识库稳定对象；一条链，不是两套平行系统。**完成持久化**（3.11 遗留项）：
`reaction_records` 表（record_id↔reaction_id FK），conditions/quantities/
components/yields/unassigned/conflicts/evidence_refs/provenance 全存，
**raw_value/raw_unit 原样保留、零单位转换**，缺项保持 `NOT_FOUND`
（§二十七）。`get_reaction_record(reaction_id)` 回答"这个 Reaction 的实验
依据来自哪个 ReactionRecord？"（§二十八）。链断 → integrity audit 报告
`REACTION_RECORD_MISSING`/`REACTION_WITHOUT_RECORD`，不假装完整（§六十）。

## 9. Literature → Reaction 链路（§二十九）

Paper → ChemicalMention → ChemicalEntity → Reaction → Evidence 保持；
metadata（title/abstract/DOI）永不升级 `EXPERIMENTALLY_SUPPORTED`（E2E F）。

## 10. Image Provenance（§三十六）

图像识别来的结构可回答"从哪张图识别出来的？"：entity provenance 携带
recognizer/recognizer_version/file_reference（3.6 的 `ImageProvenance` 复用，
不重复定义）。DECIMER 不可用 → `UNAVAILABLE`（既有测试覆盖，H 项 PASS）。

## 11. Knowledge Integrity Audit（§六十一）

`KnowledgeIntegrityService`（`crea kb audit`，exit 1 on issues）：检查
record 链、supported-status-无证据、dangling evidence ref、mention 无
scope、provenance 缺失、session 悬挂链接。**只报告不修复**。

## 12. Knowledge-aware Agent（§三十七-§三十九）

新增 `get_existing_knowledge` / `get_knowledge_gaps` / `search_knowledge`
/ `synthesize_evidence`（§三十七 清单；3.10 已有能力复用不重复注册）。
工具描述写明 §三十八 顺序与 §三十九 不重复检索规则；synthesize 明示
"未平均、未选择"。E2E 验证：答已有反应不触网。

## 13. Report v3（§九十八-§一百）

`render_research_report_v3`：v2 + `## Existing Knowledge` / `## New
Knowledge` / `## Conflicts` / `## Knowledge Gaps` / `## Knowledge State`，
区分 Existing/New/Evidence/Conflict/Gap/Inference（J 项）。**纯 renderer**
（§九十九：domain/query result → Markdown，绝不反向写库/升级证据）。
knowledge=None 时输出与 v2 **字节级一致**；`render_report/format_report/
write_report/render_research_report` 全部保留（§九十八）。

## 14. E2E（§七十四-§八十四，全真实 Workflow/ToolRegistry/Repository/
## SQLite/domain models/Evidence validation，§一百一十四）

| # | 场景 | 结果 |
|---|---|---|
| A | 首次研究→知识→持久化 | PASS：new 标注、record 链/证据/会话重开可查 |
| B | 重启后二次研究识别已有 | PASS：existing 含 RXN、无新 mint、§39 skip 记录在案 |
| C | 增量研究 Paper A+B | PASS：A 不丢、B 新增、entity 不翻倍 |
| D | 80 °C/82% vs 60 °C/71% | PASS：CONFLICT 双引用、无平均（70 C 不出现）|
| E | candidate 无证据 | PASS：保持 CANDIDATE，缺口句子出现 |
| F | 仅 metadata | PASS：DISCOVERY、无 reaction、明示无实验文本 |
| G | 链路重开恢复 | PASS（record+reaction+evidence+route link 查询）|
| Growth | 三次研究 | PASS：entities 不变、reactions +1、evidence 增长、session 全留 |
| CLI | 真实子进程 research→kb→audit→export | PASS：Existing Knowledge 节、audit ok、复用不重复 |
| Agent | 四工具 | PASS：答存量、INVALID_QUERY、synthesize 备注 |

## 15. 测试

新增 44 个（1361 → 1405，50 skipped 不变）：
`test_research_intelligence.py`（14：coverage/gap 原句/plan 确定性/§39
skip/§40 markers/冲突双引用/无记录无条件）、`test_knowledge_integrity.py`
（5：clean audit/断链报告不修复/假 status 报告/无 scope 报告/§87 secrets
redact）、`test_reaction_records.py`（5：raw 值 round-trip/NOT_FOUND 保持/
无单位转换/record 链/FK）、`test_report_v3.py`（4：v2 字节不变/四节分离/
§十五 标注/首跑标注）、`test_research_intelligence_e2e.py`（9：A-G+
growth+CLI+agent）、架构测试 +1（research intelligence 禁 LLM/Provider/
RDKit/SQL）。

## 16. Full Test Result

```
pytest -q: 1405 passed, 50 skipped, 0 failed
```

开始基线（§八十五）已确认：`1361 passed, 50 skipped, 0 failed`。Live
tests 继续 opt-in（CREA_LIVE_TESTS=1），普通 pytest 零网络（§八十六）。
License/compliance 红线未触碰（§八十七）。Secrets：.env 忽略、API keys/
Authorization 永不入库（provenance 参数入店前 `_SECRET_KEY_RE`  scrub，
测试钉住，§八十八）。

## 17. Known Limitations

- `wants_latest_literature` 是文字标记判定（确定性优先）；语义级
  "是否要最新" 留给 Phase 4 的产品层。
- Coverage 的 facet 规则是可审计的保守判定（如 EXPERIMENTAL_EVIDENCE 以
  ReactionStatus 实验级 rung 为准），不是文献计量学意义上的"完备"。
- 合成聚合只覆盖记录中陈述的条件字段；自由文本中的条件仍属未抽取。
- Report v3 的 Inference 标签沿用 v2 findings 语义（inference 类 finding），
  未新增第四种标签体系。
- 图像来源记录依赖 identity 层 ImageProvenance（DECIMER 可用性即 3.6
  的 UNAVAILABLE 语义）。

## 18. Blocking Issues

**无。** 过程中修复均为实现缺陷/测试夹具问题（`is` 方法名与关键字冲突、
快照时序导致 existing/new 误判、夹具 entity 不含目标 identity 等），无
架构返工。

## 19. 3.x Final Assessment

**3.x FINAL = PASS**

3.0 → 3.12 的能力闭环成立：

```text
                         CREA 3.x CORE
        ┌─────────────────────┼─────────────────────┐
 Chemical Intelligence   Research Intelligence   Knowledge
 Identity · Entity       Workflow · Evidence     SQLite KB
 Mention · Structure     Reaction · Route        Persistence
        │                     │                   Query · Reuse
        └─────────────────────┼─────────────────────┘
                              ▼
                  Evidence-backed Chemical Research
                              ▼
                      Research Report (v3)
```

A-L 十二项 PASS 标准全部满足：A（workflow 实际使用 KB）、B（二次复用）、
C（缺口确定性产生）、D（新证据入旧知识）、E（冲突不覆盖）、F（record↔
reaction 完整链）、G（Route↔Reaction↔Evidence 完整）、H（图像链/UNAVAILABLE
诚实）、I（audit 可运行）、J（Report v3 六类区分）、K（重开全保持）、
L（真实栈，无 mock 核心）。

**Phase 3.x = COMPLETE。** 按 §一百一十五/§一百一十六 封板：不再拆
3.13/3.14/3.15；不进入 Phase 4（Web UI / 用户系统 / embedding / RAG /
多 agent / 云部署 等全部留待产品化阶段）。

## 20. 最终结论

```text
Phase 3.12: PASS
3.x FINAL = PASS

pytest -q: 1405 passed, 50 skipped, 0 failed
```
