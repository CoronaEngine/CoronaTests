# Golden 在线图库

基线 `full-20261116-v2`：**93 张正式图片**，35 个场景。

直接在 GitHub 点击场景名查看三种算法的效果图、采样参数和耗时；点击图片可查看原图。

图片引用仓库内的 `golden/` 原图。访问者需要仓库读取权限。GI/去噪使用时域帧数，纯 PT 使用累计 SPP。

GI/去噪列显示预热后的平均帧耗时；PT 列显示全部样本的累计渲染耗时。

| 场景 | GI | PT+REBLUR | PT |
| --- | --- | --- | --- |
| [Contemporary Bathroom](docs/golden/bathroom.md) | 平均 52.25 ms/帧 | 平均 20.60 ms/帧 | 累计 28.80 s |
| [The Grey &amp; White Room](docs/golden/living-room.md) | 平均 60.26 ms/帧 | 平均 19.62 ms/帧 | 累计 27.26 s |
| [Bedroom](docs/golden/bedroom.md) | 平均 57.21 ms/帧 | 平均 23.52 ms/帧 | 累计 28.58 s |
| [Country Kitchen](docs/golden/kitchen.md) | 平均 49.58 ms/帧 | 平均 20.51 ms/帧 | 累计 24.03 s |
| [Modern Hall](docs/golden/staircase2.md) | 平均 48.74 ms/帧 | 平均 19.15 ms/帧 | 累计 27.31 s |
| [The Wooden Staircase](docs/golden/staircase.md) | 平均 46.13 ms/帧 | 平均 17.17 ms/帧 | 累计 22.15 s |
| [Salle de bain](docs/golden/bathroom2.md) | 平均 48.17 ms/帧 | 平均 18.40 ms/帧 | 累计 24.21 s |
| [The White Room](docs/golden/living-room-2.md) | 平均 52.95 ms/帧 | 平均 22.39 ms/帧 | 累计 26.17 s |
| [The Modern Living Room](docs/golden/living-room-3.md) | 平均 59.39 ms/帧 | 平均 22.86 ms/帧 | 累计 32.27 s |
| [The Breakfast Room](docs/golden/dining-room.md) | 平均 48.20 ms/帧 | 平均 14.53 ms/帧 | 累计 19.40 s |
| [Glass of Water](docs/golden/glass-of-water.md) | 平均 46.11 ms/帧 | 平均 14.11 ms/帧 | 累计 16.89 s |
| [Pontiac GTO 67](docs/golden/car2.md) | 平均 55.94 ms/帧 | 平均 12.18 ms/帧 | 累计 12.06 s |
| [Old vintage car](docs/golden/car.md) | 平均 45.45 ms/帧 | 平均 12.97 ms/帧 | 累计 12.73 s |
| [Coffee Maker](docs/golden/coffee.md) | 平均 38.49 ms/帧 | 平均 11.33 ms/帧 | 累计 11.10 s |
| [Little Lamp](docs/golden/lamp.md) | 平均 41.04 ms/帧 | 平均 10.52 ms/帧 | 累计 9.94 s |
| [Hair Curls](docs/golden/hair-curl.md) | 已知不支持 | 已知不支持 | 已知不支持 |
| [Curly Hair](docs/golden/curly-hair.md) | 已知不支持 | 已知不支持 | 已知不支持 |
| [Straight Hair](docs/golden/straight-hair.md) | 已知不支持 | 已知不支持 | 已知不支持 |
| [Victorian Style House](docs/golden/house.md) | 平均 33.88 ms/帧 | 平均 9.71 ms/帧 | 累计 9.89 s |
| [4060.b Spaceship](docs/golden/spaceship.md) | 平均 41.12 ms/帧 | 平均 12.01 ms/帧 | 累计 12.15 s |
| [Japanese Classroom](docs/golden/classroom.md) | 平均 50.51 ms/帧 | 平均 17.14 ms/帧 | 累计 26.07 s |
| [Dragon](docs/golden/dragon.md) | 平均 20.90 ms/帧 | 平均 5.23 ms/帧 | 累计 4.89 s |
| [Utah Teapot (full)](docs/golden/teapot-full.md) | 平均 51.53 ms/帧 | 平均 15.18 ms/帧 | 累计 17.13 s |
| [Utah Teapot](docs/golden/teapot.md) | 平均 44.08 ms/帧 | 平均 9.52 ms/帧 | 累计 8.23 s |
| [Cornell Box](docs/golden/cornell-box.md) | 平均 27.37 ms/帧 | 平均 7.64 ms/帧 | 累计 6.68 s |
| [Volumetric Caustic](docs/golden/volumetric-caustic.md) | 平均 26.57 ms/帧 | 平均 8.02 ms/帧 | 累计 165.08 s |
| [Water Caustic](docs/golden/water-caustic.md) | 平均 34.28 ms/帧 | 平均 10.84 ms/帧 | 累计 12.28 s |
| [Veach, Ajar](docs/golden/veach-ajar.md) | 平均 45.18 ms/帧 | 平均 16.41 ms/帧 | 累计 20.30 s |
| [Veach, Bidir Room](docs/golden/veach-bidir.md) | 平均 29.12 ms/帧 | 平均 8.50 ms/帧 | 累计 8.73 s |
| [Veach, MIS](docs/golden/veach-mis.md) | 平均 40.46 ms/帧 | 平均 10.17 ms/帧 | 累计 8.40 s |
| [Material Test Ball](docs/golden/material-testball.md) | 平均 44.60 ms/帧 | 平均 10.18 ms/帧 | 累计 9.64 s |
| [Fur Ball](docs/golden/furball.md) | 已知不支持 | 已知不支持 | 已知不支持 |
| [Lego Digger](docs/golden/lego.md) | 平均 19.47 ms/帧 | 平均 4.84 ms/帧 | 累计 5.15 s |
| [Rover](docs/golden/rover.md) | 平均 38.53 ms/帧 | 平均 11.16 ms/帧 | 累计 11.12 s |
| [Mitsuba Material Ball](docs/golden/matpreview.md) | 平均 41.74 ms/帧 | 平均 10.08 ms/帧 | 累计 9.14 s |
