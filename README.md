# CoronaTests

独立的 CoronaEngine 场景渲染回归仓库。提供下载场景、下载正式 golden、生成 golden 网页及执行回归入口；引擎以外部运行包提供，运行时不导入 CoronaEngine 源码或历史实验脚本。

所有入口都是普通 Python 程序，由脚本循环遍历场景与算法，不调用 AI agent，也不需要启动 Codex 或连接任何 AI 服务。场景及 golden 按固定清单下载，渲染与图像比较在本机完成。

## 浏览全部 Golden 图片

clone 仓库并完成 Git LFS 图片拉取后，直接双击根目录的 [index.html](index.html)，即可离线浏览全部 **92 张正式图片**、各算法耗时和局部放大。网页直接引用 `golden/<场景名>/*.png`，不保存重复图片，也不需要启动服务器、安装 Python 或配置引擎。

如果 clone 时没有自动拉取 LFS 图片，先在仓库目录执行 `git lfs pull --include="golden/**"`。正式图片或清单更新后，维护者可运行 `python scripts/generate_gallery.py` 重新生成并提交 HTML；浏览者只需打开现成的 HTML。

## 首次准备

安装 Git LFS，并使用 Python 3.11 或更高版本，在仓库根目录准备数据和依赖：

```powershell
git lfs install
git lfs pull --include="golden/**"
python -m pip install -r requirements.txt
Copy-Item config.golden.example.json config.golden.local.json
Copy-Item config.regression.example.json config.regression.local.json
```

分别在 `config.golden.local.json` 和 `config.regression.local.json` 填写本机 `engine_executable`。两份文件完全独立，可以指定各自的引擎和采样参数。运行包需要的额外 DLL 目录填入 `runtime_dirs`，按查找顺序排列；完整部署包可以留空。引擎需要包含 CEF 前端、公开场景/截图/配置 API 及 GPU timestamp 诊断能力。程序会检查实际证据，能力不足会保留日志并报错。

换机器后克隆仓库并完成 Git LFS 拉取即可获得正式基线。首次执行回归时，程序按 `baselines/default.json` 验证 `golden/`；若 PNG 仍是 LFS 指针，联网模式会执行受限的 `git lfs pull` 后重新校验。也可以先双击 `download_golden.cmd` 核验和准备基线。场景仍按 `scenes/catalog.lock.json` 下载。机器需要受支持的 Windows/NVIDIA 环境、上述 Python 依赖和可运行的 CoronaEngine 运行包；路径在本机配置，不要求使用原采集机器的 `D:/work`。

当前仓库及 Git LFS 为私有资源，图片拉取使用现有 Git 认证（HTTPS 凭据或 SSH），需要仓库读取权限。凭据不写入仓库或基线文件。

正式图片直接放在 `golden/<场景名>/`，文件名为 `gi.png`、`pt-reblur.png`、`pt.png`。目录内仅保留 92 张正式 PNG，PNG 由 Git LFS 保存实际内容；图片清单、采样配方、耗时摘要和比较阈值放在 `baselines/`。本机配置、场景缓存和临时回归输出继续忽略。工具从引擎可执行文件所在目录启动进程，运行结束关闭本次启动的引擎。

## 双击运行

日常运行只需编辑对应配置文件、保存，然后双击入口；不需要在启动时输入参数。先确保当前 `python` 已安装上述依赖且本机引擎已重新编译。

| 双击入口 | 参数文件 | 作用 |
| --- | --- | --- |
| `generate_golden.cmd` | `config.golden.local.json` | 全部 35 场景、三种算法，生成新的 golden |
| `run_full_regression.cmd` | `config.regression.local.json` | 全部 35 场景、三种算法，执行回归 |
| `run_regression.cmd` | `config.regression.local.json` | 双击时与全量回归入口相同 |
| `download_scenes.cmd` | 固定资源清单 | 单独准备全部 35 场景；渲染入口也会自动准备缺失资源 |
| `download_golden.cmd` | `baselines/default.json` | 核验仓库内 golden，补齐 Git LFS 图片，不启动引擎 |

