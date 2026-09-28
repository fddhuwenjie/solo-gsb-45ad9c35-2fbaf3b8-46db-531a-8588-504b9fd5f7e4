# 盲文压印分页 API（Braille Embossing & Pagination API）

仅依赖 **Python 标准库**（`http.server` + `sqlite3`）的盲文缩写—分页联合决策服务。
接收正文、交叉引用、盲文规则、不可拆分区间、纸张容量和双面装订参数；用动态规划
**联合决定缩写与分页**，并为每个盲文单元保留到原文字素（grapheme）的映射。
规则多解、页码振荡、区间超宽或装订溢出时**不定稿**，返回最小冲突段和已验证前缀。
修订**只追加（append-only）**；定稿导出逐页 JSON、可打印 SVG 和可复演请求。

---

## 一条命令启动

```bash
docker compose up --build
```

服务监听在 **http://localhost:8080**（可用环境变量 `BRAILLE_PORT` 改宿主端口，
例如 `BRAILLE_PORT=9000 docker compose up --build`）。

数据保存在命名卷 `braille-data`（SQLite `braille.db` + 只读导出物）。

> 不使用容器时：`BRAILLE_DATA_DIR=./data BRAILLE_PORT=8080 python3 -m app.main`
> （需要 Python 3.10+）。

健康检查：

```bash
curl http://localhost:8080/health
# {"status":"ok"}
```

---

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BRAILLE_PORT`（compose 宿主端口） | `8080` | 宿主机映射端口；容器内固定 8080 |
| `BRAILLE_HOST` | `0.0.0.0` | 监听地址 |
| `BRAILLE_DATA_DIR` | `/data` | SQLite 与导出目录 |
| `BRAILLE_DB` | `$BRAILLE_DATA_DIR/braille.db` | SQLite 文件路径 |
| `BRAILLE_MAX_GRAPHEMES` | `200000` | 单份正文的字素上限（防内存耗尽） |

请求体内的参数：

| 字段 | 说明 |
| --- | --- |
| `body` | NFC 规范化后的正文字符串；偏移量按 **Unicode 码点**计 |
| `paper.cells_per_line` / `lines_per_page` | 纸张容量（每行盲文单元数 / 每页行数） |
| `paper.max_pages` | 可选，最大页数；超出即容量冲突 |
| `binding.enabled` / `duplex` / `gutter_cells` / `edge` / `max_sheets` | 装订：双面、装订槽（占用列）、`left`/`right`、最大纸张数 |
| `rules.rules[]` | 自定义缩写：`{id, pattern, cells, priority}`；`cells` 为点号串（如 `"124"`） |
| `rules.casefold` / `unknown_char` | 大小写折叠；未知字符 `warn`(全点 ⠿)/`reject`/`keep` |
| `cross_references[]` | `{id, char_start, char_end, target_start}`：区间渲染为“数字符+目标页码” |
| `unbreakable[]` | `{id, char_start, char_end}`：区间不得被**页**边界切开（行边界允许） |

未提供规则时使用内建一级英文盲文字母表、数字（含数字符 ⠼）、标点与大写标记（⠠）。

---

## 算法概要

1. **字素切分**：stdlib 实现 UAX#29 核心（CRLF、组合记号、ZWJ、谚文等），偏移按码点。
2. **词内缩写 DP**：对每个空白分隔词，在每个位置选择规则匹配（最长/最高优先级）或一级拼写回退；
   保留 **(宽度, 未缩写单元数) 下的全部非支配切分**——更宽但省下更多拼写单元的方案与最窄、
   全拼方案一并保留，每个盲文单元都带源字素/码点区间。求解为**迭代式**后缀 DP
   （从词尾向词头填表，不消耗调用栈），单候选位置跳过不可观测的流键计算。
3. **装箱 DP（与缩写联合）**：状态为光标 `(页, 行, 列)` + 已拼写单元计数；同页只严格剪枝
   “代价严格更差”的状态，等代价但光标更宽的路径保留（它可能在后续汇合为等优方案）。
   全局按 **布局合法 → 页数最少 → 未缩写单元数最少 → 末页行/列占用最少** 依次择优；
   若等优方案的**凸点流不同**，返回 `rule_ambiguity`（`equal_optimal_dot_stream`），不定稿。
4. **交叉引用不动点**：xref 宽度=数字符+目标页码位数，依赖分页自身；反复重排直到宽度向量
   收敛（收敛后再做等优凸点流判定，容量/装订等合法性冲突优先）。向量重复出现即**页码振荡**，
   检测循环并报告；页码迭代、冲突前缀与最终导出始终使用同一条最终链。
5. **冲突模式（严格/松弛两趟）**：严格趟禁入违规；松弛趟记录违规并最大化“首个违规下标”，
   从而定位**最小冲突段**，其前的发射即**已验证前缀**。

### 长词边界（无递归深度限制）

词内求解不消耗 Python 调用栈：单个无空白词的长度只受 `BRAILLE_MAX_GRAPHEMES`
字素上限约束，与解释器递归上限无关。例如正文为 **3163 个连续 `a`**、
`paper.cells_per_line=3163`、规则为空的请求可正常创建修订（`201`）并定稿；
旧版递归求解在约 1000 字素处耗尽调用栈，接口返回 `500` 且不落库。
迭代实现与原递归算法输出逐案一致（规则匹配、字素映射、候选去重、Pareto 剪枝、
稳定排序与模板缓存语义不变），测试中用递归参考实现做了播种模糊比对。

---

## API

### `POST /plan` — 只计算、不落库

成功 `200`；无法定稿（冲突）`422`，响应仍含 `conflict` 与 `verified_prefix`。

```bash
curl -s -X POST http://localhost:8080/plan \
  -H 'Content-Type: application/json' -d '{
    "body": "hello world singing test",
    "paper": {"cells_per_line": 10, "lines_per_page": 3},
    "rules": {"rules": [
      {"id": "ing", "pattern": "ing", "cells": ["346"]},
      {"id": "ll",  "pattern": "ll",  "cells": ["123"]}
    ]}
  }'
