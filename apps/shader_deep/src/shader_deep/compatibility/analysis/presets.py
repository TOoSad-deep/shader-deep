"""可复用的预设分析视角; 每个任务绑定不可变的视角快照."""

from shader_deep.domain.legacy import LensConfig

PRESET_LENSES = (
    LensConfig(
        id="graphics-2d",
        name="2D 图形",
        origin="preset",
        focus="轮廓、曲线、图层、排列与遮罩关系",
        method_notes=("从整图到局部描述形状和排列, 再提出二维组织方式。",),
        default_questions=("哪些曲线或图层可以解释图中的连续结构?",),
    ),
    LensConfig(
        id="spatial-3d",
        name="3D 空间",
        origin="preset",
        focus="支撑几何、投影、深度与遮挡",
        method_notes=("先寻找空间线索; 单张图片不能确定的深度关系保留为假设。",),
        default_questions=("疏密变化来自透视还是元素自身分布?",),
    ),
    LensConfig(
        id="aesthetics",
        name="视觉与美学",
        origin="preset",
        focus="构图、明暗节奏、色彩层级与关键视觉特征",
        method_notes=("说明哪些特征决定整体视觉身份, 以及局部与整体的关系。",),
        default_questions=("哪些视觉特征最需要保留?",),
    ),
    LensConfig(
        id="physical-composition",
        name="物理与组成",
        origin="preset",
        focus="基础元素、组织机制、材质外观及元素依赖",
        method_notes=("区分可见现象与物理解释; 发光、反射等解释需要图像依据。",),
        default_questions=("哪些元素需要共同变化才能保持视觉关系?",),
    ),
)