两个渲染入口自动为每次运行命名并建立独立目录，结束后自动打开已有的 HTML 报告，保留命令窗口显示通过或失败。各自的引擎路径、SPP、历史帧数和预热从对应文件读取；回归文件中的 `golden` 为空时，使用仓库锁定的发布版本，缺失时自动下载。显式 `--golden` 或配置中的本地路径保持优先；也可用 `--golden-id` 选择其他已发布版本。没有受版本管理的默认清单时，才兼容旧的本机最近成功基线索引。旧的共享 `config.local.json` 不参与默认配置选择。

两个渲染入口在缺少场景时自动调用下载准备，无需手工解压或修改 XML。生成 golden 时每个场景、每种算法只执行一次正式采集，不进行重复噪声校准。按当前支持边界，全量执行 105 项检查，输出 92 张正式 PNG，另 13 项记录已知不支持状态。setup 验证图不保留，局部放大直接引用原图，不额外生成 PNG。

生成的 `index.html` 可直接离线打开，按场景三列展示算法、原始 PNG、局部放大及耗时。回归报告展示 golden、候选图和差分。所有网页资源均为本地相对路径，golden ZIP 可整体迁移。

## 自定义 SPP 和帧数

分别在两份配置文件中修改 `sampling`，例如 golden 的 `pt_spp` 设为 2048，回归的 `pt_spp` 设为 512。两份文件使用相同字段结构，修改其中一份不会影响另一份：

```json
"sampling": {
  "pt_spp": 512,
  "gi_frames": 192,
  "pt_reblur_frames": 192,
  "warmup_frames": 64
}
```

`pt_spp` 是纯 PT 最终累积的总样本数，仍以每帧 1 spp 连续累积。`gi_frames` 和 `pt_reblur_frames` 是各自时域历史的运行帧数，不启用 RGB 累积；`warmup_frames` 只从稳定计时统计中排除前若干帧，所有正式帧仍参与图像生成和总耗时。帧数/SPP 必须为正整数，预热必须非负且小于每个所选算法的帧数。例如测试 32 spp 时，可将预热设为 0 或 16。

生成 golden 后，可以在回归配置中独立修改 SPP，再双击 `run_full_regression.cmd` 执行比较。启动时无需填写 SPP、基线 ID 或输出路径。

<details>
<summary>供自动化调用的命令行接口</summary>

带参数调用保留命令行行为，不暂停；两个入口参数一致，命令行覆盖仅影响本次运行：

```powershell
.\generate_golden.cmd --baseline-id pt512 --pt-spp 512
.\run_regression.cmd --golden .workspace/experiments/golden/pt512 --pt-spp 512

# 单独下载正式基线，或选择固定的已发布版本执行回归
.\download_golden.cmd --baseline-id full-20261116-v2
.\run_regression.cmd --golden-id full-20261116-v2

# 三种预算分别指定，仍默认运行全部场景
.\generate_golden.cmd --baseline-id custom-budget --pt-spp 512 --gi-frames 128 --pt-reblur-frames 128 --warmup-frames 32
```

</details>

不同 SPP、历史帧数或预热的结果允许比较。回归模板默认纯 PT **512 spp**，golden 模板仍为 **2048 spp**；这一配对采用下述 DS8/DS4 专用阈值，其他配对及 GI、PT+REBLUR 沿用 golden 原阈值，不自动放宽。场景资源、相机、检查区域及采样预算以外的算法配方仍需匹配，否则为 `incomparable`。完整采集指纹保留实际预算，因此恢复同一次运行时仍要求参数完全一致。

网页、JSON 和逐帧证据记录实际预算，报告同时展示 golden 与候选图的 SPP。不同预算的性能数据标明每帧/每样本成本，不将不同工作量的整图耗时混为一谈。纯 PT 标题随样本数显示为 PT512、PT2048 等；内部算法 ID `pt2048` 保留，以兼容现有基线。未配置时仍采用原配方 2048/192/192、预热 64；无需修改 `recipes/algorithms.json`。

