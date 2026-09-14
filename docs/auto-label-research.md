# 图片 AI 自动标注 —— 开源方案调研与接入设计

> 调研时间：2026-09-14 ｜ 面向 echo v1.4「模型预标注」扩展（方案 P2）
> 结论先给：**不引入任何 AGPL/GPL 依赖**，用 `onnxruntime`（MIT）+ Apache-2.0/MIT 权重做零样本预标注，
> 再叠一条「相似帧标注传播」和一条「自训练小模型 ONNX 回灌」的闭环。

---

## 一、先把需求说清楚

「AI 自动标注」在数据集工具里不是一个功能，是三种不同价值的东西，混在一起做必然翻车：

| 层次 | 用户能感知的价值 | echo 现状 |
| --- | --- | --- |
| **A. 预标注初稿** | 几百张图不用从零画框，AI 先出一版，人只改错 | 无，`labels.py` 已有 YOLO txt 读写 |
| **B. 标注传播** | 连续帧几乎一样，标一张顶标一百张 | 感知哈希去重/相似分组**已有**，可白捡 |
| **C. 训练闭环** | 人工标 50~200 张 → 训个小模型 → 回来批量标剩下的 | 无，但 `dataset/exporter.py` 已能导出 YOLO |

真正省时间的顺序是 **B → C → A**：先白捡相似帧，再用小样本训练把精度拉到可用，最后才考虑通用模型。
只做 A（拿通用模型硬标）在**游戏画面**上性价比最低——原因见下一节。

---

## 二、四条硬约束（决定了选型范围）

| 约束 | 说明 | 影响 |
| --- | --- | --- |
| **许可证：仓库是 MIT** | 一旦把 AGPL/GPL 代码打进分发物，整个 echo 的许可证就被"污染"，必须改成 AGPL/GPL | ⚠️ **直接排除 Ultralytics 全家桶**（AGPL-3.0）、YOLO-World（GPL-3.0）、X-AnyLabeling（GPL-3.0）作为**内嵌依赖** |
| **离线 + Windows 打包** | PyInstaller 目录模式分发，用户可能是内网/断网环境 | 模型权重不能打进包（几百 MB），改成"首次使用时下载 / 手动导入"，推理走 ONNX Runtime |
| **用户可能没有独显** | 多数是笔记本核显/入门独显 | 必须有一条纯 CPU 能跑的路径，且速度要能接受 |
| **领域偏移：游戏画面** | COCO/ImageNet 预训练模型**认不出游戏里的物体**（Subnautica 的鱼、Rust 的建材都不在 COCO 里） | 通用检测器（YOLO11/YOLO26 的 .pt）预标注召回极低，**不能用它当主力** |

> 第 4 条最容易被忽略。很多人第一反应是"接个 YOLOv8 打标签"，实际在游戏截图上会得到一片空白——
> 那不是配置问题，是模型压根没见过这种域。所以开放词汇模型（文字提示）和自训练才是正解。

---

## 三、开源方案清单（按许可证分层）

### ✅ 可安全内嵌（Apache-2.0 / MIT）

| 项目 | 许可证 | 能力 | CPU 可行性 | 备注 |
| --- | --- | --- | --- | --- |
| **onnxruntime** | MIT | 推理运行时 | ✅ | `pip install onnxruntime`，PyInstaller 友好，无 CUDA 也能跑 |
| **Grounding DINO**（IDEA Research） | Apache-2.0 | **开放词汇检测**，给文字提示（`"fish. shark."`）直接出框 | ⚠️ 慢：800px 单图约 **6 s**（CPU EP，实测数据） | 有 tiny 版 ONNX：`onnx-community/grounding-dino-tiny-ONNX`（fp32 约 695 MB，官方仓库含多种精度共 2.29 GB） |
| **SAM / SAM 2**（Meta） | Apache-2.0 | 提示式分割（给框出掩码） | ✅（MobileSAM/EfficientSAM 轻量版） | 仅"人工在环精修"用，不必进自动流程 |
| **MobileSAM / EfficientSAM** | Apache-2.0 | 轻量分割，配 Grounding DINO 组成 **Grounded-SAM** | ✅ | ONNX 版 encoder+decoder 合计几十 MB |
| **Grounded-SAM / Grounded-SAM-2**（IDEA） | Apache-2.0 | 检测+分割流水线的现成实现 | ⚠️ | 代码可直接参考，但依赖 torch，echo 里建议只抄 ONNX 流程 |
| **Florence-2**（Microsoft） | **MIT** | 一个模型做检测/分割/描述/OCR（`<OD>`、`<CAPTION_TO_PHRASE_GROUNDING>`） | ⚠️ 中等：base 230M，CPU 单图秒级 | 许可证最干净，且能干"图像描述"，适合做数据集自动打标签/洗数据 |
| **ONNX Runtime + 自训练 YOLO** | — | 用户自己训的小模型导出 `.onnx`，echo 只做推理 | ✅ 极快（CPU 单图 30~100 ms） | **echo 不依赖 ultralytics**，训练在外部完成即可 |

