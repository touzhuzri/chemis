# CREA Research Intelligence

Phase 3.12 — the layer that turns a knowledge base into a research
system. CREA does not only *complete* a chemistry research run; it
**remembers what it has established**, **recognizes it in the next run**,
**finds the gaps**, **adds only what is missing**, **keeps conflicts**,
and **reports what changed** — every fact with its source.

```text
Question → Identity → KnowledgeBase lookup → Existing Knowledge
        → Knowledge Gaps → ResearchPlan → only the missing work
        → new Evidence → Knowledge Base grows → Research Report
```

## The principle: the LLM is never a fact source (§6)

The model may propose, extract, summarize, plan and synthesize. It never
decides chemical identity, reaction identity, experimental evidence,
route support or literature facts. Those are decided by deterministic
validation, Evidence and domain policy — exactly as in Phases 3.0–3.11.

## Knowledge state (§8/§12)

`KnowledgeState` is a **state, never a score**: `UNKNOWN` /
`PARTIALLY_KNOWN` / `KNOWN`. There is no "置信度 42%"; the report says
"已有反应结构信息，但尚未找到可验证的实验条件证据。"

`KnowledgeCoverage` records the state of six facets for one target:
`entity`, `paper`, `reaction`, `experimental_evidence`, `route`,
`route_evidence` — each with the object ids the state rests on. A facet
is KNOWN because named, stored objects stand behind it.

**Known ≠ current (§15).** KNOWN means CREA has recorded sources and
facts — with provenance and `retrieved_at`. It never means "latest" or
"still correct". The report says so on every knowledge section.

## Knowledge gaps → research planning (§16–§18)

`find_gaps(coverage)` derives `KnowledgeGap`s — each a readable sentence
plus the kind of work that would close it. `build_research_plan(...)`
turns gaps into a `ResearchPlan` **deterministically** (no planner
framework, no model): a gap in experimental evidence plans document
inspection and reaction extraction; a route gap plans retrosynthesis.

Two rules matter most:

- **§39 — do not re-search what is known.** When the knowledge covers
  the question and the user did not ask for the latest literature, the
  plan contains no `search_literature` step, and the workflow records the
  skip with its reason. The reuse path answers instead.
- **§40 — latest is spelled, not interpreted.** `最新 / 最近 / 2026 /
  recent / latest` in the question's own words means current literature
  is wanted; the knowledge base does not pretend to be current.

The plan is advice the workflow executes through its own tools. An LLM
may propose additions; execution stays with the ToolRegistry, Evidence
and validation (§6/§18).

## Evidence synthesis and conflicts (§19–§24)

`synthesize_fields` places recorded condition values side by side —
temperature, time, pressure, concentration, yield, atmosphere, workup,
purification — every observation carrying **its own** evidence, paper and
record. Where sources state different values for one field:

```text
temperature: CONFLICT
  - 80 C ← EV-A (paper-a, RXN-a)
  - 60 C ← EV-B (paper-b, RXN-b)
```

`CONFLICT`, not `INVALID` (§23): both values survived validation; they
simply disagree. The synthesis never averages, ranks or picks a winner —
each value keeps its provenance (§22/§24). An `INVALID` value is a
different thing entirely: a proposal the extraction policy refused.

## ReactionRecord ↔ Reaction (§25–§28)

The two are one chain, not two reaction systems:

```text
ReactionRecord  =  实验抽取结果（intermediate structured record）
Reaction        =  知识库中的稳定反应知识对象
```

Records persist with their conditions **as written** — `raw_value` /
`raw_unit` preserved, no unit conversion, missing fields stay
`NOT_FOUND` (§27). `get_reaction_record(reaction_id)` answers "这个
Reaction 的实验依据来自哪个 ReactionRecord？". If the chain breaks, the
integrity audit says so instead of pretending completeness (§60).

## Knowledge integrity audit (§61)

`KnowledgeIntegrityService.audit(kb)` checks what constraints cannot:
record chains that broke, supported statuses without resolvable
evidence, mentions without scope, missing provenance, dangling session
links. It **reports and never repairs** — what is broken is said, not
rewritten. `crea kb audit` runs it (exit 1 when issues are found).

## Knowledge-aware agent (§37–§39)

| tool | answers |
|---|---|
| `get_existing_knowledge` | what is already established, per facet (§7) |
| `get_knowledge_gaps` | what a complete answer would have and we do not |
| `search_knowledge` | deterministic cross-object query (entity/reaction/paper/evidence/record) |
| `synthesize_evidence` | condition values side by side; conflicts kept |

Working order (§38): resolve identity → query existing knowledge →
identify gaps → research the missing evidence → validate → update
knowledge → synthesize → report. The agent does not mechanically repeat
`search_literature` when the knowledge base already answers (§39).

## Knowledge-aware report (Report v3, §98–§100)

`render_research_report_v3` is v2 plus four sections — **Existing
Knowledge**, **New Knowledge**, **Conflicts**, **Knowledge Gaps** — and
a compact **Knowledge State**. A pure renderer: domain/query results in,
Markdown out (§99); it never opens a database and never upgrades
evidence. Without knowledge data the output is byte-identical to v2's;
every pre-3.12 API is untouched (§98).

## Image provenance (§36)

A structure recognized from an image keeps its answer to "这个结构是
从哪张图识别出来的？" — the entity's provenance carries the recognizer,
recognizer version and file reference. DECIMER being unavailable is
reported as `UNAVAILABLE`, never improvised.

## What this phase deliberately is not (§3, §89–§95)

No Web UI, no RAG, no vector DB, no embeddings, no graph database, no
ORM, no new LLM providers, no rewritten agent loop. Queries stay
deterministic; `sqlite3` stays behind the repository; the research core
is complete, and productization is Phase 4's business.

## The loop this proves

```text
第一次研究 → 产生知识 → 持久化
第二次研究 → 读取已有知识 → 识别缺口 → 只补需要的 → 知识库增长
第三次研究 → 继续复用，旧知识不丢，冲突保留
```
