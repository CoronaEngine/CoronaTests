# 本机验收记录

## 2026-11-17：移除首页分页

按用户追加要求移除每页 10 场景限制、上一页/下一页按钮和页码状态。首页一次显示全部匹配场景，保留搜索、算法/状态筛选、异常优先和独立场景详情页。仓库图库初始显示全部 35 场景；原回归仍默认显示 9 个异常场景，选择全部状态即可一次显示 35 场景。

重新生成两套页面后，全部 1,253 / 4,047 个本地链接有效。102 份正式图片/配置/阈值文件和 1,619 份回归原始工件哈希不变。自动测试覆盖超过 10 个匹配场景全部可见、无分页控件、搜索与状态筛选、差分/ROI/算法切换，完整 CPU 测试为 266 passed、4 subtests passed。证据在 `.workspace/tmp/report-no-pagination-20261117/`。

## 2026-11-17：汇总首页与场景详情页

按用户确认的方案，将仓库 golden 图库及后续回归报告改为汇总首页 + 每场景独立详情页。首页显示完整统计、采样参数、各算法状态/耗时，支持搜索、状态/算法筛选和每页 10 场景；有异常时默认只显示异常。详情页包含算法标签、场景导航、golden/候选图、按需打开的差分、单个 ROI、指标与限值，以及折叠的配置和证据链接。CSS/JavaScript 均随页面本地保存，不使用 fetch、CDN 或服务器。

对用户完成的 `regression-20261117T212524035620` 只重新生成网页，首页从 19,562,627 字节减至约 33 KB。原有 83 项通过、7 项图像异常、2 项执行失败、13 项已知不支持均保留，首页默认展示 9 个异常场景。该报告仍为 GI/去噪 192 帧、纯 PT512、预热 64 帧；没有重新运行引擎或修改判定规则。

仓库图库与该回归各生成 35 个场景页，分别核对全部 1,253 / 4,047 个本地链接。分页直接引用既有图片，不新建 PNG。正式 92 张 golden、两份本机配置、原采样配方与阈值等 102 份文件哈希不变；回归的 1,619 份原始 JSON/CSV/PNG/日志哈希不变。旧报告补充显示阈值时验证完整 golden 版本指纹、输入来源及报告内参考图 SHA256；只影响展示，不改写原始 JSON。封存 golden 包拒绝原地重写。

新增测试覆盖异常优先、全通过默认显示全部、分页/搜索/组合筛选、算法与 ROI 切换、差分开关、离线存储不可用、子页相对路径、子集算法、重复记录拒绝、未知状态、保留整体失败、已知不支持的提示样式，以及旧报告重生成不改写证据。完整 CPU 测试为 266 passed、4 subtests passed；JavaScript 逻辑及事件检查无需浏览器或网络。此处验证页面结构、全部本地资源和控件行为，不声称完成浏览器截图验收。

证据位于 `.workspace/tmp/report-pages-20261117/`。提交采用普通新增 commit，保留此前提交记录；本机配置与实验输出不进入 Git。

## 2026-11-17：PT 回归设为 512 spp，采用 DS8/DS4 阈值

回归本机配置及可提交模板的纯 PT 改为 512 spp；golden 保持 2048 spp，GI、PT+REBLUR 仍为 192 帧，计时预热均为 64 帧。本机参数文件仍忽略，不提交机器路径。

复用已经完成的 Kitchen 512 spp 图片，另外对其余 34 个场景各执行一次 PT512：30 个支持场景正常采集、4 个 hair 场景取得预期不支持证据，没有新增执行失败。这样得到全部 31 个支持场景的单次 PT512 图像，均对照原有 PT2048 golden；正式图片仍仅有原 92 张，未替换或新增低样本 golden。

少量焦散 ROI 的采样误差明显较高，采用每场景、每尺度、每 ROI 的实测误差底线，固定额外余量为 MAE 0.0005、RMSE 0.001、SSIM loss 0.005；平均颜色每通道变化最多 0.0015，平均亮度变化最多 max(0.0005, 实测亮度 × 1.5%)。DS8/DS4 均使用 float64 面积均值，所有整图/ROI 检查同时通过才放行。数值底线与参考图 SHA256、输入指纹和区域绑定，保存在 `recipes/pt512-regression.json`。每个场景只有一次正常采集，结果属于当前样本验收，不是跨随机种子或跨硬件的统计保证。

