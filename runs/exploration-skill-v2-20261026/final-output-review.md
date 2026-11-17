# 探索子 agent 最终输出 · 完整阅读版

以下保留修订版两次成功试验的完整报告字段与正文, 仅调整阅读顺序和引用链接. 报告 A/B 的 ID 各自独立. 两次均通过结构与引用校验, 尚未生成或渲染 Shader.

## 参考图与分析目标

目标: 彩色堆叠区最左上方的绿色齿轮状圆片, 包含中心孔和外轮廓. 以下是原始参考图, 不是生成结果.

![原始参考图: 最左上方绿色圆片为目标](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/apps/shader_deep/test_pic/2d-physics-balls.png)

## 两份报告的主要差异

| 项目 | 报告 A | 报告 B |
| --- | --- | --- |
| 轮廓理解 | 约 8 个圆弧齿瓣 | 倾向约 6 个内凹缺口, 保留 6–8 的不确定性 |
| 默认建模 | 基圆与均布齿圆做并集 | 基圆减去等角布置的切割圆 |
| 中心孔比例 | 孔径约为外径 20%–24% | 孔径约为外径 25%–30% |
| 绿色估计 | #34A853 | #3AA853 |
| F / R / M / S | 5 / 4 / 5 / 1 | 4 / 4 / 4 / 1 |
| 未知项 / 缺口 | 5 / 2 | 4 / 3 |
| 草图局部备选 | 2 个 | 1 个 |

以上齿数、尺寸、比例和色值均为模型目测或假设, 不是测量确认值.

## 本次检查意见（助手补充, 不属于子 agent 原文）

1. 两份报告对凹槽/齿瓣数量与轮廓构造方式有实质分歧, 不能因两份均通过程序校验就认定其中任一方案正确.
2. 报告 B 的 M2 和 S1 将差集写成 `d_outer = max(d_base, d_notch)`. 按报告自身“形状内部为负”的定义, 这表示求交; 去掉切割圆应使用 `max(d_base, -d_notch)`. 下方原文保留这个错误, 未替它改写.
3. 例如采用 B 的参数: base_radius=51, notch_radius=7.65, 切割圆沿基圆以 6 等分布置. 在 p=(20,0) 处, d_base=-31、d_notch=23.35, 原式给出 +23.35, 会排除本应位于圆片环带内的该点; 加负号后为 -23.35. 这是代数检查, 并非渲染验证.
4. 两份报告都将位置、比例和色值留为待校准项. 下一步若进入生成, 应先核实这些参数和公式.

---

## 报告 A · 完整输出

[原始 JSON](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/exploration-skill-v2-20261026/01-staged/accepted-report.json) · [原始输入与试验记录](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/exploration-skill-v2-20261026/01-staged/trial.json)

模型: `z-ai/glm-5.3-flash`; 调用: 12 次; 修复: 2 次; 耗时: 349.909 秒.

<a id="report-a-e1"></a>
### 固定目标 E1（输入材料）

名称: 最左上方绿色齿轮状圆片

范围: 原图左侧彩色堆叠区域中最左上方的绿色圆片, 包含中心孔和外轮廓

探索方向: 围绕目标的可见特征和相互关系, 提出可用于过程式 Shader 的实现机制和草图。

### 特征 F

<a id="report-a-f1"></a>
#### F1

外轮廓: 位于彩色堆叠区左上角的近圆形齿轮状盘体, 几何中心目测约在原图 (261,405)、外径约 105–110px(均为目测估计); 轮廓由约 8 个圆弧状齿瓣沿圆周近似均匀组成(齿数为目测估计), 齿间为窄而圆滑的凹谷, 齿顶半径与谷底半径之差约为平均半径的 15%–25%(目测估计, 即谷底半径约为齿顶半径的 80% 左右)。

候选机制:

- [M2](#report-a-m2): 目测齿顶为明显圆弧状凸起、凹谷窄而圆, 基圆+齿圆并集最贴近该齿形
- [M3](#report-a-m3): 若凹谷被证实为窄而尖的切口, 可用锐谷剖面替代
- [M1](#report-a-m1): 若边缘实为均匀正弦波纹(谷较宽), 余弦调制是最简实现

<a id="report-a-f2"></a>
#### F2

中心孔: 与盘体同心的圆形孔, 呈背景白色, 孔径约为外轮廓直径的 0.20–0.24(目测估计, 约 21–25px; 相对谷底基圆半径约 0.25); 孔缘锐利平滑, 无描边。

候选机制:

- [M4](#report-a-m4): 同心圆孔可用覆盖率乘法直接挖出

<a id="report-a-f3"></a>
#### F3

表面颜色: 外轮廓与中心孔之间的环带为单一平涂绿色, 无渐变、无明暗层次、无描边; 目测为中高饱和绿, 接近 #34A853(未取色, 仅目测估计)。

候选机制:

- [M5](#report-a-m5): 平涂无光影是该目标唯一可见的着色方式

<a id="report-a-f4"></a>
#### F4

边缘质量: 外轮廓与孔缘均为连续平滑曲线, 无多边形折角或锯齿; 齿顶呈圆弧形, 凹谷开口较窄, 目测谷宽与齿宽约 1:2。

候选机制:

- [M2](#report-a-m2): 并集 SDF 配合 smoothstep 给出连续平滑边缘
- [M1](#report-a-m1): 距离场覆盖同样保证边缘平滑
- [M3](#report-a-m3): 剖面法同样以 SDF 平滑边缘

<a id="report-a-f5"></a>
#### F5

邻片间隙: 目标右缘与相邻黄色圆片、右下缘与另一黄色圆片之间均保留背景白色间隙, 齿瓣互不接触; 目标整圈外轮廓无遮挡、完整可观察(间隙宽度目测约 5–15px, 属堆叠布局上下文)。

候选机制: 未登记.

### 关系 R

<a id="report-a-r1"></a>
#### R1

中心孔与外轮廓同心: 孔心与盘体几何中心目测重合, 偏移小于外半径的 2%(估计)。

参与者:

- [F2](#report-a-f2): 孔边界
- [F1](#report-a-f1): 外轮廓

候选机制:

- [M4](#report-a-m4): [M4](#report-a-m4) 与外轮廓共用同一原点计算孔, 天然满足同心关系

<a id="report-a-r2"></a>
#### R2

外轮廓的周期起伏由齿瓣与凹谷沿圆周近似等角交替构成, 该交替同时决定边缘曲率节奏: 齿顶圆弧、窄谷圆滑过渡。

参与者:

- [F1](#report-a-f1): 周期轮廓
- [F4](#report-a-f4): 边缘形态

候选机制:

- [M2](#report-a-m2): 齿圆沿圆周等角排布直接实现齿瓣-凹谷周期交替
- [M1](#report-a-m1): 余弦项按整数齿数给出等角周期起伏
- [M3](#report-a-m3): 剖面函数按整数齿数周期重复

<a id="report-a-r3"></a>
#### R3

绿色填充被外轮廓与中心孔共同约束为环带; 孔内与轮廓外均为背景白色, 填充在两条边界处同时终止。

参与者:

- [F3](#report-a-f3): 填充色
- [F1](#report-a-f1): 外界
- [F2](#report-a-f2): 内界

候选机制:

- [M4](#report-a-m4): [M4](#report-a-m4) 孔减法定义环带内界, 使填充在内界处终止
- [M5](#report-a-m5): [M5](#report-a-m5) 平涂按最终覆盖率着色, 使填充同时在外界处终止

<a id="report-a-r4"></a>
#### R4

邻片间隙使目标外轮廓整圈可见、无遮挡截断: 齿瓣计数与谷形可在整圈上目测(仍受原图分辨率限制), 无需对被遮挡段做推断补全。

参与者:

- [F5](#report-a-f5): 邻片间隙
- [F1](#report-a-f1): 外轮廓

候选机制: 未登记.

### 机制 M

<a id="report-a-m1"></a>
#### M1 · 极坐标余弦波纹圆盘

方法原文:

```text
在以盘体中心为原点的局部坐标 p 上取 r=length(p), θ=atan2(p.y,p.x); 波纹半径 radius(θ)=base_radius+amplitude*cos(lobe_count*(θ+phase)); 距离 d=r-radius(θ); 覆盖率 coverage=1-smoothstep(-edge_aa, edge_aa, d)。base_radius 为平均半径, amplitude 为波幅, lobe_count 为整数齿数, phase 为角向相位, edge_aa 为边缘平滑半宽。
```

适用条件:

- lobe_count 为正整数, 齿沿圆周均匀分布
- 凹谷为正弦形圆滑过渡, 谷宽与齿宽相近, 不产生窄尖谷

<a id="report-a-m2"></a>
#### M2 · 基圆并齿圆并集齿轮

方法原文:

```text
d_base=length(p)-base_radius; 对 i=0..tooth_count-1 计算 d_tooth_i=length(p - tooth_ring*unit(2π*i/tooth_count + phase)) - tooth_radius; d=min(d_base, min_i d_tooth_i); coverage=1-smoothstep(-edge_aa, edge_aa, d)。基圆决定谷底与盘体主体, 齿圆决定圆顶齿瓣; tooth_ring 为齿心分布半径, tooth_radius 为齿圆半径。
```

适用条件:

- tooth_count 为正整数, 齿圆均匀排布
- 齿圆伸出到基圆外并与基圆相交, 相邻齿圆互不连通, 由基圆段形成窄凹谷

<a id="report-a-m3"></a>
#### M3 · 锐谷平顶齿轮剖面

方法原文:

```text
极坐标中令 u=abs(cos(lobe_count*(θ+phase)/2)); 径向剖面 radius(θ)=base_radius*(1-notch_depth*(1-pow(u, valley_sharpness))); d=r-radius(θ), 覆盖率同前以 edge_aa 平滑。valley_sharpness 越大凹谷越窄越深, notch_depth 控制谷深比例, 得到宽齿顶、窄深谷的外形。
```

适用条件:

- 齿顶平缓或呈宽圆弧, 凹谷窄而深
- lobe_count 为正整数

<a id="report-a-m4"></a>
#### M4 · 同心圆孔减法

方法原文:

```text
在齿轮覆盖率上乘孔外掩码: hole_mask=smoothstep(hole_radius-edge_aa, hole_radius+edge_aa, length(p)); final_coverage=gear_coverage*hole_mask。hole_radius=hole_ratio*base_radius, hole_ratio 目测约 0.25, 待校准。
```

适用条件:

- 孔与盘体同心, 共用同一原点
- hole_radius 小于谷底半径, 孔不与外轮廓相交

<a id="report-a-m5"></a>
#### M5 · 纯色平涂混合

方法原文:

```text
color=mix(background_color, gear_color, final_coverage); gear_color 为单一常数(绿色, 具体值需取色校准), background_color 取原图背景白; 不引入光照、渐变、描边或纹理。
```

适用条件:

- 目标为无光照平面插画风格
- gear_color 常数值需从原图采样确认

### 草图 S

<a id="report-a-s1"></a>
#### S1 · 绿色齿轮圆片·基圆并齿圆并集方案

目标元素: [E1](#report-a-e1)

默认机制组合:

| 作用目标 | 采用机制 |
| --- | --- |
| [F1](#report-a-f1) | [M2](#report-a-m2) |
| [F2](#report-a-f2) | [M4](#report-a-m4) |
| [F3](#report-a-f3) | [M5](#report-a-m5) |
| [F4](#report-a-f4) | [M2](#report-a-m2) |
| [R1](#report-a-r1) | [M4](#report-a-m4) |
| [R2](#report-a-r2) | [M2](#report-a-m2) |
| [R3](#report-a-r3) | [M4](#report-a-m4), [M5](#report-a-m5) |

组织说明:

以盘体中心为原点建立局部坐标 p: 用 [M2](#report-a-m2) 由基圆与 8 枚均布齿圆的 SDF 并集生成 [F1](#report-a-f1) 的圆顶齿外轮廓, 同一距离场保证 [F4](#report-a-f4) 的平滑边缘与窄凹谷, 并以等角排布实现 [R2](#report-a-r2) 的齿瓣-凹谷周期交替; 用 [M4](#report-a-m4) 以 hole_ratio≈0.25(相对谷底基圆半径, 对应孔径约为外径的 0.2)在同一原点从覆盖率中挖出 [F2](#report-a-f2) 中心孔, 共用原点满足 [R1](#report-a-r1); 用 [M5](#report-a-m5) 将最终覆盖率与单一绿色常数对白色背景混合, 得到 [F3](#report-a-f3) 的平涂填充, 填充在内外两界同时终止形成环带([R3](#report-a-r3))。base_radius、tooth_ring、tooth_radius、edge_aa 与 gear_color 按目测估计初始化, 待取色与量尺校准。

##### 局部备选 1

替换项:

| 作用目标 | 采用机制 |
| --- | --- |
| [F1](#report-a-f1) | [M1](#report-a-m1) |
| [F4](#report-a-f4) | [M1](#report-a-m1) |
| [R2](#report-a-r2) | [M1](#report-a-m1) |

理由: 若凹谷被证实为均匀正弦波纹、谷宽接近齿宽, 余弦调制参数最少、更稳健。

适用条件:

- 原报告未提供.

组织说明:

用 [M1](#report-a-m1) 的极坐标余弦调制生成 [F1](#report-a-f1) 的均匀波纹外轮廓, 同一距离场保证 [F4](#report-a-f4) 平滑边缘并实现 [R2](#report-a-r2) 周期交替; 其余保持默认: [M4](#report-a-m4) 挖出 [F2](#report-a-f2) 同心孔并满足 [R1](#report-a-r1), [M5](#report-a-m5) 平涂 [F3](#report-a-f3) 环带([R3](#report-a-r3))。

##### 局部备选 2

替换项:

| 作用目标 | 采用机制 |
| --- | --- |
| [F1](#report-a-f1) | [M3](#report-a-m3) |
| [F4](#report-a-f4) | [M3](#report-a-m3) |
| [R2](#report-a-r2) | [M3](#report-a-m3) |

理由: 若凹谷被证实为窄而深的尖切口、齿顶平缓, 采用锐谷剖面获得更硬的谷形。

适用条件:

- 原报告未提供.

组织说明:

用 [M3](#report-a-m3) 的锐谷剖面生成 [F1](#report-a-f1) 的窄深谷齿轮外轮廓, 同一剖面保证 [F4](#report-a-f4) 边缘平滑并实现 [R2](#report-a-r2) 周期交替; 其余保持默认: [M4](#report-a-m4) 挖出 [F2](#report-a-f2) 同心孔并满足 [R1](#report-a-r1), [M5](#report-a-m5) 平涂 [F3](#report-a-f3) 环带([R3](#report-a-r3))。

### 未知项

1. **涉及 [F1](#report-a-f1)** — 齿瓣数量目测为 8, 需在更高分辨率下绕整圈复核齿数与角向相位(齿顶/凹谷相对 12 点方向的相位), 直接决定 tooth_count/lobe_count 与 phase 取值。

2. **涉及 [F4](#report-a-f4)** — 凹谷剖面介于圆滑波谷与窄尖切口之间, [M1](#report-a-m1)/[M2](#report-a-m2)/[M3](#report-a-m3) 的取舍及 valley_sharpness、tooth 参数需放大原图比对; 同时需确认各凹谷深度与宽度是否一致(个别凹谷目测似有深浅差异, 也可能是分辨率或抗锯齿所致)。

3. **涉及 [F2](#report-a-f2)** — 中心孔是透明通孔还是白色填充? 本图中孔内只见背景白, 两种实现在单体复现中等价; 若复现整幅堆叠需确认孔的层级语义。

4. **涉及 [F3](#report-a-f3)** — 绿色色值未经取色, 需采样原图环带中部像素确定 gear_color 常数(目测接近 #34A853)。

5. **涉及 [F5](#report-a-f5)** — 目标与相邻黄色圆片在齿尖对齿尖处间隙极窄(几何估计约 2–7px), 当前分辨率难以确认是否完全无接触; 若存在接触会影响堆叠复现的间距参数, 但不影响单体轮廓构造。

### 缺口

1. **涉及 [F5](#report-a-f5), [R4](#report-a-r4)** — 邻片间隙属于堆叠场景的布局上下文, 不由目标单体 Shader 构造, 草图不覆盖; 若需复现整幅堆叠, 还缺实例摆放与间距布局机制(超出本轮目标范围)。

2. **涉及 [F1](#report-a-f1), [F2](#report-a-f2), [F3](#report-a-f3)** — 缺少像素级测量与取色: 平均半径、齿深比例、孔径比与绿色色值均为目测估计, 需要校准后才能固化 Shader 常数。

---

## 报告 B · 完整输出

[原始 JSON](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/exploration-skill-v2-20261026/02-staged/accepted-report.json) · [原始输入与试验记录](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/exploration-skill-v2-20261026/02-staged/trial.json)

模型: `z-ai/glm-5.3-flash`; 调用: 11 次; 修复: 2 次; 耗时: 365.019 秒.

<a id="report-b-e1"></a>
### 固定目标 E1（输入材料）

名称: 最左上方绿色齿轮状圆片

范围: 原图左侧彩色堆叠区域中最左上方的绿色圆片, 包含中心孔和外轮廓

探索方向: 围绕目标的可见特征和相互关系, 提出可用于过程式 Shader 的实现机制和草图。

### 特征 F

<a id="report-b-f1"></a>
#### F1

绿色圆片主体: 扁平单色圆盘, 位于参考图左侧彩色堆叠区最左上方, 圆心目测约在 1280x720 画布的 (260, 408) 处 (估计); 外接直径目测约 100px (估计), 填充为均匀绿色, 目测约 #3AA853 (估计值)。

候选机制:

- [M2](#report-b-m2): 基圆 SDF 减等角切割圆能直接复现 [F1](#report-b-f1) 外轮廓与 [F2](#report-b-f2) 中'圆弧齿顶+圆弧凹槽'的齿轮剪影, 几何最贴合。
- [M1](#report-b-m1): 极坐标周期轮廓可等效生成 [F1](#report-b-f1) 的同一剪影, 适合需连续调节凹槽波形或避免逐切割圆求最小值的场合。

<a id="report-b-f2"></a>
#### F2

外轮廓凹槽: 沿外缘等角距分布的圆弧形内凹缺口, 目测 6 个 (60° 等角距; 计数在 6-8 间仍需逐像素核实, 当前读数最支持 6); 缺口切入深度约为半径的 10%-15% (估计), 缺口截面圆滑呈圆弧, 齿顶段为基圆圆弧且较宽, 整体呈齿轮/链轮式剪影; 相位上目测一个凹槽位于正上方 (≈0°, 误差 ±15°)。

候选机制:

- [M2](#report-b-m2): 切割圆阵列天然给出等角距分布且截面为圆弧的缺口, 对应 [F2](#report-b-f2) 的圆滑凹槽与 [R2](#report-b-r2) 的均匀周期分布。
- [M1](#report-b-m1): notch_wave 周期波形以 notch_count 为周期重复, 同样满足 [R2](#report-b-r2) 的等角距分布与 [R3](#report-b-r3) 的幅度约束。

<a id="report-b-f3"></a>
#### F3

中心圆孔: 位于圆片几何中心的白色圆形通孔, 孔径约为外径的 25%-30% (目测估计); 孔内为页面背景色, 无其他填充或描边。

候选机制:

- [M3](#report-b-m3): 孔是 [F1](#report-b-f1) 外轮廓的同心内圆 ([R1](#report-b-r1)), 用 SDF 差集打孔最直接, 孔径可由 hole_radius 单参数控制。

<a id="report-b-f4"></a>
#### F4

平色硬边渲染: 圆片为单色平涂, 无描边、无投影、无渐变、无纹理; 轮廓与孔边缘锐利, 仅见像素级抗锯齿软化。

候选机制:

- [M4](#report-b-m4): 平色硬边对应 SDF 阈值化加像素级平滑, 同时作用于 [F1](#report-b-f1) 外轮廓与 [F3](#report-b-f3) 孔边界, 满足 [R4](#report-b-r4) 的边界质量一致性。

### 关系 R

<a id="report-b-r1"></a>
#### R1

同心关系: 中心孔与主体外轮廓共用同一圆心, 孔是外轮廓的同心内圆。

参与者:

- [F3](#report-b-f3): 内孔
- [F1](#report-b-f1): 主体

候选机制:

- [M3](#report-b-m3): [M3](#report-b-m3) 以同一局部原点计算 d_outer 与 d_hole 并取 max(d_outer, hole_radius - length(p)), 孔与外轮廓共用圆心, 直接实现同心关系。

<a id="report-b-r2"></a>
#### R2

周期分布关系: 凹槽沿主体外缘等角距重复出现, 相邻凹槽角间距一致, 目测约 60° (即 6 个周期, 待核), 构成旋转不变的花样; 相位参考: 一个凹槽朝向正上方 (估计)。

参与者:

- [F2](#report-b-f2): 重复单元
- [F1](#report-b-f1): 承载轮廓

候选机制:

- [M2](#report-b-m2): [M2](#report-b-m2) 的切割圆按 2π/notch_count 等角放置, 凹槽沿外缘等角距重复, 实现周期分布。
- [M1](#report-b-m1): [M1](#report-b-m1) 的 notch_wave(notch_count*theta) 以 notch_count 为周期重复, 同样实现等角距分布。

<a id="report-b-r3"></a>
#### R3

幅度关系: 凹槽只向内切入基圆, 齿顶落在基圆上, 谷底半径一致; 轮廓半径随角度在谷底半径与 base_radius 之间周期变化, 无向外凸出的齿。

参与者:

- [F2](#report-b-f2): 内切缺口
- [F1](#report-b-f1): 基圆

候选机制:

- [M2](#report-b-m2): [M2](#report-b-m2) 中切割圆圆心落在基圆上, 齿顶保持基圆圆弧、谷底半径为 base_radius - notch_radius, 轮廓半径只在谷底与 base_radius 间变化且无外凸齿, 满足幅度约束。
- [M1](#report-b-m1): [M1](#report-b-m1) 的 profile_r = base_radius - notch_depth*n(theta) 把轮廓半径限制在 [谷底, base_radius] 区间, 满足幅度约束。

<a id="report-b-r4"></a>
#### R4

渲染一致性: 同一平色硬边规则同时决定主体绿色区域与中心孔镂空区域的边界表现, 两者边界质量一致。

参与者:

- [F4](#report-b-f4): 渲染属性
- [F1](#report-b-f1): 绿色区域
- [F3](#report-b-f3): 镂空区域

候选机制:

- [M4](#report-b-m4): [M4](#report-b-m4) 对最终 SDF 统一施加 smoothstep 硬边与单色平涂, 外轮廓与孔边界共用同一规则, 实现渲染一致性。

### 机制 M

<a id="report-b-m1"></a>
#### M1 · 极坐标周期轮廓生成

方法原文:

```text
对局部坐标 p 计算 r=length(p), theta=atan2(p.y, p.x); 用以 notch_count 为周期的波形 notch_wave(notch_count*theta)∈[0,1] (平滑方波或余弦抬升) 得到凹槽强度 n(theta); 轮廓半径 profile_r = base_radius - notch_depth*n(theta), 覆盖条件为 r < profile_r。一次求值即得整圈齿轮剪影, 凹槽波形可连续调节。
```

适用条件:

- 在极坐标下逐像素求值
- 凹槽沿角度以 notch_count 为周期重复
- 齿顶半径为常量 base_radius, 谷底半径 = base_radius - notch_depth

<a id="report-b-m2"></a>
#### M2 · 圆 SDF 布尔减缺口阵列

方法原文:

```text
d_base = length(p) - base_radius; 在半径 base_radius 的圆周上按 2π/notch_count 等角放置 notch_count 个切割圆, 切割圆半径 notch_radius, 圆心落在基圆上; d_notch = min_i( length(p - cutter_center_i) - notch_radius ), 可借角度取模简化为单次求值; d_outer = max(d_base, d_notch)。切割圆与基圆相交所留缺口截面为圆弧, 齿顶保持基圆圆弧。
```

适用条件:

- 凹槽截面按圆弧近似
- 齿顶段仍为基圆圆弧
- 切割圆心落在基圆上时切入深度 notch_depth = notch_radius

<a id="report-b-m3"></a>
#### M3 · 同心圆孔差集

方法原文:

```text
d_hole = length(p) - hole_radius (孔内为负); d_final = max(d_outer, -d_hole), 即从外轮廓 SDF 中减去同心圆孔; 孔边界与外边界获得相同的抗锯齿处理, hole_radius 为可调单参数。
```

适用条件:

- 孔与外轮廓同心
- 孔为单一圆形, 无环带或倒角

<a id="report-b-m4"></a>
#### M4 · 硬边平色覆盖率输出

方法原文:

```text
coverage = 1 - smoothstep(-edge_aa, edge_aa, d_final), edge_aa 取约 1-2 像素; 输出 fill_color * coverage 的单色平涂, 不叠加光照、渐变或描边, 背景处 coverage 为 0。
```

适用条件:

- 边缘锐利仅需像素级抗锯齿
- 单色平涂, 无材质变化

### 草图 S

<a id="report-b-s1"></a>
#### S1 · SDF 布尔齿轮圆片方案

目标元素: [E1](#report-b-e1)

默认机制组合:

| 作用目标 | 采用机制 |
| --- | --- |
| [F1](#report-b-f1) | [M2](#report-b-m2) |
| [F2](#report-b-f2) | [M2](#report-b-m2) |
| [F3](#report-b-f3) | [M3](#report-b-m3) |
| [F4](#report-b-f4) | [M4](#report-b-m4) |
| [R1](#report-b-r1) | [M3](#report-b-m3) |
| [R2](#report-b-r2) | [M2](#report-b-m2) |
| [R3](#report-b-r3) | [M2](#report-b-m2) |
| [R4](#report-b-r4) | [M4](#report-b-m4) |

组织说明:

以圆片中心为局部坐标原点。用 [M2](#report-b-m2) 生成 [F1](#report-b-f1) 外轮廓与 [F2](#report-b-f2) 凹槽: d_base = length(p) - base_radius (base_radius 目测约 51px), notch_count≈6 个切割圆沿基圆每 60° 等角分布 (实现 [R2](#report-b-r2) 的 60° 周期), notch_phase≈0° 使一个凹槽位于正上方 (目测相位, 待校准), 圆心落在基圆上, notch_radius≈0.15*base_radius (实现 [R3](#report-b-r3) 的齿顶在基圆、谷底半径一致的幅度约束), d_outer = max(d_base, d_notch); 用 [M3](#report-b-m3) 取 d_final = max(d_outer, hole_radius - length(p)) 打出同心 [F3](#report-b-f3), hole_radius≈0.28*base_radius (目测, 同心性即 [R1](#report-b-r1)); 最后用 [M4](#report-b-m4) 以 coverage = 1 - smoothstep(-edge_aa, edge_aa, d_final) 输出均匀绿色 fill_color≈#3AA853 (目测) 的硬边平色, 外轮廓与孔边界共用同一规则 ([R4](#report-b-r4)), 不含光照与渐变。

##### 局部备选 1

替换项:

| 作用目标 | 采用机制 |
| --- | --- |
| [F1](#report-b-f1) | [M1](#report-b-m1) |
| [F2](#report-b-f2) | [M1](#report-b-m1) |
| [R2](#report-b-r2) | [M1](#report-b-m1) |
| [R3](#report-b-r3) | [M1](#report-b-m1) |

理由: 若目标平台需避免逐切割圆求最小值, 或需连续调节凹槽截面形状 (如圆角矩形槽) 时, 改用极坐标周期轮廓; 其余默认选择不变。

适用条件:

- 原报告未提供.

组织说明:

用 [M1](#report-b-m1) 以 profile_r = base_radius - notch_depth * notch_wave(notch_count*theta + notch_phase) (notch_depth≈0.15*base_radius, notch_count≈6, notch_phase≈0° 使一个凹槽朝向正上方, 波形取平滑方波或余弦抬升) 直接生成 [F1](#report-b-f1) 外轮廓与 [F2](#report-b-f2) 凹槽, 其周期性实现 [R2](#report-b-r2), 幅度约束实现 [R3](#report-b-r3); [F3](#report-b-f3) 仍由 [M3](#report-b-m3) 差集打孔 ([R1](#report-b-r1) 同心), [F4](#report-b-f4) 仍由 [M4](#report-b-m4) 硬边平色 ([R4](#report-b-r4) 一致性)。

### 未知项

1. **涉及 [F2](#report-b-f2), [R2](#report-b-r2)** — 凹槽数量目测最可能为 6 (60° 等角距), 但 6-8 均有可能, 需逐像素计数核实; 同时确认缺口截面是正圆弧还是圆角矩形槽, 这决定 [M2](#report-b-m2) 的切割体形状或 [M1](#report-b-m1) 的 notch_wave 波形。

2. **涉及 [F1](#report-b-f1), [F3](#report-b-f3)** — 精确色值、外接直径与孔径/外径比例未实测, 目前均为目测估计 (绿≈#3AA853, hole_radius≈0.28*base_radius), 复现前需取色与量距校准。

3. **涉及 [F2](#report-b-f2)** — 凹槽切入深度 (目测约为半径的 10%-15%) 与初始角相位 (目测一个凹槽位于正上方, ±15°) 未精确测定, 复现时需与原图对齐校准。

4. **涉及 [F1](#report-b-f1)** — 与相邻黄色圆片的间隙: 中心距约 105px 略大于两外半径之和 (约 102px), 初步判断无压叠、[F1](#report-b-f1) 剪影完整, 但齿顶对齿顶的局部间隙仅约 3px, 是否精确相切需放大核实。

### 缺口

1. **涉及 [E1](#report-b-e1), [F1](#report-b-f1)** — 堆叠遮挡不在本轮范围: 当前机制只绘制孤立圆片; 若要复现整堆彩色圆片的相互遮挡与绘制顺序 (含相邻圆片相位交错咬合的外观), 需补充层级/遮挡机制。

2. **涉及 [F4](#report-b-f4)** — 原图未见描边、投影、高光等材质细节, 现有机制不含任何光照; 若最终风格要求立体感, 需另补高光/投影机制。

3. **涉及 [F2](#report-b-f2), [R2](#report-b-r2)** — 若逐像素核实后凹槽为非等距分布或计数超出 6-8 区间, [M1](#report-b-m1)/[M2](#report-b-m2) 的等角周期假设需改用非均匀角度分布方案。

---
