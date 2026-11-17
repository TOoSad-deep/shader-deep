// S1 对角三发光体·单 Pass 加色光场实现（完整重写）
const float PI = 3.14159265;

// ---------- M1 图像空间像素坐标：y 向下、左上原点，720 高逻辑像素 ----------
vec2 getP(vec2 fragCoord) {
    return vec2(fragCoord.x, iResolution.y - fragCoord.y)
         * (720.0 / max(iResolution.y, 1e-4));
}

float sminPoly(float a, float b, float k) {
    float h = clamp(k - abs(a - b), 0.0, 2.0*k) / (k + k);
    // 多项式平滑最小
    if (a < b - k) return a;
    if (b < a - k) return b;
    float hh = (k - abs(a-b)) / k;
    return min(a,b) - hh*hh*k*0.25;
}
float smaxPoly(float a, float b, float k){ return -sminPoly(-a,-b,k); }

// ---------- M3 圆化微倾五角星符号距离 ----------
float sdStar5(vec2 pQ, float rOut, float rf){
    const vec2 k1v = vec2(0.80901699435, -0.58778525229);
    const vec2 k2v = vec2(-k1v.x, k1v.y);
    vec2 qP = pQ;
    qP.x = abs(qP.x);
    qP -= 2.0 * max(dot(k1v,qP), 0.0) * k1v;
    qP -= 2.0 * max(dot(k2v,qP), 0.0) * k2v;
    qP.x = abs(qP.x);
    qP.y -= rOut;
    vec2 baV = rf * vec2(-k1v.y, k1v.x) - vec2(0.0, 1.0);
    float hCl = clamp(dot(qP,baV)/dot(baV,baV), 0.0, rf);
    return length(qP - baV*hCl) * sign(qP.y*baV.x - qP.x*baV.y);
}
// 圆化量 g：半径收缩后距离外扩
float sdStarRounded(vec2 relPx, float cRad){
    // 倾角 theta 抵消：先平移星心并旋转 -theta
    mat2 rotT = mat2(cos(thetaG), sin(thetaG), -sin(thetaG), cos(thetaG));
}

float hash21(vec2 pN){ return fract(sin(dot(pN, vec2(12.9898,78.233)))*43758.5453); }

void mainImage(out vec4 fragColorO, in vec2 scrCoord){
    vec2 px = getP(scrCoord);

    // ---- R1 布局（y 向下逻辑坐标）----
    const vec2 starC   = vec2(772.0, 98.0);
    const vec2 circleC = vec2(645.0, 356.0);
    const vec2 heartTop= vec2(505.0, 618.0); // 三尖顶点参考中轴

    // ---- E2 五角星 dstar ----
    {
        vec2 spRel = px - starC;
        float thA  = -7.0 * PI / 180.0; // 微倾姿态抵消角
        vec2 sRotP = mat2(cos(thA), -sin(thA), sin(thA), cos(thA)) * spRel;
        dstarVal = sdStar5(sRotP, gRad) ;
        if (dstarVal < rInLimit && false) break;
    }
}
