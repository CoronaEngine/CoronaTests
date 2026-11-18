# 运行流程与故障定位

各入口共享固定的场景清单、完整算法配方、采集验证和报告生成链。新机器只需 Python 依赖及明确的外部引擎运行包，不需要 CoronaEngine 源码目录中的 Python 工具或历史实验目录。

三个入口均默认覆盖全部 35 个场景；两个渲染入口各运行三种算法，共 105 项。六场景快速档仅在显式传入 `--suite quick` 时启用。执行器是本仓库的普通 Python 程序，不依赖 AI agent 或 Codex。

日常使用编辑对应配置文件并保存，然后直接双击入口。`generate_golden.cmd` 使用 `config.golden.local.json`，`run_full_regression.cmd` / `run_regression.cmd` 使用 `config.regression.local.json`。两份文件的 SPP、历史帧数、预热和引擎路径各自独立。启动时无需输入参数，运行 ID 和结果目录自动生成，结束后打开已有报告并保留结果窗口。

## 场景准备

`scenes/catalog.lock.json` 锁定 35 个场景的 URL、归档 SHA256、大小和 XML 选择。默认不抓取网页后自动改变套件；新增场景是显式修改清单和验证覆盖的动作。

下载使用 `.part` 支持 HTTP Range 恢复，完整文件仍需 SHA256 与 ZIP CRC 校验。解压限制在目标目录内，拒绝越界成员和符号链接。原始资源与派生准备结果分开保存，准备结果记录每个文件的 SHA256。

旧版 XML 的兼容规范化和 Mitsuba serialized 网格到 PLY 的转换在本仓库内完成；输入、适配版本和输出哈希参与准备清单。缺纹理、无效网格、错误归档或未完成解压不能标记 ready。hair 资源照常准备，其几何支持限制由实际引擎导入证据判定。

## 引擎运行包

`config.regression.local.json` 示例字段如下；生成 golden 使用独立的 `config.golden.local.json`，字段相同但无需 `golden` 选择：

```json
{
  "engine_executable": "D:/path/to/runtime/corona_engine.exe",
  "runtime_dirs": ["D:/path/to/additional-dlls"],
  "golden": null,
  "sampling": {
    "pt_spp": 2048,
    "gi_frames": 192,
    "pt_reblur_frames": 192,
    "warmup_frames": 64
  }
}
```

`runtime_dirs` 是明确的 DLL 搜索顺序，会计入候选运行指纹。命令行 `--engine-executable` 和可重复的 `--runtime-dir` 覆盖本机配置；不解析引擎仓库的 CTest 文件。`golden` 为空时按 `baselines/default.json` 选择仓库固定的发布版本，缺失时自动下载；也可通过 `--golden-id` 选择已发布 ID，或用优先级更高的 `--golden` / 本机配置路径指定已有完整基线。显式路径和显式 ID 不能同时指定。没有仓库默认清单时保留旧本机 latest 索引回退。

## 仓库内 golden 与备份恢复

正式基线直接按场景放在 `golden/<场景名>/`，仅有 `gi.png`、`pt-reblur.png`、`pt.png`，合计 93 张。图片由 Git LFS 管理；清单、耗时摘要、采样配方及阈值在 `baselines/`。完整克隆后可直接验证；若 PNG 仍为 LFS 指针，联网入口先核对其 OID 与图片清单，再执行限定于 `golden/**` 的 `git lfs pull`，完成后复核。`--offline` 不触发 Git 或网络拉取，缺少实际图片时明确报错。

当前正式索引不引用 Release、重复采集或原始诊断包。Git LFS 拉取使用仓库现有 Git 认证，支持 HTTPS 或 SSH，不保存到配置或清单。下载器保留旧归档格式的读取兼容，但默认基线只使用仓库内 93 张图片。

`baselines/default.json` 指定正式版本，`<id>.json` 指向图片目录和 `<id>.manifest.json`，后者记录原图 SHA256、任务支持状态、算法预算与计时摘要。`<id>.thresholds.json` 保存原有误差阈值及固定区域。读取时核对清单指纹、93 张原图、阈值来源和任务覆盖；图片缺失、损坏或多出文件均不放行。