```

每个单元形如：

```json
{"dots": "123", "unicode": "⠇", "mask": 7, "col": 3,
 "grapheme_start": 2, "grapheme_end": 4,
 "char_start": 2, "char_end": 4, "rule_id": "ll"}
```

### 文档与修订（修订只追加）

| 方法/路径 | 作用 |
| --- | --- |
| `POST /documents/{doc}/revisions` | 追加一个修订并计算布局；`201` 新建 / `200` 同内容去重 |
| `GET  /documents/{doc}` | 文档元信息与全部修订（按序号） |
| `GET  /documents/{doc}/revisions/{n}?full=1` | 取修订；`full=1` 含完整分页，否则为摘要 |
| `POST /documents/{doc}/revisions/{n}/finalize` | 仅当状态 `ok` 可定稿；幂等拒绝重复定稿（`409`） |
| `GET  /documents/{doc}/finalizations/{n}` | 定稿元信息与摘要 |
| `GET  /documents/{doc}/finalizations/{n}/pages.json` | **逐页 JSON**（不可变，只读） |
| `GET  /documents/{doc}/finalizations/{n}/pages.svg` | **可打印 SVG**（每页一框、实点、页码） |
| `GET  /documents/{doc}/finalizations/{n}/replay.json` | **复演请求**（自包含，重放得到同一布局） |
| `POST /documents/{doc}/finalizations/{n}/verify` | **压印样张回读核验**（只读，不改定稿） |
| `POST /documents/{doc}/finalizations/{n}/compensate` | **故障压针补偿**（只读定稿，不重新分页/改写） |
| `POST /documents/{doc}/finalizations/diff/compare` | **定稿修订影响对比**（只读两份定稿，不重新规划） |

```bash
DOC=book
curl -s -X POST http://localhost:8080/documents/$DOC/revisions \
  -H 'Content-Type: application/json' -d @request.json
