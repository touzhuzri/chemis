# Molecule Identity（Stage 2.2）

本文档记录 CREA 的化学身份规范。核心约束（§二/§二十四）：**CREA 不自创化学
身份**；跨软件、跨数据库的核心身份是 Standard InChI + Standard InChIKey。

## 1. Chemical identity definition

一个分子的化学身份 = 其 Standard InChI（标准化结构表示）。Standard InChIKey
是对应固定长度索引表示。二者共同构成跨 provider 的核心结构身份。

## 2. Standard InChI

- 由 RDKit 在 `chem/normalize.py`（唯一的 RDKit 归属地）生成，不在服务/API/UI
  各自重复计算（§五）。
- 存储字段：`ChemicalEntity.inchi`；内部版本化键 `identity_key =
  crea-identity-key-v1:<Standard InChI>`。

## 3. Standard InChIKey

- 字段：`ChemicalEntity.inchikey`。
- 用于数据库检索和互操作（`find_by_inchikey`、SQLite `idx_entities_inchikey`）。
- **不是**「永远唯一」——它是 Standard InChI 的固定长度索引表示；身份边界由
  Standard InChI 的标准化结果决定（§二十六）。

## 4. Internal record ID

- `ChemicalEntity.entity_id`（如 `CHEM-00000001`）。
- 只表示「CREA 数据库中的这一条记录」，**不**称为 `chemical_id`，**不**作为
  跨数据库身份。

## 5. External provider IDs

- `ChemicalEntity.external_identifiers`，如 `{namespace:"pubchem", identifier:
  "<cid>", source:"pubchem"}`。
- PubChem CID / ChEMBL ID 只是 `external_ids`，**不**替代 InChIKey。

## 6. SMILES 的角色

- SMILES 是输入/交换表示，**不是** chemical identity（§三）。
- 规范化后的 `canonical_smiles` 只是结构记法，身份始终由 InChI 决定。

## 7. normalization policy

- 输入经 `normalize` → RDKit validated Mol → Standard InChI/InChIKey →
  canonical_smiles → `MoleculeRecord`（复用现有 `ChemicalEntity`）。
- 身份一致性：`CCO` / `OCC` 等同一标准化结构的表示必须得到相同 InChI/InChIKey。

## 8. stereochemistry policy

- 尊重 Standard InChI 的标准化结果，明确立体化学（R/S、E/Z）不随意翻转/合并。
- 不实现额外的立体化学指派系统。

## 9. salt policy

- 不自行制定复杂 salt stripping 规则。现有 normalization policy 未定义的
  （质子化/盐/混合物），保守处理：保持原结构并记录 limitation（§七）。

## 10. duplicate policy

- 同一 Standard InChIKey 不创建多个重复记录（§九）。`MoleculeLibrary.add`
  按 `identity_key`（Standard InChI）去重，重复添加返回既有记录。
- Molecule（身份）与 Sample/Experiment 分离——那些是后续对象，本阶段不做。

## 11. provenance policy

- 复用现有 `Provenance`/字段：`identity_source`（CURATED / USER_PROVIDED /
  PUBCHEM / CHEMBL）、`source_id`、`source_url`、`retrieved_at`。
- 一个分子（身份）可对应多个来源；身份与 provenance 是两个维度。

## 现实限制

InChIKey 是检索索引，非「永远唯一」；CREA 不额外发明结构等价规则，一切以
Standard InChI 标准化结果为准。
