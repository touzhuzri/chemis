# Molecule Explorer（第一阶段）

> 产品定位：**Chemistry Tool First, AI Second**。输入一个分子，系统性地展示
> 它的结构、性质、相似关系、骨架、官能团与已知反应——全部由确定性化学计算
> 完成，不需要 LLM。

## 核心链条

```text
Molecule → Structure → Properties → Similarity
        → Scaffold / Functional Groups → Reaction Knowledge
```

而不是 `Molecule → AI Chat`。

## 如何使用

**Web UI**（`python -m crea.cli serve`）：

- 首页输入 SMILES（如 `CCO`）→「打开 Molecule Explorer」
- 或直接访问 `#/molecule/CCO`

**服务层**：

```python
from crea.services.inspection import MoleculeInspectionService
result = MoleculeInspectionService().inspect("CCO")
```

**HTTP**：

```text
GET /api/molecule/inspect?smiles=CCO
GET /api/molecule/compare?smiles_a=CCO&smiles_b=CC(=O)O
```

## 新增能力（复用现有 chemistry 层，零重写）

| 模块 | 职责 |
|---|---|
| `chem/functional_groups.py` | 官能团检测（SMARTS）+ `matches_smarts` 基元 |
| `services/inspection.py` | `MoleculeInspectionService`：identity + properties + FG + scaffold + fragments + reactions + DBE |
| `services/reaction_library.py` | 种子反应库 + `ReactionMatcher` |
| `services/similarity.py` | `SimilarityService`：指纹相似度 + scaffold + MCS（复用现有 `screen_similarity`/`compare_scaffolds`/`find_core_match`） |

复用的现有能力：`normalize`、`screen_similarity`、`compare_scaffolds`、
`murcko_scaffold_smiles`、`decompose_molecule`、`find_core_match`、
`molecule_svg`。

## 种子反应库（Reaction Library）

**source_type = CURATED** —— 本种子库是**人工整理的教育/工程数据**
（textbook transformations），不是文献引用，不是从数据库抓取的事实。
每个 `ReactionTemplate` 带 `source_id = seed/<id>` 与 provenance，可追溯。

覆盖的反应类别（46 条）：烯烃（加氢/卤化/氢卤化/酸性水合/硼氢化-氧化/
环氧化/氧化断裂）、炔烃（加氢/部分还原/氢卤化/水合）、醇（伯/仲醇氧化）、
羰基（醛亲核加成、酮还原）、羧酸还原、卤代烃（亲核取代/消除）。

**匹配语义**（§7）：`ReactionMatcher` 只判断「模板底物 SMARTS 是否与当前
结构匹配」——这是「可能发生」，不是「AI 判断一定能做」，更不是合成路线。

## 测试

`tests/inspection/test_inspection.py`（14）：乙醇/苯/乙炔/羰基/卤代烃检测、
无假阳性（苯不匹配醇氧化、醇不匹配烯烃反应、芳基卤不匹配脂肪族取代）、
相似度、骨架提取、反应匹配正/负/多匹配、非法分子、API 端点。

## 第一阶段明确未实现

3D conformer、分子绘制输入、图片识别、正式 molecule library / PubChem 接入、
文献接入、AI 分析、完整逆合成/路线优化、自动实验条件推荐。这些全部留后续。

---

## Molecule Library（Stage 2.2）

本地分子库，以 Standard InChIKey 为核心身份（详见 `docs/MOLECULE_IDENTITY.md`）。

**使用方式**：

- Web：首页「Molecule Library」→ `#/library`（添加/列表/筛选/打开/删除）。
- 打开一条记录 → 进入 Molecule Explorer（复用，不另建 detail 页）。
- 相似搜索：Molecule Explorer 的「Similarity — Search Local Library」按
  fingerprint similarity 排序库中分子，显示 score / InChIKey / source。

**API**：

```text
POST   /api/molecules                            {smiles, source_type, source_id}
GET    /api/molecules                            list + count
GET    /api/molecules/{record_id}
GET    /api/molecules/identity/{inchikey}
GET    /api/molecules/search/similar?smiles=
GET    /api/molecules/search/substructure?smiles=
DELETE /api/molecules/{record_id}
```

**身份语义**：record_id = `entity_id`（内部记录 ID），`identity_key` =
`crea-identity-key-v1:<Standard InChI>`，`inchikey` = Standard InChIKey，
外部 ID 在 `external_identifiers`。重复结构（如 CCO/OCC）只产生一条记录。

**Provider 抽象**：`MoleculeProvider`（`services/molecule_library.py`）为分子
来源抽象；`LocalMoleculeProvider` 是本地库实现。外部 PubChem/ChEMBL 是后续
可选，本阶段只做 local library + 小规模 lookup，不做大规模导入（§二十七）。

---

### Stage 3.1 更新：Reaction Knowledge Model

Reaction suggestions 现在来自正式的 `ReactionTemplate`（详见
`docs/REACTION_KNOWLEDGE_MODEL.md`）——结构化 class family / transformation
（input→output features）/ substrate SMARTS / reagent / conditions / mechanism
/ selectivity / limitations / evidence_status，而不是简单的字符串列表。
匹配语义不变：SMARTS 匹配 = 「结构符合该转化模式」，**不是**「反应一定成功」。

---

### Stage 3.2 更新：Reaction Explorer

Molecule Explorer 中每个匹配反应现在有「View Reaction」，可进入 Reaction
Explorer 的 Detail 页查看完整结构化 `ReactionTemplate`（详见
`docs/REACTION_EXPLORER.md`）。支持按 class_family 浏览 + 名称搜索。