curl -s -X POST http://localhost:8080/documents/$DOC/revisions/1/finalize
curl -s http://localhost:8080/documents/$DOC/finalizations/1/pages.svg -o pages.svg
```

### `POST /documents/{doc}/finalizations/{n}/verify` — 压印样张回读核验

针对**已定稿**修订（未定稿返回 `404`），提交压印后扫描仪按页、行排列的实测盲文单元
掩码（整数 `0..63`，与逐页 JSON 的 `mask` 同编码）。接口只读取该定稿的逐页 JSON 做比对，
**不重新规划、不改写定稿**。

```bash
curl -s -X POST http://localhost:8080/documents/book/finalizations/1/verify \
  -H 'Content-Type: application/json' -d '{
    "pages": [
      {"lines": [
        [19, 17, 123, 123, 135, 0, 2456, 135, 1235, 123],
        [234, 124, 1345, 346, 0, 2345, 15, 234, 2345]
      ]}
    ]
  }'
```

规则与输出：

- 页数必须与定稿一致；每页实测**行数**必须与该页定稿一致，否则 `400 invalid_request`；
  掩码须为 `0..63` 的整数（布尔值、字符串、越界均非法）。行内实测单元数可多于/少于预期。
- 每行用**最小编辑代价**（替换/漏印/额外各代价 1）对齐实测与预期单元；同代价时按
  **匹配 → 替换 → 漏印 → 额外单元** 的次序裁决——因此一处漏印记为单个 `omission`，
  不会让其后所有单元错位。
- 响应清单：
  - `omissions`（漏印）：标 `expected` 的页/行/列，带预期掩码、点号及对应原文字素
    （`grapheme_start/end`、`char_start/end`、`text`）；
  - `extras`（额外单元）：标 `measured` 的页/行/行内序号（`cell_index`，从 1 起）及实测掩码/点号；
  - `dot_differences`（凸点差异，即替换）：同时标预期页行列与实测位置，列出
    `missing_dots`（缺失点号）与 `extra_dots`（多余点号），并带原文字素与码点区间。
- `pages[]` 为各页计数（`matched/substituted/omitted/extra/dot_differences/
  missing_dots/extra_dots/edit_cost/expected_cells/measured_cells`），`totals` 为总体汇总，
  `perfect=true` 且 `discrepancy_count=0` 表示样张与定稿完全一致。

> 实测页/行 JSON 也接受简写形式：页可直接给行数组（`"pages": [[[mask,...],...]]`）。

### `POST /documents/{doc}/finalizations/{n}/compensate` — 故障压针补偿

压印机部分压针损坏时，把纸张旋转 180° 可用对侧的好针打出同一个凸点：
半翻转把点号列上下左右对调（`1↔6、2↔5、3↔4`），页内坐标反变换为
`device_line = lines_per_page + 1 - line`、
`device_col = cells_per_line + 1 - col`、`device_dot = 7 - dot`。

接口针对**已定稿**修订（未定稿返回 `404`），只读取该定稿的**逐页单元**，
**不重新分页、不改写定稿**。请求给出：

| 字段 | 说明 |
| --- | --- |
| `working_dots` | 仍可工作的设备压针点号列表（`1..6` 整数、不可重复；空列表合法但非空定稿必然 `409`） |
| `orientations` | 允许的纸张方向，仅可取 `normal`、`rotate_180`；非空、不可重复，先后次序仅回显 |
| `max_passes` | 最多允许压印遍数，整数 `1..2`（物理方向只有两种，第三遍不可能再补出任何凸点） |

非法点号、未知/重复方向、遍数越界、类型错误（含布尔值）一律 `400 invalid_request`。

搜索与裁决：

- 先把 `rotate_180` 下的页内行列与点号按上述物理坐标反变换，再按
  **遍数最少**搜索方向组合；等长组合按固定次序 **`normal` 先于 `rotate_180`**
  破平局（与请求里的排列次序无关）。
- 每个目标凸点只分配给**首个可驱动它的遍次**（normal 能用就不留给 rotate），
  因此第一遍会压掉它能覆盖的全部凸点，旋转遍只补剩余凸点。
- 输出逐遍的**设备坐标掩码**（`pages[].lines[].cells[]`：
  `device_line/device_col/device_dots/mask`）及每个设备单元的**原页行列映射**
  （`source.page/line/col/dots`，带字素与码点区间）。各遍掩码反变换合并后
  **必须与原定稿逐点相等**——不缺点也**不能多压**，接口在应答前自行校验这一不变量。

无可行组合时返回 **`409 conflict`**：

- `reason=unreachable_dot`：某凸点在所有允许方向下都没有可用针（与遍数预算无关）；
- `reason=max_passes_exceeded`：各点单独都可达，但覆盖全部点所需遍数
  （`required_passes`）超过 `max_passes`。
- 响应列出**首个不可达单元**（`first_unreachable_cell`：页/行/列/点号/预期掩码/
  字素码点区间）、**缺失点号** `missing_dots`（全局不可达点号集合，去重升序）和
  **已验证页前缀** `verified_prefix`（该单元之前各页的页坐标重建、页数与凸点数）。

8080 端口调用示例（先创建并定稿修订 1，见上文）：

```bash
# 压针 3 损坏，允许正向 + 旋转、最多 2 遍
curl -s -X POST http://localhost:8080/documents/book/finalizations/1/compensate \
  -H 'Content-Type: application/json' -d '{
    "working_dots": [1, 2, 4, 5, 6],
    "orientations": ["normal", "rotate_180"],
    "max_passes": 2
  }'
