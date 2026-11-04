#version 300 es
// == ShaderGen params ==
// param: u_xd | type: float | min: 0.12 | max: 0.22 | default: 0.158 | label: lobe offset
// param: u_r | type: float | min: 0.16 | max: 0.24 | default: 0.2105 | label: lobe radius
// param: u_yc | type: float | min: -0.02 | max: 0.10 | default: 0.0492 | label: lobe centre y
// param: u_tip | type: float | min: -0.42 | max: -0.26 | default: -0.3475 | label: tip y
// param: u_wmax | type: float | min: 0.30 | max: 0.39 | default: 0.3614 | label: body half width
// param: u_fall | type: float | min: 0.45 | max: 1.10 | default: 0.45 | label: taper curve
// param: u_glow | type: float | min: 0.50 | max: 1.40 | default: 1.0949 | label: bg glow
// param: u_hi | type: float | min: 0.30 | max: 1.30 | default: 0.55 | label: highlight gain
// == end params ==
precision highp float;
uniform vec2 u_resolution;
uniform float u_time;
uniform float u_xd;
uniform float u_r;
uniform float u_yc;
uniform float u_tip;
uniform float u_wmax;
uniform float u_fall;
uniform float u_glow;
uniform float u_hi;
out vec4 fragColor;

// 将点到边界线段的距离与水平射线交点一起累计.
// 距离始终来自有限线段, 尖端以下不会再变成横跨画布的半平面.
void accumulateEdge(vec2 p, vec2 a, vec2 b, inout float distance2, inout bool inside) {
    vec2 edge = b - a;
    vec2 delta = p - a;
    float t = clamp(dot(delta, edge) / max(dot(edge, edge), 1.0e-12), 0.0, 1.0);
    vec2 nearest = delta - t * edge;
    distance2 = min(distance2, dot(nearest, nearest));
    if ((a.y > p.y) != (b.y > p.y)) {
        float crossing = a.x + (p.y - a.y) * edge.x / edge.y;
        if (p.x < crossing) inside = !inside;
    }
}

vec2 bodyPoint(float t, vec2 shoulder, vec2 tip) {
    float height = shoulder.y - tip.y;
    float taper = clamp((u_fall - 0.45) / 0.65, 0.0, 1.0);
    vec2 control1 = shoulder - vec2(0.0, 0.40 * height);
    vec2 control2 = tip + vec2(shoulder.x * mix(0.62, 0.22, taper), 0.24 * height);
    float s = 1.0 - t;
    // 起始切线竖直, 与上方圆瓣在最外侧的切线相接; 终点固定在底尖.
    return s*s*s*shoulder + 3.0*s*s*t*control1 + 3.0*s*t*t*control2 + t*t*t*tip;
}

float heartDistance(vec2 p) {
    p.x = abs(p.x);
    // 圆瓣与主体共用最大半宽, 不再由两个不一致的边界直接取并集.
    // 极端 offset/radius 组合约束为相接的两瓣, 避免分裂为两个形状.
    float offset = min(u_xd, u_r * 0.999);
    float angle = acos(-offset / u_r);
    float scaleX = u_wmax / (offset + u_r);
    vec2 notch = vec2(0.0, u_yc + sqrt(max(u_r*u_r - offset*offset, 0.0)));
    vec2 shoulder = vec2(u_wmax, u_yc);
    vec2 tip = vec2(0.0, u_tip);
    float distance2 = 1.0e6;
    bool inside = false;
    vec2 previous = notch;
    // 每段曲线用 32 条有限线段近似; 镜像后构成闭合轮廓.
    // 中轴不是外边界, 不计入距离, 以免心形内部出现接缝.
    for (int index = 1; index <= 32; ++index) {
        float a = angle * (1.0 - float(index) / 32.0);
        vec2 next = vec2((offset + u_r*cos(a))*scaleX, u_yc + u_r*sin(a));
        accumulateEdge(p, previous, next, distance2, inside);
        previous = next;
    }
    previous = shoulder;
    for (int index = 1; index <= 32; ++index) {
        vec2 next = bodyPoint(float(index) / 32.0, shoulder, tip);
        accumulateEdge(p, previous, next, distance2, inside);
        previous = next;
    }
    return sqrt(distance2) * (inside ? -1.0 : 1.0);
}

void main(){
  vec2 p = (gl_FragCoord.xy - 0.5*u_resolution)/u_resolution.y;
  float y = p.y;

  float d = heartDistance(p);

  vec3 darkC = vec3(0.500, 0.400, 0.320);
  vec3 brtC  = vec3(0.818, 0.648, 0.534);
  vec2 bq = (p - vec2(0.0, 0.06))*vec2(1.0/0.75, 1.0/0.95);
  float g = exp(-dot(bq, bq))*u_glow;
  vec3 bg = mix(darkC, brtC, clamp(g, 0.0, 1.0));
  bg += vec3(0.040, 0.026, 0.070)*(1.0 - smoothstep(-0.50, -0.05, y));
  bg += vec3(0.030, 0.024, 0.018)*exp(-max(d, 0.0)*12.0);

  vec3 deepC = vec3(0.500, 0.072, 0.145);
  vec3 midC  = vec3(0.700, 0.145, 0.215);
  vec3 brtI  = vec3(0.845, 0.300, 0.345);
  vec2 c = vec2(0.125, 0.030);
  vec2 q = (p - c)*vec2(1.0/0.305, 1.0/0.345);
  float s = exp(-dot(q, q));
  vec3 col = mix(deepC, midC, smoothstep(0.0, 0.75, s));
  col = mix(col, brtI, smoothstep(0.50, 1.00, s)*0.92*u_hi);
  col *= 1.0 - 0.05*(1.0 - smoothstep(-0.348, -0.26, y));

  float cov = (1.0 - smoothstep(-0.0055, 0.0045, d));
  fragColor = vec4(mix(bg, col, cov), 1.0);
}