使用生产比较函数离线计算：31 个正常样本全部通过；每场景的黑图、±5% 曝光、三个 ROI 各自的局部错色（约占该 ROI 面积 0.64%）共 186 项，全部为 image-failed。原有采集报告和原始失败标签保留，另生成明确标记为离线重新判定的 PT 专项报告，不将它称为重新运行完整 105 项回归。该网页直接引用既有采集图片，没有复制或重新渲染图片；869 个本地链接均有效。

脚本集成测试覆盖正常噪声由 DS 判定、缺失/过期/区域不符策略在准备场景前拒绝、其他 SPP 保留原阈值，以及执行失败不能被新策略掩盖。原始全量回归中 GI 的 7 项图像失败，以及 Bathroom2 PT+REBLUR、Veach Bidir GI、Dragon GI 的执行失败仍保留，未放宽它们的规则。因此 PT 专项通过不等于当前引擎全量三算法通过。

证据位于 `.workspace/tmp/pt512-policy-20261117/`；实际采集为 `.workspace/experiments/regression/pt512-collection-20261117T124920Z/`，复用 Kitchen 来自 `pt512-vs2048-20261117T111055Z/`。专项结果为 `.workspace/experiments/pt512-policy-20261117.html`。

完整 CPU 测试为 255 passed、4 subtests passed，耗时 20.99 秒。额外核对全部 31 张候选的原始 CSV，integrator frame 均严格为 1..512，图像 SHA256 匹配，所属引擎均确认关闭。92 张正式图片、原基线清单/阈值、根目录图库和 golden 本机配置哈希不变；回归本机配置仅 `sampling.pt_spp` 从 2048 变为 512。

以下为之前各阶段的历史记录，其预算、发布形式和策略以对应阶段为准。

2026-11-16，在独立目录 `D:/work/corona/CoronaTests` 验证下载、生成 golden 和回归三个普通 Python 入口。运行不依赖 AI agent、Codex、CoronaEngine 源码中的 Python 模块或昨日临时执行器。引擎作为外部运行包启动。

本次渲染的代码与配方版本为 `ba54aecf41ba7d0d6c20cdc350644585bea4a1c5`。最终的默认全量参数改动不改变显式 `--suite full` / `--suite quick` 的采集配方；对应入口选择测试单独验证。

## 资源与环境

- 从上游独立下载 35 份归档，共 697,262,196 字节；校验 SHA256、ZIP CRC、解压及适配后的 3,997 个文件。没有复制旧实验的准备资源。
- 本机使用 NVIDIA GeForce RTX 3070 Ti、驱动 610.88；开始前只读查询确认原生温控和热降频。保留驱动温度、风扇、功耗及保护策略，连续批处理，无额外帧间限速。
- 所有场景采用固定数值相机、1920×1080 及完整配方。GI / PT+REBLUR 各 192 帧、前 64 帧预热；PT2048 为真实连续 2,048 个 1spp 样本。
- PT+REBLUR 明确使用已认可的 detail REBLUR：diffuse prepass 0、max radius 10。92 份历史成功采集的同算法 REBLUR 设置与配方逐项一致。

## 实际运行

| 运行 | 范围 | 结果 |
| --- | --- | --- |
| `full-20261116-v2` golden | 35 场景 × 3 算法 | 92 个图像组合有效且校准完成；13 个已知不支持组合 |
| `full-regression-20261116-v2` | 相同 105 项 | 92 pass；13 expected-unsupported；退出码 0 |
| `quick-regression-20261116-v2` | 显式 `--suite quick`，18 项 | 18 pass；退出码 0 |

本次全量回归中，92 项候选与各自 golden 的显示域 MAE 和 RMSE 均为 0，硬件/驱动身份一致。逐项耗时与变化比例仍独立记录，不将本次图像相同当作未来必须逐字节相同的判定规则。

另外，对昨天权威 selection-map 中的 92 个可比较图像进行了完整历史复核，新生成图像与历史图像的 MAE / RMSE 全部为 0。复核脚本仅用于验收，不参与正式执行链。

