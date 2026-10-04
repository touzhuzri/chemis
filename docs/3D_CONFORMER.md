# 3D Conformer + Molecule Viewer（Stage 2.1）

## 1. 为什么需要 3D

第一阶段的 Molecule Explorer 展示了 2D 结构 + 性质。3D 让用户看到一个
**由算法生成的三维构象**，并与之交互（旋转/缩放/平移）——这对理解分子的
空间形状（特别是立体化学）有用。它不是「AI 分析」，也不是「实验测量」，
只是确定性几何。

## 2. 生成方法：RDKit ETKDG

管线（`chem/conformer.py`）：

```text
SMILES → normalize（复用）→ RDKit Mol → AddHs → ETKDG embed
      → optional MMFF94 / UFF optimization → 提取坐标 → MoleculeConformer
```

没有自己实现构象生成算法（§4）——ETKDG 是 RDKit 的确定性嵌入算法。

## 3. conformer 的意义

一个 conformer 是**「由特定算法和参数生成的一个合理构象」**，不是分子
唯一的三维形状。同一个分子可以有很多构象（构象柔性）；本阶段只生成
**一个代表性 conformer**，不扩展到 conformer ensemble（§10/§六）。

## 4. optional optimization

`generate(smiles, optimize=False)`：默认只做 ETKDG 嵌入；`optimize=True`
时用 MMFF94（失败回退 UFF）做几何优化。没有为了「看起来更科学」而无条件
执行昂贵优化（§四）。

## 5. stereochemistry 注意事项

输入从 canonical SMILES 重建（携带立体化学标记），ETKDG 尊重已指定的
立体中心——**已有明确 R/S、E/Z 的输入，不会在 normalization/3D 管线中被
无意破坏**（测试钉住 `C[C@H](O)F` 的 CIP 标签不被翻转）。本阶段不实现
立体化学指派系统（§十一）。

## 6. 当前只生成单个 conformer

`conformer_id = 0`，一次生成一个。不做 conformer ensemble、不做缓存、
不持久化（§十三，无新数据库）。

## 7. 不表示实验测得的真实构象

UI 明确标注「Generated conformer (ETKDG)」——不描述为「真实分子结构」。

## 8. API 使用方式

```text
GET /api/molecule/3d?smiles=CCO
GET /api/molecule/3d?smiles=CCO&optimize=true
```

返回（`core/models/conformer.py`）：

```json
{
  "canonical_smiles": "CCO",
  "formula": "C2H6O",
  "conformer_id": 0,
  "generation_method": "ETKDG",
  "optimized": false,
  "optimization_method": "",
  "charge": 0,
  "atoms": [{"atom_index": 0, "element": "C", "atomic_number": 6, "x": ..., "y": ..., "z": ...}],
  "bonds": [{"a": 0, "b": 1, "order": "single"}],
  "warnings": []
}
```

错误结构化，无 500 + traceback：

```json
{"error": {"code": "INVALID_MOLECULE", "message": "..."}}            // 400
{"error": {"code": "CONFORMER_GENERATION_FAILED", "message": "..."}} // 422
```

坐标是项目自己的 `MoleculeConformer` 数据模型，RDKit `Conformer` 对象从不
跨出 chem 层（§五）。

## 9. frontend viewer 使用方式

`web/src/components/MoleculeViewer.tsx`：无依赖的 canvas ball-and-stick。
拖拽旋转、滚轮缩放、Shift+拖拽平移、Reset View、Show Hydrogens（默认关 H）。
元素配色（CPK）在前端，后端不把「颜色」当化学事实保存（§七）。

Molecule Explorer 页新增「3D Structure」区：「Generate 3D」按需生成（idle /
generating / success / failed 四态，无假进度条），成功后显示 viewer +
Regenerate + Reset View。

## 10. known limitations

- 单个代表性 conformer，无 ensemble。
- 无 on-disk 缓存/持久化（on-demand 内存生成）。
- 优化只支持 MMFF94/UFF（RDKit 内置），无半经验/DFT。
- 大分子（>100 重原子）ETKDG 可能较慢或失败——失败是结构化错误，不是崩溃。
- viewer 是正交投影的 ball-and-stick，不做空间填充/表面/原子标注选中。

**Stage 2.1 到此停止，不自行进入 Stage 2.2。**
