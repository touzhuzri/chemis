# Reaction Explorer（Stage 3.2）

## 1. 页面目的

把 Stage 3.1 的 `ReactionTemplate` 做成用户可浏览的界面：输入分子后，从
「只看反应名字」升级为「按类别浏览、点击查看完整结构化信息」。

## 2. ReactionTemplate 数据来源

全部来自 `services/reaction_library.py::SEED_REACTION_TEMPLATES`（46 条 curated
模板）。API：`GET /api/reaction-templates`（列表 + class 过滤 + 名称搜索）、
`GET /api/reaction-templates/{template_id}`（详情）。

## 3. Matching reaction 的含义

Molecule Explorer 中显示的匹配反应来自后端 `ReactionMatcher`（SMARTS 子结构
匹配），不是前端重判「这是不是醇」。匹配 = 「结构符合该转化模式」，**不是**
「反应一定成功」——UI 用「Matching reaction patterns」而非「Recommended /
Will work / Best reaction」。

## 4. Reaction detail 字段说明

Name / class family + specific class / transformation（input → output）/
substrate SMARTS / product pattern / reagents / typical conditions /
mechanism / selectivity / limitations / evidence status / source / template_id。

## 5. SMARTS 的展示方式

不只显示原始 SMARTS：`transformation.input_features` 给人类可读描述
（如 "primary_alcohol"），`substrate_patterns` 给 SMARTS，二者并置。

## 6. Product pattern 可能为空

当前 46 条 seed 的 `product_patterns` 均为空（无法可靠编码 product SMARTS）。
UI 明确显示「Product pattern not encoded.」，**不生成假的产物结构**（§七）。

## 7. Curated evidence 的含义

`evidence_status = curated` + `source_type = CURATED` 表示「人工整理的教育/
工程数据」，不是文献证据、不是「Verified / Experimentally proven」。当前不
含 literature（留 Stage 3.4）。

## 8. 当前不包含 literature

无 DOI/paper search、citation list、experimental procedure。只保留一个
Evidence / Provenance 信息区。

## 9. 当前不代表 reaction feasibility

只有 SMARTS 模式匹配，没有实验成功预测。匹配结果不是可行性判断。

## 10. 当前不提供 AI recommendations

无 AI 气泡、无「智能推荐」文案。全部展示来自结构化 `ReactionTemplate` 字段。

## 导航

Molecule Explorer 的每个匹配反应 → 「View Reaction」→ Reaction Detail（带
`from` 来源分子）→ 可「Back to Molecule」返回原分子，或「Back to Reaction
Explorer」。

## 测试

`tests/reaction/test_reaction_explorer.py`（8）：list 46 条、class 过滤、名称
搜索、detail 完整结构化模型、未知 template_id 结构化 404、无重复 template_id
+ 全部可查。

---

### Stage 3.4 更新：Evidence 区

Reaction Detail 现在显示 `ReactionTemplate` 的 **Evidence** 记录（详见
`docs/REACTION_EVIDENCE.md`）：evidence_type / support_scope / source /
status。curated 模板显示其 CURATED_KNOWLEDGE 自证据；无记录时显示
「No evidence records.」——**不是**「No literature found.」（Evidence ≠
Literature）。API：`GET /api/reaction-templates/{id}/evidence`。
