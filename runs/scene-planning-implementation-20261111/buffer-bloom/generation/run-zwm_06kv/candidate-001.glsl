float dot2(vec2 v){ return dot(v,v); }

mat2 rot2(float a){ float c=cos(a), s=sin(a); return mat2(c,-s,s,c); }

// 圆角五角星 SDF (外接半径 r, 内半径比 rf)
float sdStar5(vec2 p, float r, float rf){
    const vec2 k1 = vec2(0.809016994,-0.587785252);
    const vec2 k2 = vec2(-k1.x,k1.y);
    p.x = abs(p.x);
    p -= 2.0*max(dot(k1,p),0.0)*k1;
    p -= 2.0*max(dot(k2,p),0.0)*k2;
    p.x = abs(p.x);
    return length(p-vec2(clamp(p.x,-r*rf,r*rf),r)) * sign(p.y-r);
}

// 解析心形 SDF, 原生姿态为尖角朝下 (y 向上坐标系)
float sdHeart(vec2 p){
    p.x = abs(p.x);
    if(p.y+p.x>1.0)
        return sqrt(dot2(p-vec2(0.25,0.75))) - sqrt(2.0)/4.0;
    return sqrt(min(dot2(p-vec2(0.0,1.0)),
                    dot2(p-0.5*max(p.x+p.y,0.0)))) * sign(p.x-p.y);
}

float sdCircle(vec2 p, float r){ return length(p)-r; }

void mainImage(out vec4 fragColor, in vec2 fragCoord){
    vec2 res = iResolution.xy;
    // M1: 纵横比校正坐标, y 向上
    vec2 uv = (fragCoord - 0.5*res)/res.y;

    // M2: 五角星 SDF, 中心 (0.18,0.36), 外接半径 0.13, 姿态倾斜 -8°, 圆角 0.02
    vec2 sq = rot2(-0.14) * (uv - vec2(0.18,0.36));
    float dS = sdStar5(sq, 0.13, 0.46) - 0.02;

    // M3: 圆形 SDF, 圆心 (0,0), 半径 0.10
    float dC = sdCircle(uv, 0.10);

    // M4: 倒置心形 SDF, 中心 (-0.19,-0.35), 半高约 0.12, 先旋转 180° 使尖角朝上
    vec2 hc = vec2(-0.19,-0.35);
    float hs = 0.218; // 缩放: 原生心形总高约 1.1 → 目标半高 0.12
    vec2 hq = -(uv - hc)/hs;
    float dH = sdHeart(hq)*hs - 0.012;

    vec3 col = vec3(0.0);

    // M5: 宽域指数光晕 (加法累加, 交叠形成暖黄→紫粉→蓝过渡)
    // 星: 暖黄橙晕
    col += vec3(1.00,0.62,0.25) * 0.85 * exp(-2.2*max(dS,0.0));
    // 圆: 粉紫红晕
    col += vec3(0.85,0.35,0.75) * 0.85 * exp(-2.6*max(dC,0.0));
    // 心: 蓝晕, 对 d 施加向下/左下方向偏置使扩散偏下 (F8)
    vec2 hd = uv - hc;
    float hl = max(length(hd), 1e-4);
    float down = smoothstep(0.1,-0.6, hd.y/hl);          // 指向下方时为 1
    float dHe = dH * mix(1.0, 0.55, down*0.85);
    col += vec3(0.35,0.65,1.00) * 0.85 * exp(-2.4*max(dHe,0.0));

    // M6: 实心核心着色 (中心近白, 边缘保留饱和基色)
    // 星: 中心亮黄近白, 边缘饱和黄
    float mS = 1.0 - smoothstep(-0.004, 0.004, dS);
    vec3 cS = mix(vec3(1.00,0.85,0.30), vec3(1.00,0.97,0.75),
                  pow(clamp(-dS/0.13,0.0,1.0), 0.6));
    col += mS * cS;

    // 圆: 中心近白, 边缘淡粉
    float mC = 1.0 - smoothstep(-0.004, 0.004, dC);
    vec3 cC = mix(vec3(1.00,0.60,0.85), vec3(1.00,0.96,0.98),
                  pow(clamp(-dC/0.10,0.0,1.0), 0.5));
    col += mC * cC;

    // 心: 中心浅蓝近白, 边缘亮蓝
    float mH = 1.0 - smoothstep(-0.004, 0.004, dH);
    vec3 cH = mix(vec3(0.45,0.75,1.00), vec3(0.85,0.96,1.00),
                  pow(clamp(-dH/0.12,0.0,1.0), 0.6));
    col += mH * cH;

    // M7: 输出 (加色合成后截断)
    fragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}