```

成功响应（节选）：

```json
{
  "document_id": "book", "revision_no": 1, "status": "compensated",
  "working_dots": [1, 2, 4, 5, 6],
  "orientations": ["normal", "rotate_180"],
  "max_passes": 2, "pass_count": 2, "target_dot_count": 50,
  "passes": [
    {"pass": 1, "orientation": "normal", "dot_count": 39, "pages": [
      {"page": 1, "lines": [
        {"device_line": 1, "cells": [
          {"device_line": 1, "device_col": 1, "device_dots": [1, 2, 5],
           "mask": 19,
           "source": {"page": 1, "line": 1, "col": 1, "dots": [1, 2, 5],
                      "char_start": 0, "char_end": 1,
                      "grapheme_start": 0, "grapheme_end": 1}}]}]}]},
    {"pass": 2, "orientation": "rotate_180", "dot_count": 11, "pages": [
      {"page": 1, "lines": [
        {"device_line": 2, "cells": [
          {"device_line": 2, "device_col": 3, "device_dots": [4],
           "mask": 8,
           "source": {"page": 1, "line": 2, "col": 8, "dots": [3]}}]}]}]}
  ]
}
```

不可行时（仅允许正向且压针 3 损坏）：

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  http://localhost:8080/documents/book/finalizations/1/compensate \
  -H 'Content-Type: application/json' -d '{
    "working_dots": [1, 2, 4, 5, 6],
    "orientations": ["normal"], "max_passes": 1
  }'
# 409
```

> 该接口为纯只读计算：不触碰修订只追加约束，定稿摘要与导出物的哈希保持不变。

### `POST /documents/{doc}/finalizations/diff/compare` — 定稿修订影响对比

给出同一文档的**两个已定稿**修订号（`old_revision`、`new_revision`，必须互不相同）；
接口只读取两份修订各自的**正文与逐页数据**，**不重新规划、不改写任何定稿**。
任一修订不存在或**未定稿**返回 `404`；修订号非法或两号相同返回 `400`。

```bash
curl -s -X POST http://localhost:8080/documents/book/finalizations/diff/compare \
  -H 'Content-Type: application/json' -d '{
    "old_revision": 1,
    "new_revision": 2
  }'
```

对齐与标记规则：

- 两侧正文先按 **Unicode 字素簇**（与规划器同一 UAX#29 核心切分）切分，再对**字素文本**
  做**最小编辑对齐**（替换/删除/插入代价均为 1）。**码点坐标不参与判等**：插删会推移
  后续字素的坐标，但相同字素仍被正确配对——边界示例：旧 `aa` vs 新 `acaba`，保留的
  第二个 `a` 与新正文下标 2 处的 `a` 配成匹配（仅分页位移），操作流为
  `匹配, 插入, 匹配, 插入, 插入`，而不会误判成替换。操作流只携带两侧各自的真实下标，
  每个字素至多消费一次。同代价依次选择 **匹配 → 替换 → 删除 → 插入**，由后缀 DP 表
  加贪心回溯实现，重复文本（如 `abab→ab`）下结果稳定，删除一律落在尾部而不会把后续
  字素错位；公共前缀直接裁剪，公共后缀仅当其字素文本不出现在两侧中段时才裁剪——
  前后缀裁剪绝不改变操作流，近邻修订只需对齐中间差异段。中段过大（`n*m` 超过表项上限）时
  改用**对角束带精确 DP**：束带半宽从编辑距离下界（长度差、逐字素计数失衡）起倍增，直到束带内
  的 `rest(0,0)` 可证明精确。每条最小编辑流都落在半宽 = 编辑距离的束带内，且束带中真实代价
  不超过半宽的单元取值与完整表一致，因此贪心回溯输出的操作流与完整二维 DP **逐字相同**
  （同代价仍按 匹配→替换→删除→插入），差异段、码点范围与重压页均不受影响。