Golden 对每个可出图组合执行三次采集，共 276 次成功图像采集和 13 次支持检查。Dragon GI 的 directional-only 光源不满足当前 Falcor DI 有功率光源要求；四个 hair 场景的 12 个组合不支持几何导入。这些项使用实际诊断确认，没有生成占位图片，也没有从套件中删除。

对应离线网页与原始逐项耗时：

- `.workspace/experiments/golden/full-20261116-v2/index.html`
- `.workspace/experiments/golden/full-20261116-v2/timings.csv`
- `.workspace/experiments/regression/full-regression-20261116-v2/index.html`
- `.workspace/experiments/regression/full-regression-20261116-v2/timings.csv`

Golden ZIP 为 `.workspace/experiments/golden/full-20261116-v2.zip`。Git 保存 `baselines/full-20261116-v2.json` 和对应阈值文件，大图、场景和运行日志保留在本机 `.workspace/`。

## 失败恢复与边界

初次 golden 的 Rover GI 遇到原生 C++ 异常及 Vulkan device lost，同时存在 Windows nvlddmkm 153 事件。全量回归首次 bedroom / PT+REBLUR 完成采集后在退出时触发原生访问异常，未满足正常退出合同。这两次均先记录失败并返回非零，没有套用不支持豁免，也没有发布失败 golden。

保持引擎、资源、相机和配方不变，显式 `--resume` 只重试失败项；Rover GI 三次采集和 bedroom / PT+REBLUR 重试均正常结束。其余结果经输入合同与证据哈希验证后复用。首次失败的 `repeat-N.failed-<timestamp>`、原始日志及首次汇总仍保留。此次工作未修改或宣称修复引擎中的这两处原生异常。

性能按原始逐帧 CSV 和逐 pass GPU timestamp 记录。图像比较使用同算法 golden、校准阈值和固定区域；性能变化仅告警，不作为图像失败的豁免。PNG 指标属于显示域；PT2048 不代表完全收敛或全部物理特性支持。

## CPU 与网页检查

最终默认全量参数的测试先确认旧默认仅选择 6 场景时失败，改动后通过。完整 CPU 测试命令为 `python -m pytest tests -q`，结果 102 passed、4 subtests passed，耗时 75.63 秒；不会启动真实引擎或下载公网大场景。三个 `.cmd` 的 `--help` 也均可直接运行。

此前独立新克隆的第一条测试命令通过，并核对了所有 13 份原始 CSV / 日志 fixture 在 Git、原目录和克隆中的字节哈希，确认无需预先建立临时目录、也未破坏原始行尾证据。

使用 Edge headless 对最终 golden 和全量回归网页生成截图并检查：35 场景导航、三算法列、原图、耗时、基线/候选/差分、局部区域入口和证据链接均正常显示。截图位于 `.workspace/tmp/implementation/browser-final/`。

最终只读审计通过，证据为 `.workspace/tmp/implementation/acceptance-audit-v2-final.json`。审计逐次重算全部正式采集的帧数、统计耗时、GPU envelope 和 dispatch sum，核验原始 PNG / CSV / JSON 哈希、差分与区域裁剪，并检查三个网页的本地链接和锚点。Golden ZIP 的 4,502 个成员与完整产物目录逐项一致。

在开始全量回归前已记录 golden、索引和 ZIP 共 4,505 份文件哈希；全量及快速回归结束后全部保持不变。两次保留的失败尝试单独核验，不计入成功采集数。第一次临时审计误将 PT2048 的统计预热预期设为 0；依据历史原始记录校正为 64 后重新通过，原始失败审计也保留，正式采集和配方没有因此修改。

## 自定义采样入口验证

后续增加共享 `sampling` 配置及 `--pt-spp` / `--gi-frames` / `--pt-reblur-frames` / `--warmup-frames` 覆盖后，完整 CPU 测试为 121 passed、4 subtests passed，耗时 80.01 秒。新增测试先观察到配置被忽略、CLI 不识别、报告固定 PT2048 的失败，再验证实际配方覆盖、参数优先级、无效数值、预热约束、不同预算不可比和恢复拒绝。未覆盖采样参数时，全部 105 项输入指纹与现有全量 golden 一致。

