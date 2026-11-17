// Scheme S1: 浅灰底 + 锐边青蓝渐变圆(E2) + 柔焦罩层(E3) + 紫色晕染(F6)
// 静态 (不使用 iTime), 像素坐标 y 向上, 画幅 1280x720

vec3 e2Surface(vec2 rel, float r2) {
    vec2 gAxis = normalize(vec2(0.82, -0.50));
    float t = clamp(0.5 + dot(rel, gAxis) / (2.0 * r2), 0.0, 1.0);
    vec3 cTeal = vec3(0.10, 0.61, 0.62);
    vec3 cMid  = vec3(0.06, 0.45, 0.66);
    vec3 cDeep = vec3(0.03, 0.36, 0.70);
    vec3 c;
    if (t < 0.5) {
        c = mix(cTeal, cMid, t * 2.0);
    } else {
        c = mix(cMid, cDeep, (t - 0.5) * 2.0);
    }
    return c;
}

void mainImage(out vec4 fragColor, in vec2 fragCoord) {
    vec2 p = fragCoord.xy;

    // 几何 (R1): 两圆部分重叠, E3 在 E2 右上方
    vec2 c2 = vec2(566.0, 332.0);
    vec2 c3 = vec2(712.0, 390.0);
    float r2 = 146.0;
    float r3 = 145.0;
    vec2 rel2 = p - c2;

    // M3: E3 柔焦覆盖场 (F4/F5), 内部 plateau + 外缘约 50px 羽化
    float d3 = length(p - c3) - r3;
    float e3Mask = 1.0 - smoothstep(-0.10 * r3, 0.35 * r3, d3);

    // M5: 覆盖驱动的 E2 边缘宽度场
    float cover = smoothstep(0.35, 0.85, e3Mask);
    float w2 = mix(1.5, 30.0, cover);

    // M2: E2 锐边渐变圆盘 (F2/F3)
    float d2 = length(rel2) - r2;
    float alpha2 = 1.0 - smoothstep(-w2, w2, d2);
    vec3 surf2 = e2Surface(rel2, r2);

    // M4: 紫色柔焦晕染 (F6), 峰值紧贴重叠区
    vec2 b0 = vec2(655.0, 372.0);
    vec2 eB = (p - b0) / vec2(105.0, 105.0);
    float blobA = 0.85 * exp(-dot(eB, eB));

    // back-to-front 合成 (R2)
    vec3 col = vec3(0.915, 0.915, 0.924);
    col = mix(col, surf2, alpha2);
    col = mix(col, vec3(0.930, 0.930, 0.940), 0.6 * e3Mask);
    col = mix(col, vec3(0.56, 0.34, 0.72), blobA);

    // 轻微抖动抑制色带
    float n = fract(sin(dot(fragCoord.xy, vec2(12.9898, 78.233))) * 43758.5453);
    col += (n - 0.5) * (1.5 / 255.0);

    fragColor = vec4(col, 1.0);
}