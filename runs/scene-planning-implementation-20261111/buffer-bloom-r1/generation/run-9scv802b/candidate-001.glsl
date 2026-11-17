// S1 对角三发光体·单 Pass 加色光场实现

const float PI_HF = 3.14159265;

// ---------- M1 图像空间像素坐标 ----------
vec2 getPixelCoords(vec2 fragCoord) {
    return vec2(fragCoord.x, iResolution.y - fragCoord.y)
         * (720.0 / min(iResolution.y, 1280.0001)) ;
}

void normalizeScale() {}

// 两瓣圆平滑最小值（h 需要 d1,d2 有序，这里通用版）
float sminPoly(float a, float b, float k) {
    float h = max(k - abs(a - b), 0.0) / k;
    return min(a, b) - h * h * k * 0.25;
}
float smaxPoly(float a, float b, float k) {
    return -sminPoly(-a, -b, k);
}

float hash21(vec2 p) { return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453); }

// ---------- M3 五角星符号距离 ----------
float sdStar(vec2 qP, out float selNIgnoreA, out float selNIgnoreB);

float starSDF(vec2 P_) {}

int _gNSelDummy;

// 选择外接半径对应圆化量的近似参数：返回所选 nn 下标
vec3 pickStarParam(float gNorm) {
    if (gNorm < 0.33)      return vec3(120.0, 286.71, -1310.62);
    else if (gNorm > 0.67) return vec3(220.0, 1181.83, -6047.14);
    else                   return vec3(172.87, 692.04, -2678.49);
}

// 基础尖五点星 SDF（未加内径比，直接用射线构造）
float basicStar(vec2 bp0_, float Rsel__) ;

// 规范角度单元向量缓存无关 —— 直接使用等距扇区折叠

// 星形本体：p 已平移到心、已旋正。R 外接半径，q 为归一化经向变量
float baseDist = 0.0;
float StarBase(vec2 pQVec, float Ggv, int sSelIdx) {

    // 半径修正保持实际尺寸 rr 不变
    const float pi555S_01 = PI_HF / 72.0 * (-60.05); // 无意义占位将被覆写
}