通过实际 `.cmd` 入口对 kitchen 执行三模式短采样：纯 PT 累计 8 spp，GI / PT+REBLUR 各 8 帧，计时预热 0。`sampling-kitchen-20261116` 完成 9 次真实 golden 采集且校准通过，随后 `sampling-kitchen-regression-20261116` 的 3 次真实回归全部通过，显示域 MAE 均为 0。两阶段分别耗时 160.42 秒和 54.35 秒；这是参数链验证，未将单场景结果作为全量验收。

只读审计逐次核对 12 次采集的实际 integrator frame 1..8、公开截图 API 的 render_frames、原始 CSV 总耗时与每样本成本、GPU 累积 pass 数、正常退出及证据哈希。纯 PT 每帧都有一次 RGBFilm 累积 pass，另外两种模式没有该 pass。新网页截图确认标题为 PT8，正文记录 8-sample total 和实际帧数/预热；没有残留写死的 2048 样本说明。

验证后，原全量 golden、索引和 ZIP 的 4,505 份哈希仍与回归前快照完全一致。默认 golden 指针已按原字节恢复到 `full-20261116-v2`，验证专用基线索引移至 `.workspace/archive/configurable-sampling-pilot-indices/`，原始采集和报告保留。证据为 `.workspace/tmp/configurable-sampling/legacy-inputs.json`、`launcher-verification.json`、`pilot-verification.json` 和 `golden-preview.png`。

## 无参数双击入口验证

生成 golden、普通回归和全量回归的无参数 `.cmd` 入口共用 `scripts/desktop_run.cmd`，明确读取仓库中的 `config.local.json`，自动生成运行 ID / 输出目录，并在结束时打开已有报告、保留结果窗口。带参数的两个原始入口继续支持无暂停的自动化调用。

执行 6 个真实 Windows CMD 场景，在启动 Python 渲染器的位置使用参数记录器，保留实际配置解析和 35 场景 / 三算法选择。验证无参数的三个入口均读取文件配置的 512 / 128 / 96 / 16 预算、自动输出目录和最近成功 golden；检查通过和失败退出码、保留窗口、路径含空格、Python 缺失，以及原命令行调用的兼容性。全部通过，未启动 GPU 重跑；证据为 `.workspace/tmp/config-only-launchers/verification.json`。本次只调整启动包装和文档，Python 采样与渲染代码未改动。

## 2026-11-17：独立配置与不同预算比较

当前版本将默认配置拆为 `config.golden.local.json` 和 `config.regression.local.json`。本机分别复制保留原来的参数值；旧共享文件归档到 `.workspace/archive/shared-config-20261117/config.local.json`，默认入口不再读取它。三个无参数 Windows CMD 入口的真实启动测试确认各自读取正确文件，golden 2048 / 回归 512 的文件配置互不影响，并保持 35 × 3 项覆盖和结果窗口保留。

按用户确认的新规则，帧数、计时预热及纯 PT 样本标题允许不同，场景/相机/算法配置等其他输入仍需严格匹配。比较继续绑定 golden 原阈值和原始输入指纹，完整候选指纹仍包含实际预算以保护恢复执行。完整测试为 136 passed、4 subtests passed，耗时 81.41 秒；覆盖跨预算同图通过、变黑仍拒绝、非预算输入变化拒绝、配置隔离和双击路由。此规则替代前一版本“不同预算直接不可比”的行为。

读取现有全量基线验证：默认 105 项输入指纹全部不变；回归 PT 改为 512 spp 后，35 项纯 PT 的完整输入指纹变化，105 项去除允许预算差异后的输入仍全部匹配。证据为 `.workspace/tmp/independent-configs/input-compatibility.json`。

真实验证从独立的临时回归参数文件读取 8 spp、预热 0，对 kitchen 既有 2048 spp / 预热 64 的 golden 执行比较。引擎正常退出，原始 CSV 为 integrator frame 1..8，每帧恰好一次 RGBFilm 累积。候选与此前已验证的 8 spp 图像逐字节相同。此次 MAE 0.171488、RMSE 0.241777 超过原阈值，因此实际回归结果为 `image-failed`、退出码 1，完成了比较而非判为不可比；未自动放宽阈值。运行耗时 23.91 秒，只验证单场景跨预算路径，不代表新的全量回归通过。

