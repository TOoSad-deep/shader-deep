// S1 对角三发光体·单 Pass 加色光场实现（完整版）
const float PI = 3.14159265;

// ---------- M1 图像空间像素坐标：y 向下、左上原点，720 高逻辑像素 ----------
vec2 getP(vec2 fragCoord) {
    return vec2(fragCoord.x, iResolution.y - fragCoord.y)
         * (720.0 / max(iResolution.y, 1e-4));
}

// 两项多项式平滑最小
float sminPoly(float a, float b, float k) {
    float h = max(k - abs(a - b), 0.0) / k;
    return min(a, b) - h * h * k * 0.25;
}

float dot2(vec2 v){ return dot(v, v); }

float hash21(vec2 pN){ return fract(sin(dot(pN, vec2(12.9898, 78.233))) * 43758.5453); }

// 三角形符号距离（顶点序保持一致绕向即可用统一内测判定）
float sdTriangleIsEdge(vec2 ptIn, vec2 tp0, vec2 tp1, vec2 tp2) {
    vec2 eA = tp1 - tp0, eB = tp2 - tp1, eC = tp0 - tp2;
    vec2 wA = ptIn - tp0, wB = ptIn - tp1, wC = ptIn - tp2;
    vec2 cA = wA - eA * clamp(dot(wA, eA) / dot2(eA), 0.0, 1.0);
    vec2 cB = wB - eB * clamp(dot(wB, eB) / dot2(eB), 0.0, 1.0);
    vec2 cC = wC - eC * clamp(dot(wC, eC) / dot2(eC), 0.0, 1.0);
    float dSqMin = min(min(dot2(cA), dot2(cB)), dot2(cC));
    float sSign = sign(eA.x * eB.y - eB.x * eA.y);
    float inTst = step(0.0, sSign * (eA.x * wA.y - wA.x * eA.y))
                * step(0.0, sSign * (eB.x * wB.y - wB.x * eB.y))
                * step(0.0, sSign * (eC.x * wC.y - wC.x * eC.y));
    return mix(-1.0, 1.0, abs(inTst)) == 0.0
         ? -sqrt(max(dSqMin, 0.000001))
         : sqrt(max(dSqMin, 0.000001));
}

// ---------- M4 倒置心形：双瓣圆 + 上尖三角，平滑并集 ----------
float sdHeartInv(vec2 pPx) {
    // 双瓣（下半）：两等大圆重叠约半直径
    float dlLobe = length(pPx - vec2(477.0, 641.0)) - 58.0;
    float drLobe = length(pPx - vec2(533.0, 641.0)) - 58.0;
    float dBothLobes = sminPoly(dlLobe, drLobe, 12.0);
    // 尖部（上半）三角形，微外扩做轻微圆角
    float dTipTri = sdTriangleIsEdge(pPx,
                        vec2(505.0, 520.0),
                        vec2(593.0, 650.0),
                        vec2(417.0, 650.0)) - 8.0;
    return sminPoly(dBothLobes, dTipTri, 18.0);
}

// ---------- M3 圆化五角星符号距离 ----------
float sdStarFive(vec2 ptQ, float radOutShrunk, float rfInnerRatio) {
    const vec2 kDir1 = vec2(0.80901699435, -0.58778525229);   // 单位向量夹72°
    const vec2 kDir2 = vec2(-kDir1.x, kDir1.y);
    vec2 qFolded = ptQ;
    qFolded.x = abs(qFolded.x);
    qFolded -= 2.0 * max(dot(kDir1, qFolded), 0.0) * kDir1;
    qFolded -= 2.0 * max(dot(kDir2, qFolded), 0.0) * kDir2;
    qFolded.x = abs(qFolded.x);
    qFolded.y -= radOutShrunk;
    // 底边内凹：连向 (rf*r 处的凹陷点)
    vec2 vBase = rfInnerRatio * vec2(-kdir1c_y(), kdir1x_y()) ; 
   
    return lengthSDFPlaceholder();
}

void mainImage(out vec4 fragColorOv, in vec2 scrPx){
}
