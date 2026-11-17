// ---- helpers ----
mat2 rot(float a){ float c = cos(a), s = sin(a); return mat2(c, -s, s, c); }

float smin(float a, float b, float k){
    float h = clamp(0.5 + 0.5*(b - a)/k, 0.0, 1.0);
    return mix(b, a, h) - k*h*(1.0 - h);
}

// five-pointed star SDF, point along +p.y
float sdStar5(vec2 p, float r, float rf){
    const vec2 k1 = vec2(0.809016994, -0.587785252);
    const vec2 k2 = vec2(-0.809016994, 0.587785252);
    p.x = abs(p.x);
    p -= 2.0*max(dot(k1, p), 0.0)*k1;
    p -= 2.0*max(dot(k2, p), 0.0)*k2;
    p.x = abs(p.x);
    p.y -= r;
    vec2 ba = rf*vec2(-0.809016994, 0.587785252) - vec2(0.0, 1.0);
    float h = clamp(dot(p, ba)/dot(ba, ba), 0.0, r);
    return length(p - ba*h)*sign(p.y*ba.x - p.x*ba.y);
}

// isosceles triangle SDF: apex at origin, base at y = q.y, half width q.x
float sdTriIso(vec2 p, vec2 q){
    p.x = abs(p.x);
    vec2 a = p - q*clamp(dot(p, q)/dot(q, q), 0.0, 1.0);
    vec2 b = p - q*vec2(clamp(p.x/q.x, 0.0, 1.0), 1.0);
    float s = -sign(q.y);
    vec2 d = min(vec2(dot(a, a), s*(p.x*q.y - p.y*q.x)),
                 vec2(dot(b, b), s*(p.y - q.y)));
    return -sqrt(d.x)*sign(d.y);
}

// far-field soft glow fog
vec3 fog(vec2 p, vec2 c, vec3 col, float rF, float a){
    float q = length(p - c)/rF;
    return col*(a*exp(-2.2*q));
}

void mainImage(out vec4 fragColor, in vec2 fragCoord){
    // M1: logical pixel coords, origin top-left, height = 720
    float S = 720.0/iResolution.y;
    vec2 p = vec2(fragCoord.x, iResolution.y - fragCoord.y)*S;

    // ---- shared SDFs ----
    // star (M3): slightly tilted (tip up ~180deg flip + small lean)
    vec2 sq = rot(3.0206)*(p - vec2(772.0, 98.0));
    float dStar = sdStar5(sq, 82.0, 0.5) - 8.0;

    // circle (M2)
    float dCirc = length(p - vec2(645.0, 356.0)) - 78.0;

    // inverted heart (M4): smooth union of two lobes + upward triangle tip
    float dL1 = length(p - vec2(477.0, 641.0)) - 56.0;
    float dL2 = length(p - vec2(533.0, 641.0)) - 56.0;
    float dLob = smin(dL1, dL2, 18.0);
    float dTri = sdTriIso(p - vec2(505.0, 512.0), vec2(84.0, 120.0)) - 6.0;
    float dHeart = smin(dLob, dTri, 24.0);

    // ---- M8: near-black base + additive composition ----
    vec3 col = vec3(0.010, 0.010, 0.012);

    // M7: far-field fog (E1/F2, R2, R3)
    col += fog(p, vec2(785.0,  85.0), vec3(1.000, 0.643, 0.251), 250.0, 0.30); // warm brown, upper right
    col += fog(p, vec2(645.0, 356.0), vec3(0.925, 0.424, 0.769), 300.0, 0.38); // purple-pink, middle
    {
        vec2 dd = p - vec2(468.0, 652.0);
        dd.y *= 0.85; // slight downward stretch
        float q = length(dd)/330.0;
        col += vec3(0.376, 0.706, 1.0)*(0.34*exp(-2.2*q)); // blue, lower left
    }

    // M6: shape-hugging bloom
    col += vec3(1.000, 0.643, 0.251)*exp(-max(dStar, 0.0)/45.0);
    col += vec3(0.925, 0.424, 0.769)*exp(-max(dCirc, 0.0)/50.0);
    col += vec3(0.376, 0.706, 1.000)*exp(-max(dHeart, 0.0)/50.0);

    // M5: solid cores with core->edge gradient
    float tS = clamp(-dStar/40.0, 0.0, 1.0);
    vec3 cS = mix(vec3(1.000, 0.769, 0.251), vec3(1.000, 0.957, 0.784), tS*tS*(3.0 - 2.0*tS));
    float tC = clamp(-dCirc/78.0, 0.0, 1.0);
    vec3 cC = mix(vec3(1.000, 0.816, 0.894), vec3(1.000, 1.000, 1.000), tC*tC*(3.0 - 2.0*tC));
    float tH = clamp(-dHeart/60.0, 0.0, 1.0);
    vec3 cH = mix(vec3(0.588, 0.859, 1.000), vec3(0.839, 0.965, 1.000), tH*tH*(3.0 - 2.0*tH));

    // opaque coverage with ~2px soft edge
    col = mix(col, cS, 1.0 - smoothstep(-1.5, 1.5, dStar));
    col = mix(col, cC, 1.0 - smoothstep(-1.5, 1.5, dCirc));
    col = mix(col, cH, 1.0 - smoothstep(-1.5, 1.5, dHeart));

    // tiny dither against banding in the wide dark gradients
    float n = fract(sin(dot(fragCoord, vec2(12.9898, 78.233)))*43758.5453);
    col += (n - 0.5)*(1.0/255.0);

    fragColor = vec4(max(col, vec3(0.0)), 1.0);
}