报告截图确认同时显示 golden 2048 spp、候选 8 spp、原阈值政策和不同预算的计时提示。本机两份配置文件在验证期间均未改写，现有 golden / 索引 / ZIP 的 4,505 份哈希全部不变。证据为 `.workspace/tmp/independent-configs/actual-verification.json`、`comparison-preview.png`，真实报告位于 `.workspace/experiments/regression/independent-configs-kitchen-20261117/index.html`。

## 2026-11-17：完整正式 golden 远端发布和干净克隆下载

先发布既有 `full-20261116-v2`，没有混入外部实验图库的 Bedroom 更新。核验原 golden 的 105 个逻辑任务、92 项校准、276 次成功图像采集及 13 项实际不支持证据；4,501 个封存工件与 `golden.json` 共 4,502 个文件逐一匹配原 ZIP。归档原始大小 2,079,949,916 字节、SHA256 为 `1310bd96108878aa7461690d435c8feb76f42b67a4301cae8b10ce8f37e84317`，没有重打包或改写采集记录。

完整数据按两个原始字节分段上传到私有仓库 Release `golden-full-20261116-v2`，Git 中的索引固定附件 URL/API ID、字节数和 SHA256。使用现有 Git HTTPS 凭据，不要求把令牌写入配置。下载器在跨主机重定向时移除授权头，安装前检查分段、重组归档、安全解包、校准清单、阈值来源及全部工件；校验失败不覆盖已安装版本。

完整 CPU 测试为 207 passed、4 subtests passed，耗时 86.87 秒。覆盖认证/重定向、HTTP 200/206、中断恢复、离线 `.part` 复用、损坏包、ZIP 路径/NUL/大小写别名、原子安装、默认版本、显式路径、采样预算兼容和原有 CMD 入口。独立代码审查未发现阻塞问题。

从 GitHub 新克隆到不同目录，开始时没有 `.workspace`、golden 或下载缓存；加入待提交的消费端代码和固定清单后，实际通过生产下载器从私有 Release API 下载全部数据。重组 ZIP 的 SHA256 与原归档一致，安装后的 4,502 个文件逐字节匹配封存源目录，1,877 个离线 HTML 链接均可解析。此后使用该克隆准备 Kitchen 场景，实际运行 PT2048 回归，结果为 pass，退出码 0，引擎已正常关闭。该步骤验证一个真实回归任务，不代表重新采集全部 105 项，也不是第二台物理 GPU 的实机验收。

证据在 `.workspace/experiments/golden-remote-20261117/` 的 `local-verification.json`、`remote-import-verification.json`、`publication-verification.json`、`clean-regression-verification.json`；完整测试日志为 `.workspace/tmp/golden-remote-20261117/full-tests.log`。新机器仍需受支持的运行环境和自己的完整引擎运行包；历史绝对路径只作为来源保留。

## 2026-11-17：正式图片精简

按用户要求，以上完整采集包不再作为当前远端发布内容。正式目录直接改为 `golden/<场景名>/`，共 31 个有图场景子目录、92 张 PNG：GI 30 张、PT+REBLUR 31 张、纯 PT 31 张。每张选用原有 `repeat-0/image.png`，逐一核对 SHA256，未重新渲染或调整图片。重复采集、setup、裁剪、网页、日志、逐帧 CSV 和运行环境记录不进入正式图片目录；完整原始记录与 ZIP 在本机 `.workspace/` 保留。

图片映射、原有采样配方、计时摘要和 13 项不支持状态整理到 `baselines/full-20261116-v2.manifest.json`；比较阈值和区域保持原值，阈值文件移除噪声采集及受控退化的详细记录。清单不包含本机引擎路径、DLL 目录或环境变量。`config.golden.local.json`、`config.regression.local.json` 的文件哈希均未变化，继续被 Git 忽略。