### ⚠️ 只能用「外挂/用户自装」，不能内嵌

| 项目 | 许可证 | 为什么不内嵌 | 怎么用 |
| --- | --- | --- | --- |
| **Ultralytics**（YOLO11/YOLO26/YOLOE、`auto_annotate`） | **AGPL-3.0** | 内嵌 = echo 必须整体 AGPL；商用/闭源都要买 Enterprise 授权 | 用户自己 `pip install ultralytics` 训练，导出 `.onnx` 后交给 echo 推理。echo 代码里**不 import** |
| **YOLO-World** | GPL-3.0（继承 Ultralytics） | 同上 | 同上（或用 Grounding DINO 替代） |
| **X-AnyLabeling** | **GPL-3.0**（v4.0.0，约 9.9k★，2026-08 仍活跃） | 功能极全（SAM 1/2/3、Grounding DINO、YOLO 全系、Qwen3-VL、ONNX/TensorRT、COCO/VOC/YOLO 导出、中文界面），但 GPL 内嵌会传染 | **推荐作为"姊妹工具"**：echo 负责采集+去重+AI 预标注，精细标注/复核交给 X-AnyLabeling，两边用 YOLO txt 对接（echo 已支持导入导出） |
| **SAM 3**（2025-11 发布） | 自定义许可证 | 条款需单独审阅 | 暂不接入 |

### 📌 参考实现（不改代码，只学做法）

| 项目 | 许可证 | 学什么 |
| --- | --- | --- |
| **Autodistill**（Roboflow，约 2.8k★） | Apache-2.0（**插件各自许可证，`autodistill-yolov8` 会拉 ultralytics**） | "基座模型打标签 → 训练小模型"的标准工作流与 ontology（提示词↔类名映射）设计 |
| **Label Studio / CVAT** | Apache-2.0 / MIT | 服务端方案，对单机小工具过重，不接 |
| **labelme** | MIT | 若未来要做内置编辑器，可参考其标注数据格式（但我们会用 YOLO txt） |

---

## 四、四条路线对比与推荐

| 路线 | 依赖/体积 | 速度（CPU） | 精度（游戏画面） | 许可证风险 | 结论 |
| --- | --- | --- | --- | --- | --- |
| **A. 零样本开放词汇**（Grounding DINO tiny + MobileSAM，ONNX） | 权重 ~200 MB~700 MB，首次下载 | 约 **6 s/图** | 召回尚可、误检偏多，适合出"初稿" | ✅ 无 | **做**：作为"没有训练数据时的第一步" |
| **B. 相似帧标注传播** | **0 依赖** | 毫秒级 | 对连拍帧接近 100% | ✅ 无 | **先做**：echo 已有 phash，标一张传一组，性价比最高 |
| **C. 自训练小模型回灌**（外部训练 → `.onnx` → echo 推理） | 权重几 MB | **30~100 ms/图** | **最高**（同域训练） | ✅ 只要 echo 不打包 ultralytics | **做**：主力路径，形成"标→训→标"闭环 |
| **D. VLM/云 API 打标**（Qwen3-VL / GPT 等） | 需 API Key + 联网 | 网络决定 | 中等，且能出描述 | ✅（用户自带 key） | **可选**：做成"用户自填 key"的可选通道，默认关闭 |

**推荐组合：B（立刻可用）→ C（主力）→ A（冷启动兜底）→ D（可选）**

---

## 五、接入设计（echo v1.4 草案）

### 5.1 模块划分

```
echo/autolabel/
├─ __init__.py          # 对外门面：AutoLabeler.run(image_paths, spec, progress_cb)
├─ registry.py          # 模型清单：id / 名称 / 许可证 / 体积 / sha256 / 下载地址 / 输入尺寸
├─ store.py             # 模型下载与本地缓存（%APPDATA%\echo\models\），断点续传 + 校验
├─ engines/
│   ├─ base.py          # 统一接口：load() / infer(bgr) -> list[Box(class_id, xyxy, score)]
│   ├─ onnx_zero_shot.py# Grounding DINO + MobileSAM（开放词汇，文字提示）
│   ├─ onnx_yolo.py     # 用户导入的 .onnx（自训练模型，含 YOLO/RT-DETR 输出解析）
│   └─ propagate.py     # 相似帧标注传播（复用 quality.find_similar）
├─ prompts.py           # 提示词配置：{"fish": 0, "shark": 1} ↔ 类名表，落盘 classes.txt
└─ writer.py            # 结果写成 YOLO txt（复用 dataset/labels.py），原子落盘
```

### 5.2 与现有代码的接缝