新机器完成 Python 依赖和引擎运行包配置后，可直接运行 `run_full_regression.cmd`，由入口准备缺失的 golden 和固定场景。只浏览参考图时使用 `download_golden.cmd`，完成后打开所下载基线的 `index.html`。历史记录中的原机绝对路径保留为采集来源，新回归使用本机路径并生成独立运行目录，不复用原机的 resume 合同。

两份文件各自提供 `sampling`；`--pt-spp`、`--gi-frames`、`--pt-reblur-frames`、`--warmup-frames` 分别覆盖当前入口的配置值。各字段省略或设为 null 时保留配方默认值。纯 PT 每帧 1 spp，因此累计帧数就是总 SPP；另外两种模式的帧数用于时域历史。Python 和 `.cmd` 入口均按运行类型选择独立文件，不自动回退到共享文件；`--config` 仍可显式指定其他配置。

所有生效值在计算完整采集指纹之前确定。比较时仅允许帧数、计时预热及纯 PT 样本标题不同，继续核对其他场景/相机/算法输入；阈值来源仍绑定 golden 原始输入指纹。PT512 对 PT2048 使用 `recipes/pt512-regression.json` 的 DS8/DS4 规则，其他预算沿用 golden 阈值。恢复同一运行时仍检查完整指纹，不能复用其他预算的采集。

CEF 调试接口仅绑定本机，端口默认 9980。`--port` 可修改；已占用端口不会被当作可附着目标。`--timeout` 是单次截图/API 工作上限，默认 7200 秒。

每个采集从 exe 所在目录启动独立引擎，抑制交互视口出帧并通过公开截图 API 连续批处理。采集有一个独立 setup 帧，正式图像重新重置历史并验证精确的 integrator frame 1..N。前端 polling 不构成帧间节流。

采集前后读回数值相机、积分器、环境和 REBLUR 配置，核验 GI 私有上下文生命周期及所需 pass。配置读回不足时不能仅凭“截图存在”算成功。源码工具、运行包、资源和配方在同一次批次期间保持冻结。

## 证据与时序

GitHub 在线浏览从根目录 `GALLERY.md` 进入，按场景打开 `docs/golden/<scene>.md` 查看效果图和耗时。README 提供三算法预览；这些页面均直接引用 `golden/` 原图，遵循仓库读取权限，不需要部署站点。维护者更新正式基线后运行 `python scripts/generate_gallery.py`，会同步刷新本地 HTML、GitHub Markdown 和 README 预览，再一起提交。

报告的 `index.html` 只保留汇总表、完整结果统计、搜索/算法/状态筛选，全部匹配场景一次显示，不分页。有异常时默认筛选异常，已知不支持单独统计。`pages/<场景>.html` 按算法切换，默认打开异常算法，提供上一/下一场景导航；原图并排，差分和单个 ROI 按需打开。`assets/report.css`、`assets/report.js` 均为本地文件，不使用 fetch、CDN 或服务器。

后续采集将实际使用的原阈值一并记录到 `comparison.json` 的 `raw_thresholds`，只用于展示，不改变判定规则。旧回归可运行 `python scripts/regenerate_report.py <结果目录> --golden golden` 重新生成展示；它验证参考图 SHA256 和阈值输入来源，只修改 HTML/CSS/JavaScript，原始结果、计时与图片不变。封存 golden 的工件哈希包含旧页面，不允许直接改写；仓库根图库单独生成新页面。

采集中的每项 `runs/<scene>--<algorithm>/repeat-0/` 保存 `run.json`、`attempt.json`、原始 `image.png`、`frames.csv`、CPU/QPC stages、GPU timestamp/diagnostics、frame map、timing JSON 和 API/引擎日志。生成 golden 不创建 `repeat-1` 或 `repeat-2`；成功后整理成单份正式图片、网页及计时摘要，不保留 setup 图和独立裁剪 PNG。

`sample_ms = total_ms / frames`，涵盖所有正式样本，包括统计预热窗口；`shown_frame_ms` 是最后所示样本。稳定 mean/median 排除配置的预热帧，默认 64。纯 PT 总成本包含全部配置的累计样本，golden 模板 2048、回归模板 512，不能只用最后样本或稳定帧均值代替总成本。GPU envelope 含 dispatch 间隙，dispatch sum 不等于整帧延迟。