完整 CPU 测试为 229 passed、4 subtests passed，耗时 87.93 秒。新增测试覆盖按场景发布、仅保存原图、阈值不变、JSON 换行兼容、图像损坏拒绝及 Git LFS 指针还原；生产下载入口以 `--offline` 成功读取新的正式目录。此次没有启动 GPU 渲染。准备与核对记录在 `.workspace/tmp/golden-92/`。

## 2026-11-17：生成 golden 改为单次采集

生成流程移除两次重复采集和经验噪声校准。每个场景、算法组合只调用一次采集；正式计时与报告仍来自这一次采集。新基线使用固定阈值，整图及检查区域分别为 MAE 0.003、RMSE 0.006、SSIM loss 0.015、相对亮度偏差 0.015，与既有 92 项正式基线的限值逐项一致。新清单明确记录固定阈值已准备好、未进行经验校准；旧基线继续按其保存的阈值读取。

完整 CPU 测试为 236 passed、4 subtests passed，耗时 17.70 秒。全量调度测试使用模拟采集边界，确认 35 × 3 项各调用一次，其中 92 项出图、13 项为预期不支持；最终目录和 ZIP 均恰好 92 张 PNG，没有 repeat-1 / repeat-2，完整 resume 不再采集。还验证了固定阈值的同图通过、退化图失败、缺失区域拒绝、独立 SPP 及旧基线兼容。本次仅验证脚本流程，没有重新执行 GPU 全量渲染。

现有正式图片、清单、误差阈值及两份本机参数文件均未改动。证据为 `.workspace/tmp/golden-single-capture/` 中的 `red.log`、`full-final.log`、`data-preservation.json`。

## 2026-11-17：仓库内静态 Golden 图库

根目录新增 `index.html`，使用仓库内的相对路径直接引用 `golden/<场景名>/*.png`。页面包含 35 个场景、105 个算法状态卡片和全部 92 张正式图，保留耗时及 SVG 局部放大；没有复制图片、保存裁剪图或引用本机归档路径。样式、展示数据已写入 HTML，不需要服务器或 JavaScript；clone 并拉取 LFS 图片后可直接打开。需要刷新图库时运行 `python scripts/generate_gallery.py`，只重写 HTML。

本机静态校验通过全部 589 个链接和 92 张 PNG，确认图片目录仍仅有原 92 张图。完整 CPU 测试为 237 passed、4 subtests passed，耗时 17.94 秒，包含纯 HTML 渲染不写入其他文件的验证。浏览器工具因本地文件协议限制未能预览页面；链接和图片完整性通过本地文件解析验证。证据在 `.workspace/tmp/golden-gallery/`。

## 2026-11-17：重新生成 Bedroom Golden

使用本机 golden 配置和当前引擎，对 Bedroom 三种算法各正式采集一次：GI 192 帧、PT+REBLUR 192 帧、纯 PT 2048 spp，计时预热 64 帧。只读驱动查询确认 RTX 3070 Ti / 610.88 的 95°C 降频和 98°C 关闭保护，按连续批处理执行。三项实际采集全部成功、退出码 0，所属引擎均已关闭。

GI 稳定均值 57.211 ms，PT+REBLUR 稳定均值 23.522 ms；纯 PT 全部 2048 个样本的 renderer 区间总耗时 28,576.190 ms。GI 原图 SHA256 更新为 `cc9823e1eaa65e7c4ff2f89728c84dbdf168f8dda7c57b6fc27da6acaa40d734`，与旧图 MAE 0.00365905 / RMSE 0.02634177；PT+REBLUR、纯 PT 的新 PNG 与各自旧图逐字节一致，三项计时均刷新。此为显式更新 golden，不将新旧差异称为回归通过。

保留其他 89 张图、其余场景记录和阈值原值；本机配置文件未改。Bedroom 的新采集引擎指纹单独记录在对应条目中，阈值来源按条目指纹验证，其余条目继续绑定原引擎。正式目录仍有 92 张 PNG，根目录图库已同步，全部 589 个本地链接有效。完整 CPU 测试为 239 passed、4 subtests passed，耗时 17.67 秒；新增测试验证局部更新的引擎来源绑定。原图、原清单及新的三项采集证据保存在忽略的 `.workspace/`，核对记录在 `.workspace/tmp/bedroom-golden-refresh-20261117/`。