| 现有部件 | 接法 |
| --- | --- |
| `quality.find_similar()` | 标注传播直接用相似分组，零新依赖 |
| `storage/db.py` | `images` 表加 `auto_labeled` 标记 + `label_source`（human/auto/propagated），**AI 结果默认不进 `labeled` 状态**，避免污染"已标注"口径 |
| `dataset/labels.py` | 复用 YOLO txt 读写，保证导出格式一致 |
| `dataset/importer.py` | 与"标注导入"共用校验逻辑（越界框、类别 id 一致性） |
| `ui/pages/library_page.py` | 新增「AI 预标注」按钮 + 进度条；复核态用不同颜色边框区分"AI 标的/人改过的" |
| `ui/pages/settings_page.py` | 「模型管理」卡片：下载/删除/导入本地 onnx、显示许可证与体积 |

### 5.3 交互流程（建议）

```
图片库 → 勾选若干张（或"全部未标注"） → 点【AI 预标注】
  → 选引擎：相似帧传播（秒级） / 开放词汇（慢，需先下模型） / 我的模型（.onnx）
  → 开放词汇填提示词：fish, shark, plant    → 确认
  → 后台线程跑，逐图进度 + 可随时中止
  → 完成后：标注写入 YOLO txt，DB 标 label_source=auto
  → 界面高亮可选："AI 标了 N 个框，置信度 < 0.4 的有 M 个"（人工优先看这些）
```

### 5.4 关键工程决策

| 决策 | 选择 | 理由 |
| --- | --- | --- |
| 推理运行时 | **onnxruntime**（CPU EP） | MIT、无 torch 依赖、PyInstaller 打包体积可控（约 15~40 MB） |
| 权重分发 | **不打包**，首次使用按需下载到 `%APPDATA%\echo\models\` | 几百 MB 打进安装包不现实；内网用户支持手动导入 zip |
| 是否内置 torch | **否** | torch 会让分发包从 ~120 MB 涨到 1 GB+，且 CUDA 版本不可控 |
| 是否内嵌 ultralytics | **否** | AGPL 传染；训练交给外部，echo 只吃 `.onnx` |
| AI 标注的状态 | 单独标记，**不计入"已标注"** | 方案 §5.2 的审核口径不能被自动结果污染 |
| 速度策略 | 相似帧传播优先；开放词汇只对"代表帧"跑（利用现有相似分组去重） | 把 6 s/图的成本从 500 张压到 50 张 |

---

## 六、落地顺序建议

| 阶段 | 内容 | 依赖 | 预计改动 |
| --- | --- | --- | --- |
| **v1.4-a** | 相似帧标注传播 + AI/人工标注来源标记 | 无新依赖 | `autolabel/propagate.py`、db 字段、库页按钮 |
| **v1.4-b** | 自训练 `.onnx` 导入 + 批量推理 | `onnxruntime` | `autolabel/engines/onnx_yolo.py`、模型管理卡片 |
| **v1.4-c** | 零样本开放词汇（Grounding DINO tiny ONNX） | onnxruntime + 权重下载 | `onnx_zero_shot.py`、提示词界面、下载管理 |
| **v1.4-d**（可选） | VLM/云 API 通道（用户自填 key） | 无 | 一个 engine 实现 + 设置项 |

**建议先做 v1.4-a**：零模型、零下载、当天能用，而且对"固定选区连拍"这种 echo 的主力使用方式效果最好。

---

## 七、风险与坑（提前记下来）

1. **许可证**：任何"顺手 pip install ultralytics"都会把 echo 变成 AGPL。CI 里加一道依赖检查（`pip list` 断言无 `ultralytics`）。
2. **模型体积与网络**：Grounding DINO tiny fp32 约 695 MB；官方 ONNX 仓库含多精度共 2.29 GB。要提供 int8 量化版并显示下载进度，失败可续传。
3. **CPU 速度**：Grounding DINO 的 deformable attention 在 ORT 标准版里没有 CUDA kernel，**GPU 和 CPU 一样慢（约 6 s/图）**，只有 TensorRT EP 能到 ~70 ms。别宣传"有显卡就快"。
4. **多类提示的排版陷阱**：社区版 ONNX 导出把提示词的分隔循环固化在计算图里，多类提示（`"a. b. c."`）会**静默丢类且与词序相关**；已有修正版导出（fixedmask，695 MB）或改为"每类跑一次"。
5. **误检与漏检**：自动标注是"初稿"不是"真值"。界面上必须让低置信度框显眼，绝不能默认当 ground truth 导出（会训练出自证循环的坏模型）。
6. **类别 id 一致性**：提示词→类名的映射必须落盘（`classes.txt`），否则下次标注 id 漂移，导出的数据集标签全错。
7. **相似帧传播的边界**：只在**同一批次、同一选区**内传播；跨批次/跨选区的"相似"会把框错位（echo 的 phash 是全局的，传播前必须按 `region` 分组二次确认）。