- 每个已配对字素比较其盲文**掩码序列**与**页/行/列**：
  - 掩码不同 → `retranslated`（**盲文重译**，规则/上下文变化导致凸点变化）；
  - 掩码相同但页行列变化 → `pagination_shift`（**仅分页位移**，凸点不变）；
  - 完全一致 → `unchanged`（**完全不变**）。
  编辑操作分别记为 `body_substituted`（正文**改**）、`body_deleted`（正文**删**）、
  `body_inserted`（正文**增**）。
- 相邻的变化标记合并为一个**差异段** `segments[]`，带
  `old_char_start/end`、`new_char_start/end`（Unicode **码点**半开区间，纯增/纯删侧为
  `null`）与 `old_page_range`、`new_page_range`（受影响**页范围**，无该侧时为 `null`）、
  段内 `kinds` 与 `mark_count`；`changes[]` 为逐字素明细（含各自旧/新码点区间、文本与所在页）。
  **纯删除**在新版没有字素，但仍给出**新版重压落点页**：删除点之后第一个新版字素所在页
  （删至文末时为最后一个保留字素所在页），计入该段的 `new_page_range` 与
  `new_reemboss_pages.content_changed`——新版该页内容确实减少，需要重新压印。

页影响清单（关键产出）：

- `old_affected_pages` —— **旧版受影响页**；`new_reemboss_pages` —— **新版需重新压印页**；
- 两者各自区分 `content_changed`（**内容变化**：正文增删改或盲文重译，凸点确实不同）
  与 `cascade_reflow`（**连锁重排**：凸点完全相同，只是位置被挤到别的页/行/列）；
- 两类列表互不相交（同页同时有内容变化时计入 `content_changed`），并附 `count`。
  典型情形：文末追加内容时，旧版可能完全无受影响页，新版仅追加页需要压印；
  文末截断（纯删除）时，新版最后一个保留字素所在页（落点页）需要压印；
  正文/纸张参数导致整体重排而凸点流不变时，所有移动页都归入 `cascade_reflow`。

对齐的边界行为（内存守卫）：

- 对齐表项上限为 **10,000,000 格**（每格 4 字节）。裁剪后的中段 `n*m` 不超过上限时按
  完整后缀表一次算清；超过时启用上述束带 DP，内存约为 `(2k+1) × 中段长度`
  （k 为两侧正文的编辑距离），与正文长度解耦。
- 因此**长重复正文上的近似修订不再被误拒**：例如旧文为 3163 个 `a`、新文仅把首字改为
  `c`（公共后缀守卫无法裁剪，完整表需 3163×3163 ≈ 一千万格，旧实现返回
  `400 invalid_request`），束带只需约 3×3163 格即完成，且操作流、差异段、码点范围、
  重压页与未裁剪算法完全一致。
- **真正高差异**的两份长文（编辑距离大，束带随 k 增宽）所需表项仍会超过上限，
  接口返回 `400 invalid_request`（`revisions differ too much to align`）——
  资源保护行为不变，只是拒绝判据从 `n*m` 改为实际所需的束带表项数。

响应还含 `identical`、`edit_cost`、各类标记计数 `counts`、两侧页数/字素数与摘要哈希：

