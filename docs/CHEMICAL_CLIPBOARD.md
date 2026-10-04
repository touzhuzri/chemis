# Chemical Clipboard / ChemDraw Interoperability — 调查记录

> §19 要求：**在没有验证的情况下不得假设 clipboard 格式。** 本文严格区分
> 【已验证】与【待验证】。目标是未来实现
> `ChemDraw Ctrl+C → CREA → ChemicalEntity` 与
> `CREA → Copy Structure → ChemDraw Ctrl+V`。

## 1. 本次调查环境与结论

【已验证】本机（Windows 11）现状：

- **未安装 ChemDraw / PerkinElmer 套件**（`C:/Program Files/PerkinElmer`
  等路径不存在）→ **无法在本机实测 ChemDraw 的 clipboard 行为**。
- Python 环境无 `win32clipboard` / `pyperclip`（未来实现需引入 pywin32
  或 win32 API 调用）。
- 外部网页核查被网络策略阻断（WebFetch 域名校验失败；revvity.com 对
  自动请求返回 403）→ **无法取得官方 clipboard 文档**。

因此本文对具体格式**不下结论**。

## 2. 待验证假设（社区流传，全部标记 UNVERIFIED）

以下仅是需要验证的问题清单，不是事实：

| # | 假设 | 验证方法 |
|---|---|---|
| H1 | ChemDraw Ctrl+C 同时提供 CDXML（文本格式的 ChemDraw 结构） | Windows 下用 Win32 `EnumClipboardFormats` 枚举 |
| H2 | 同时提供旧版二进制 CDX（`ChemDraw CDX` format） | 同上；`GetClipboardFormatName` 记录注册名 |
| H3 | 同时提供 MDL MOLfile（V2000/V3000） | 同上；取数据验证可解析 |
| H4 | 同时提供纯文本 SMILES/InChI | 取 CF_UNICODETEXT 验证 |
| H5 | 同时提供位图/图元文件（显示用，不作身份） | 枚举图片类格式 |
| H6 | 其他软件（MarvinSketch/MolView/Biovia）互操作以 MOL/SMILES 为公约数 | 各软件复制后枚举对比 |
| H7 | CDXML 有公开的 File Format Specification（Revvity/PerkinElmer 文档） | 人工在可访问网络下读取并引用版本 |

## 3. 标准验证流程（在装有 ChemDraw 的 Windows 机器上执行）

1. 安装 `pywin32`（开发依赖，MIT 许可已核）。
2. ChemDraw 中画苯乙炔 → Ctrl+C。
3. 枚举并记录：

```python
import win32clipboard as w
w.OpenClipboard()
fmt = 0
while True:
    fmt = w.EnumClipboardFormats(fmt)
    if fmt == 0: break
    try: name = w.GetClipboardFormatName(fmt)
    except Exception: name = "?"
    data = w.GetClipboardData(fmt)
    print(fmt, name, type(data).__name__,
          (data[:60] if isinstance(data, (bytes, str)) else ""))
w.CloseClipboard()
```

4. 对每个格式尝试解析（CDXML→XML 读结构；MOL→RDKit；text→SMILES 试解析
   但**不猜**——失败即失败）。
5. 反向验证：CREA 侧 `SetClipboardData` 分别写 CDXML/MOL/SMILES，在
   ChemDraw Ctrl+V 测哪(s)个被接受。
6. 结果回填本文第 2 节表格（UNVERIFIED → CONFIRMED/REFUTED），并在
   docs/LICENSES.md 记录 CDXML 规范的使用条款（格式规范的版权/使用
   条款需单独核读）。

## 4. 设计约束（无论验证结果如何都成立）

- **身份永远来自结构解析，不来自图片**：剪贴板位图若无结构表示，只能
  走 DECIMER 路线并标记 `PROBABLE/AMBIGUOUS`（§21），绝不当 EXACT。
- **文本 SMILES 若存在**，按 `resolver.py` 正常解析；解析失败原样报错，
  不"修正"。
- **CDXML/MOL 若存在**，走 `representations.py` 标准入口；格式版本问题
  作为解析失败返回，不猜。
- 复制方向只写开放/已验证的格式（优先 MOL/SMILES），CDXML 写出仅在
  规范条款核读后启用。
- 未来 clipboard 集成是 **input/output adapter**，不进核心身份系统。

## 5. 状态

| 项 | 状态 |
|---|---|
| 本机 ChemDraw 实测 | **BLOCKED（无软件）** |
| 官方格式文档核读 | **BLOCKED（网络策略）** |
| 验证流程 | 已定稿（§3），可在具备条件的机器上执行 |
| 对 CREA 4.0 当前阶段的影响 | 无——Stage B/C 不依赖 clipboard；仅约束未来 adapter 设计 |