启动、资源加载、编译及文件输出成本由采集墙钟时间另行记录。每次保存只读 GPU/温控观测，不修改驱动温度、风扇、功耗或保护策略。

## Golden 与回归

生成 golden 时每个场景、每种算法只采集一次。按当前全量套件，105 项中有 93 项出图、12 项检查已知不支持状态。比较使用固定限值：MAE 0.003、RMSE 0.006、SSIM loss 0.015、相对亮度偏差 0.015，整图及固定区域相同；这些值与当前正式基线一致。阈值配置仍绑定 baseline ID、输入合同和采集构建指纹，不再进行重复噪声估计或追加渲染。

新 `golden.json` 使用 `threshold_policy: fixed`、`thresholds_ready: true`，并将 `calibrated` 记为 false，明确没有进行经验噪声校准；`thresholds.json` 保存完整的固定阈值及区域。采集完整有效且阈值齐备即可发布索引、更新最近成功指针并打包 ZIP。旧的已校准基线仍可读取，图片和阈值不变。对已封存 golden 使用 resume 仅验证完整性，不重新采集或改写基线及归档。

回归先验证 golden 的清单及全部证据哈希，然后检查所选任务与基线的资源、相机、采样预算以外的算法配方、区域和预期支持边界是否一致。PT512 对 PT2048 在准备场景及启动 GPU 前额外验证数值底线对应的参考图、输入指纹及区域；同一次运行冻结该策略文件，恢复时也检查它的哈希。策略不锁定候选引擎版本，允许正常测试修改后的引擎。

这一配对将显示 RGB 以 float64 分别做 DS8、DS4 面积均值，整图和所有 ROI 同时检查 MAE、RMSE、SSIM loss。限值为各自实测 512 spp 误差 + 0.0005 / 0.001 / 0.005；额外检查平均 RGB 通道变化 ≤ 0.0015、平均亮度变化 ≤ max(0.0005, 实测亮度 × 1.5%)。不生成新的低样本 golden 图片，也不自动更新底线。报告记录策略副本、每项实际值/限值及两个尺度的状态；原分辨率差分与指标仅用于诊断。其他预算和算法仍使用原阈值。

报告同时显示两边预算。缺项或不可比项不会用其他模式图片替代。候选运行单独保存，报告中的 golden 图复制到报告自身目录，差分和链接不会依赖旧磁盘位置。

报告按完整所选范围列出结果。Dragon GI 只有原 directional-only 光源与实际 Falcor 无功率/要求 active Falcor DI 的日志同时吻合，才算已知不支持；hair 必须有实际 unsupported hair 诊断且场景 actor 为空。新错误诊断、无关崩溃或空场景均不能自动套用旧豁免。

## 常见失败

- 下载/哈希失败：检查 `.part`、网络和固定清单；重新运行可恢复。上游替换同名资源时不得自动接受新字节。
- 启动失败：查看 `engine.log`、实际运行包及 `runtime_dirs`。确认 CEF 资源、DLL、GPU 驱动与公开 API 能力。
- 配置/相机或 pass 不符：检查 `api.jsonl`、`run.json` 和 `engine-native.log`，保持证据，不调低验证条件。
- 图像失败：先看基线/候选/差分和具体失败指标，确认是否为预期渲染改动。正式更新基线要显式生成新 ID。
- 恢复拒绝：比较 `run-config.json`、任务 `_contract` 和证据哈希；输入漂移时使用新输出目录。中断尝试自动移到同一任务下保留，未确认关闭的进程先由操作者检查。
- 性能变化：核对硬件、驱动、后台负载和时间定义。初版只告警，不用性能波动掩盖图像失败。

任何失败都保留已有结果，不自动覆盖 golden，也不关闭无关用户进程。

正式数据整理为 `golden/<场景名>/` 后，本机 latest 指针由已验证的仓库索引更新。旧的完整采集记录留在忽略的 `.workspace/`，不随新目录上传。清单不包含本机引擎路径、DLL 目录或环境变量；JSON 使用规范内容指纹，避免 Windows 与其他平台的换行差异影响校验。PNG 使用 LFS，原图 SHA256 不变。