```json
{
  "document_id": "book",
  "status": "compared",
  "identical": false,
  "old_revision_no": 1,
  "new_revision_no": 2,
  "old_page_count": 4,
  "new_page_count": 5,
  "edit_cost": 8,
  "counts": {"body_substituted": 0, "body_deleted": 0, "body_inserted": 8,
             "retranslated": 0, "pagination_shift": 0, "unchanged": 66},
  "segments": [
    {"kinds": ["body_inserted"], "mark_count": 8,
     "old_char_start": null, "old_char_end": null,
     "new_char_start": 66, "new_char_end": 74,
     "old_page_range": null, "new_page_range": {"start": 4, "end": 5}}
  ],
  "old_affected_pages": {"content_changed": [], "cascade_reflow": [], "count": 0},
  "new_reemboss_pages": {"content_changed": [4, 5], "cascade_reflow": [], "count": 2}
}
```

> 接口只做只读比对，不触碰修订只追加约束，也不生成或修改任何导出物。

### 确定性 / 键序无关

持久化与摘要统一使用“排序键 + 紧凑分隔”的规范 JSON 与 SHA-256；
**调换请求 JSON 的键顺序不会改变摘要或布局**，同内容修订自动去重。

### 冲突响应（不生成定稿）

```json
{
  "status": "conflict",
  "conflict": {
    "kind": "interval_too_wide",
    "interval_id": "u1",
    "reason": "page break forced inside unbreakable interval",
    "line_capacity": 5,
    "minimal_segment": {"char_start": 3, "char_end": 11,
                        "grapheme_start": 3, "grapheme_end": 11,
                        "text": "cd ef gh"}
  },
  "verified_prefix": {"pages": [ /* 已验证的页 */ ],
                      "page_count": 1, "char_end": 6,
                      "grapheme_end": 6, "emitted_cells": 5}
}
```

冲突类型 `kind`：

- `rule_ambiguity` —— 规则多解（同模式不同展开且优先级不能裁决、同位置等优匹配，
  或全局择优下页数/未缩写单元数/末页行列全部相等但**凸点流不同**，`reason=equal_optimal_dot_stream`）；
- `page_number_oscillation` —— xref 页码宽度不收敛（含循环宽度向量与振荡的 xref id）；
- `interval_too_wide` —— 不可拆分区间无法放入同一页 / 内含超行宽原子；
- `content_too_wide` —— 原子本身比一行可用宽度更宽；
- `capacity_overflow` / `binding_overflow` —— 超出 `max_pages` / `max_sheets`。

HTTP 状态：`400` 请求非法，`404` 不存在，`409` 状态冲突（重复定稿/定稿冲突修订），
`422` `/plan` 计算成功但无法定稿，`500` 服务器故障。

---

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：缩写与单元映射、非支配切分前沿与（宽度,未缩写单元）取舍、列容量、
数字符/大写标记、规则多解（静态、动态与等优凸点流）、全局择优次序、优先级裁决、
区间整体移动/超宽、双面/装订槽/空白补页、xref 渲染与不动点收敛、键序无关确定性、
CRLF/组合字字素、仅追加存储、去重、冲突不可定稿、不可变导出物，合成历史的页码振荡，
以及压印样张回读的最小编辑对齐（漏印不级联）、凸点差异/额外/漏印清单、结构非法与 HTTP 接口，
还有故障压针补偿（180° 物理反变换、最少方向组合、normal/rotate_180 固定破平局、
每点只进首个可用遍次、设备坐标掩码与原页行列映射、合并不多不缺、不可达点/遍数超限 409
与已验证页前缀、非法点号/重复方向/遍数越界 400、只读不改定稿与 HTTP 接口），
还有定稿修订影响对比的字素最小编辑对齐（仅按字素文本判等、坐标移位不误判、
重复文本稳定、同代价匹配→替换→删除→插入、前后缀裁剪不改变操作流、
束带 DP 与完整表引用逐字一致、长重复正文单字修订不误拒、高差异长文仍拒绝）、
正文增删改/盲文重译/仅分页位移/完全不变分类、相邻变化合并段与旧新码点/页范围、
纯删除的新版重压落点页、内容变化与连锁重排页清单、未定稿拒绝与 HTTP 接口，
以及超递归上限长词的迭代词内求解（3163 字素边界可创建修订并定稿、
与递归参考实现逐案一致、数字符/规则匹配/模板缓存语义保持）。
