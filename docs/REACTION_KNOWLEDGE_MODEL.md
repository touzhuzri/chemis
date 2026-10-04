# Reaction Knowledge Model（Stage 3.1）

## 目标

把「反应名称 / seed reaction / 匹配结果」升级为一个正式、稳定、可扩展的
**Reaction Knowledge Model**——回答「CREA 到底把一个化学反应理解成什么对象」。
目标不是增加反应，而是定义这个对象。

## 复用了什么（原地扩展，无第二套模型）

- `core/models/reaction_template.py::ReactionTemplate` 原地扩展（**没有**
  `ReactionTemplateV2`）。
- `services/reaction_library.py::ReactionMatcher` 继续用 SMARTS 子结构匹配。
- 复用现有 `Provenance`；46 条 seed reactions 全量迁移（Stage 3.3 扩充到 46）。

## ReactionTemplate 最终设计（§六-§十九）

```text
ReactionTemplate
├── template_id           内部记录 ID（≠ name，≠ 外部反应 ID）
├── name                  人类可读标签（不推导语义）
├── class_family          反应族（substitution/addition/elimination/
│                         oxidation/reduction/coupling/rearrangement/condensation）
├── reaction_class        具体类别（hydrogenation / alcohol_oxidation / …）
├── transformation        结构化：input_features → output_features（+ note）
├── substrate_patterns    SMARTS 列表（所有 pattern 属于 template）
├── product_patterns      SMARTS 列表（无法可靠编码时为空 + 明确 note）
├── reagents              [ReagentSpec(name, role)]（与 conditions 分离）
├── conditions            [ConditionSpec(kind, value)]（典型条件，非实验实例）
├── mechanism_class       独立字段，绝不从 class 猜测（§14）
├── selectivity           SelectivitySpec（regio/stereo/chemo，仅存储不推断）
├── limitations           [Limitation(text, evidence_status)]
├── evidence_status       unknown/curated/literature_supported/user_provided
├── external_references   [ExternalReference(namespace, identifier)]
└── provenance            复用现有 Provenance
```

## 关键语义

1. **Identity ≠ Name**：`template_id` 是内部记录 ID；`name`（如
   "Hydrogenation"）不是唯一身份——同一名字可对应不同底物/转化。
2. **内部 ID ≠ 外部 ID**：`template_id` 不声称是国际反应 ID；ORD/Reaxys/CAS
   等放 `external_references`，CID 式的外部引用同样分离。
3. **Mechanism ≠ Class**：`mechanism_class`（SN1/SN2/E2/…）独立，绝不从
   class 推导。
4. **匹配 ≠ 成功保证**（§20）：`ReactionMatch.note` 明确——「substrate
   pattern matched; this is a known transformation pattern, not a guarantee
   that the reaction succeeds」。UI 用「Matching Known Transformation」而非
   「Will React」。
5. **证据诚实**（§17/§18）：seed reactions `source_type=CURATED` +
   `evidence_status=curated`；limitation 默认 CURATED，不伪装成 literature。
6. **product pattern 不伪造**（§11）：无法可靠编码的 `product_patterns=[]`
   + `product_pattern_note="product pattern not yet encoded"`。

## 迁移

46 条 seed reactions 全部迁移，每条含 id / name / class_family / reaction_class
/ 结构化 transformation / substrate SMARTS / reagent / conditions / mechanism /
selectivity / limitations / provenance / evidence_status。

## 测试

`tests/reaction/test_reaction_template.py`（14）：结构化对象、ID 唯一、class
family 结构化、transformation 结构化、mechanism 独立、selectivity 存储、
CURATED provenance、内部 ID ≠ 外部引用、product pattern 诚实、matcher
positive + negative（苯不匹配醇氧化、乙醇不匹配烯烃水合、芳基卤不匹配脂肪族
取代）、匹配≠成功保证。

## 明确不做

完整 bond-edit inference、真实 Reaction Instance 模型、ORD/Reaxys/CAS 接入、
Literature、AI、Retrosynthesis、Route Planning、新外部 Reaction Provider。
这些留后续 Stage。

---

## Stage 3.3 扩充

从 18 条扩充到 **46 条** curated ReactionTemplate。新增覆盖：

- **Aromatic EAS**（5）：硝化、卤化、Friedel-Crafts 烷基化/酰化、磺化
- **Alkene**：syn dihydroxylation
- **Alkyne**：dissolving-metal（trans）还原、氧化断裂、端炔烷基化
- **Alkyl halide**（机制细分）：SN1 / SN2 / E1 / E2（不同底物 SMARTS）
- **Alcohol**：脱水、伯醇→羧酸、醇→卤代烃
- **Carbonyl**：醛还原/氧化、酮 Grignard、Wittig、氰醇
- **Amine**：酰化、烷基化
- **Nitrile / Ester**：还原、水解
- **Epoxide**：开环
- **Rearrangement**：pinacol 重排

全部 `source_type=CURATED`、`evidence_status=curated`、SMARTS 可编译；positive /
negative 测试钉住跨家族无假阳性（苯不匹配烯烃氢化、氟苯不匹配 SN2、烷烃不匹配
羰基化学）。coupling（双底物）本阶段不编码。