## 范围和计时

固定 35 场景 × 三算法，共 105 个逻辑组合。下表为模板预算，可按上节配置覆盖。当前边界为 92 个可出图组合，Dragon 的 GI 和四个 hair 场景的 12 个组合有明确支持限制；每次仍需检查实际支持证据，未知失败不能算作已知限制。

| 算法 | 图像配方 | 耗时 |
| --- | --- | --- |
| GI | RTXPT GI + Falcor DI pairwise + Stable Planes + REBLUR；192 帧、无 RGB 累积 | 所示帧及预热 64 帧后的 mean/median |
| PT+REBLUR | 普通 PT + detail REBLUR；192 帧、无 RGB 累积 | 所示帧及预热 64 帧后的 mean/median |
| 纯 PT | 普通 PT、不去噪；golden 累积 2048 spp，回归累积 512 spp，每帧 1 spp | 全部样本总耗时、平均每样本成本、最后样本耗时 |

共同条件为原数值相机及 FOV、1920×1080、NEE 1/1、深度 5–16。Falcor DI 的 10 光源 + 1 BSDF 提议独立记录。完整配方在 `recipes/algorithms.json`，固定相机在 `scenes/cameras.json`。

六场景快速档为 kitchen、classroom、glass-of-water、water-caustic、cornell-box、material-testball，共 18 项，沿用当前入口的采样配置。原有介质/材质导入限制在 `scenes/suite.json` 中显式记录，不代表完整支持原场景全部物理特性。

计时来源为原始逐帧 CSV 与逐 pass GPU timestamp：renderer API 区间、GPU envelope、dispatch sum 分别列出，不混称。整体墙钟时间包括启动/准备等额外成本，另行记录。性能先报告变化和告警，图像判定使用基线保存的比较阈值。

## 数据和恢复

- 原始 ZIP、解压原件、适配资源：`.workspace/downloads/mitsuba/`。
- 正式 golden：`golden/<场景名>/`，仅正式 PNG，通过 Git LFS 上传及还原。
- 新生成、待发布的 golden 仍输出到 `.workspace/experiments/golden/<baseline-id>/`，正式版本校验完成后再整理到 `golden/`。
- 回归：`.workspace/experiments/regression/<run-id>/`。
- Git 内的 `baselines/` 保存默认版本、图片哈希清单、算法配置、耗时摘要和阈值，元数据不放入图片目录。
- 原始采集记录和旧 ZIP 仅在本机 `.workspace/` 保留。

当前正式目录为 [`golden/`](golden/)，共 31 个有正式图片的场景子目录、92 张图：GI 30 张、PT+REBLUR 31 张、纯 PT 31 张。35 个测试场景中四个 hair 场景不支持出图，Dragon 不支持 GI，限制仍记录在套件和基线清单中。图片字节及既有比较阈值保持原值。

`--offline` 只使用校验通过的本地基线或缓存归档，禁止 golden 和场景联网下载。`--scene`、`--algorithm` 可重复传入筛选所选套件；子集 golden 只覆盖声明的范围，不能用来假装完成 full 回归。场景下载入口筛选为 `--scenes kitchen cornell-box`。

中断后用同一 `--baseline-id` 或 `--output` 加 `--resume`。仅复用版本、输入合同和所有证据哈希都一致的结果。未完成的尝试保留为 `repeat-N.interrupted-<timestamp>` 后重试；若上次引擎 PID 尚未确认关闭，先检查该进程及日志。完整有效 golden 在恢复时只核验，原图、清单和 ZIP 都保持原字节。

普通失败不会自动覆盖或更新 golden。需要更新基线时，显式运行生成脚本并使用新的 ID；只有采集完整有效且比较阈值齐备的结果才更新本机“最近成功 golden”索引。正式发布只整理正式图片到 `golden/<场景名>/`，同时提交 `baselines/` 中对应的清单和阈值，并更新默认索引。

