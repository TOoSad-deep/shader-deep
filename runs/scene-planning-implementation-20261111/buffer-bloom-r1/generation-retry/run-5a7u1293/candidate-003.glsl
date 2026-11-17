// ---- shared helpers ----
float smin(float a, float b, float k){
    float h = clamp(0.5 + 0.5*(b - a)/k, 0.0, 1.0);
    return mix(b, a, h) - k*h*(1.0 - h);
}

// isosceles triangle SDF: apex at origin pointing +y, base at y = q.y (half width q.x)
float sdTriIso(vec2 p, vec2 q){
    p.x = abs(p.x);
    vec2 a = p - q*clamp(dot(p, q)/dot(q, q), 0.0, 1.0);
    vec2 b = p - q*vec2(clamp(p.x/q.x, 0.0, 1.0), 1.0);
    float s = -sign(q.y);
    vec2 d = min(vec2(dot(a, a), s*(p.x*q.y - p.y*q.x)),
                 vec2(dot(b, b), s*(p.y - q.y)));
    return -sqrt(d.x)*sign(d.y);
}

// five-pointed star SDF (polar construction):
// one point along screen-up, outer radius r, inner radius rf*r,
// lean angle in radians (+ leans right). Approximate signed distance.
float sdStar5(vec2 q, float r, float rf, float lean){
    float Ri = r*rf;
    float a = atan(q.x, -q.y) - lean;               // angle from nearest spike axis
    float sec = 1.2566370614;                        // 72 deg
    float m = mod(a, sec);
    float phi = min(m, sec - m);                     // 0..36 deg
    float cp = cos(phi), sp = sin(phi);
    // radius at which the spike edge line crosses the ray at angle phi
    float t = (r*Ri*0.5877852523) /
              (Ri*0.5877852523*cp + (r - Ri*0.8090169944)*sp);
    return length(q) - t;
}

// far-field soft glow fog (caller may pre-warp the offset for directionality)
vec3 fog(vec2 d, vec3 col, float rF, float a, float k){
    float q = length(d)/rF;
    return col*(a*exp(-k*q));
}

void mainImage(out vec4 fragColor, in vec2 fragCoord){
    // M1: logical pixel coords, origin top-left, height = 720
    float S = 720.0/iResolution.y;
    vec2 p = vec2(fragCoord.x, iResolution.y - fragCoord.y)*S;

    // ---- shared SDFs ----
    // star (M3): point up, leaning ~9 deg left, tips rounded by 8
    float dStar = sdStar5(p - vec2(772.0, 102.0), 76.0, 0.5, -0.15708) - 8.0;

    // circle (M2)
    float dCirc = length(p - vec2(645.0, 356.0)) - 78.0;

    // inverted heart (M4): two lobes + upward triangular tip, all softened
    float dL1 = length(p - vec2(456.0, 647.0)) - 50.0;
    float dL2 = length(p - vec2(550.0, 647.0)) - 50.0;
    float dLob = smin(dL1, dL2, 8.0);
    float dTri = sdTriIso(p - vec2(503.0, 526.0), vec2(98.0, 114.0)) - 6.0;
    float dHeart = smin(dLob, dTri, 14.0);

    // ---- M8: near-black base + additive composition ----
    vec3 col = vec3(0.010, 0.010, 0.012);

    // M7: far-field directional fog (E1/F2, R2, R3)
    {
        vec2 dw = p - vec2(800.0, 95.0);             // warm brown, upper right
        dw.x *= (dw.x > 0.0) ? 0.85 : 1.35;          // spreads right, held back left
        col += fog(dw, vec3(1.000, 0.643, 0.251), 400.0, 0.55, 1.6);
    }
    {
        vec2 dp = p - vec2(645.0, 356.0);            // purple pink, middle
        col += fog(dp, vec3(0.925, 0.424, 0.769), 340.0, 0.45, 1.8);
    }
    {
        vec2 db = p - vec2(445.0, 680.0);            // blue, lower left
        db.y *= (db.y > 0.0) ? 0.80 : 1.35;          // spreads down, held back up
        col += fog(db, vec3(0.376, 0.706, 1.000), 460.0, 0.55, 1.5);
    }

    // M6: shape-hugging bloom
    col += vec3(1.000, 0.643, 0.251)*exp(-max(dStar, 0.0)/45.0);
    col += vec3(0.925, 0.424, 0.769)*exp(-max(dCirc, 0.0)/50.0);
    col += vec3(0.376, 0.706, 1.000)*exp(-max(dHeart, 0.0)/50.0);

    // M5: solid cores with core->edge gradient
    float tS = clamp(-dStar/30.0, 0.0, 1.0);
    vec3 cS = mix(vec3(1.000, 0.769, 0.251), vec3(1.000, 0.957, 0.784), tS*tS*(3.0 - 2.0*tS));
    float tC = clamp(-dCirc/16.0, 0.0, 1.0);
    vec3 cC = mix(vec3(1.000, 0.816, 0.894), vec3(1.000, 1.000, 1.000), tC*tC*(3.0 - 2.0*tC));
    float tH = clamp(-dHeart/85.0, 0.0, 1.0);
    vec3 cH = mix(vec3(0.647, 0.871, 1.000), vec3(0.816, 0.949, 1.000), tH*tH*(3.0 - 2.0*tH));

    // opaque coverage with ~3px soft edge
    col = mix(col, cS, 1.0 - smoothstep(-1.5, 1.5, dStar));
    col = mix(col, cC, 1.0 - smoothstep(-1.5, 1.5, dCirc));
    col = mix(col, cH, 1.0 - smoothstep(-1.5, 1.5, dHeart));

    // tiny dither against banding in the wide dark gradients
    float n = fract(sin(dot(fragCoord, vec2(12.9898, 78.233)))*43758.5453);
    col += (n - 0.5)*(1.0/255.0);

    fragColor = vec4(max(col, vec3(0.0)), 1.0);
}