需要随仓库保存的配置为两份 `config.*.example.json` 模板、`recipes/algorithms.json`、`recipes/pt512-regression.json`、`scenes/catalog.lock.json`、`scenes/suite.json`、`scenes/cameras.json` 以及 `baselines/*.json`。两份 `config.*.local.json` 包含本机引擎和 DLL 路径，继续由 Git 忽略，不上传；golden 与回归的 SPP 仍在各自本机文件中独立配置。

## 通过条件与测试

**PT512 对 PT2048**：将归一化显示 RGB 按不重叠 8×8、4×4 块取 float64 均值，分别在 DS8、DS4 上比较整图及所有固定 ROI。每个场景、每个尺度、每个 ROI 的限值为已验证 PT512 的实测误差底线加固定余量：

| 检查 | 允许范围 |
| --- | --- |
| MAE | 实测底线 + 0.0005 |
| RMSE | 实测底线 + 0.001 |
| SSIM loss | 实测底线 + 0.005，即 SSIM 最多下降 0.005 |
| 平均 RGB 每个通道 | 相对已验证 PT512 的绝对变化 ≤ 0.0015 |
| 平均亮度 | 相对已验证 PT512 的绝对变化 ≤ max(0.0005, 该区域实测平均亮度 × 1.5%) |

所有检查必须同时通过。原分辨率指标仅作诊断，DS 指标计算前不量化为 8 位。少量焦散区域本身噪声较大，因此不用一个宽松的全局限值覆盖所有场景。亮度/颜色检查防止曝光变化恰好减小相对 2048 图的误差而被漏放。数值底线在 `recipes/pt512-regression.json`，不新增低样本 golden 图片；它绑定每张 2048 参考图的 SHA256、输入指纹及 ROI。参考图变化或缺失配置时在启动 GPU 前拒绝，需重新验证对应底线，不能自动接受新的候选误差。

当前 31 个支持 PT 的场景各有一次实际 512 spp 采集，全部通过；186 个黑图、±5% 曝光变化和局部错色样例全部拒绝。这是当前数据的验收结果，单次采集不能估计跨随机种子、驱动或硬件的噪声分布，也不能保证任意错误都能检出。四个 hair 场景仍验证已知不支持证据。此规则不豁免引擎崩溃、缺项或其他算法失败。

同场景、同算法对自己的 golden 比较；PNG 指标属于显示域，PT2048 也不保证完全收敛。新 golden 使用固定阈值：MAE 0.003、RMSE 0.006、SSIM loss 0.015、相对亮度偏差 0.015，整图和固定区域采用相同限值，与当前正式基线的限值一致。单次采集不估计噪声波动，不自动放宽阈值；旧 golden 保留自身阈值。检查区域、阈值或数据缺失时不放行。已有配对 PFM 时附加线性指标。

所选覆盖完整，且仅含 `pass` 或精确匹配的 `expected-unsupported`，回归返回 0。图像失败、未知执行失败、不可比、阈值不完整、支持变化及缺项返回非零。固定阈值与历史校准阈值均可用于回归；缺失或无效的阈值不能放行。

```powershell
python -m pytest tests
```

CPU 自动测试不会启动引擎/GPU，也不会下载公网大场景。GPU 由显式 golden/回归命令串行运行，使用只读驱动查询确认原生温控，连续满负载执行；不加 FPS 上限、帧间 sleep 或温度反馈限速。

场景来自固定的 Bitterli Mitsuba 资源清单。原始 URL、归档 SHA256、来源和各场景许可链接在 `scenes/catalog.lock.json`，归档中的作者和许可文件随原始/准备资源保留。

完整工作流和故障定位见 [docs/workflow.md](docs/workflow.md)，本机实际全量运行、失败恢复及验证记录见 [docs/validation.md](docs/validation.md)。
