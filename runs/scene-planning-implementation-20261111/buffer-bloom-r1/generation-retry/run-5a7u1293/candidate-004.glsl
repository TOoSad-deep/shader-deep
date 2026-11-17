// ---- shared helpers ----
float smin(float a, float b, float k){
    float h = clamp(0.5 + 0.5*(b - a)/k, 0.0, 1.0);
    return mix(b, a, h) - k*h*(1.0 - h);
}

// isosceles triangle SDF: apex at origin pointing screen-up, base at y = q.y (half width q.x)
float sdTriIso(vec2 p, vec2 q){
    p.x = abs(p.x);
    vec2 a = p - q*clamp(dot(p, q)/dot(q, q), 0.0, 1.0);
    vec2 b = p - q*vec2(clamp(p.x/q.x, 0.0, 1.0), 1.0);
    float s = -sign(q.y);
    vec2 d = min(vec2(dot(a, a), s*(p.x*q.y - p.y*q.x)),
                 vec2(dot(b, b), s*(p.y - q.y)));
    return -sqrt(d.x)*sign(d.y);
}

// five-point star SDF (near-exact): spike along screen-up, outer radius r,
// inner radius Ri, lean angle rotates the whole star (+ leans axis right).
// Angle-fold by 72 deg symmetry, mirror, then exact segment distance to the
// spike edge; inside/outside decided by the radial boundary radius t(phi).
float sdStar5(vec2 q, float r, float Ri, float lean){
    float a = atan(q.x, -q.y) - lean;            // angle from spike axis
    float sec = 1.2566370614;                    // 72 deg
    float m = mod(a + 0.5*sec, sec) - 0.5*sec;   // representative in [-36,36] deg
    float dl = m - a;
    float cd = cos(dl), sd = sin(dl);
    vec2 p = vec2(cd*q.x - sd*q.y, sd*q.x + cd*q.y); // fold into principal sector
    p.x = abs(p.x);
    float rad = max(length(p), 1e-6);
    float sp = p.x/rad, cp = -p.y/rad;           // sin/cos of sector angle phi
    float t = (r*Ri*0.5877852523)/
              max(Ri*0.5877852523*cp + (r - Ri*0.8090169944)*sp, 1e-6);
    vec2 T = vec2(0.0, -r);                      // tip (screen-up)
    vec2 V = vec2(Ri*0.5877852523, -Ri*0.8090169944); // inner vertex at +36 deg
    vec2 pa = p - T, ba = V - T;
    float h = clamp(dot(pa, ba)/dot(ba, ba), 0.0, 1.0);
    float seg = length(pa - ba*h);
    return (rad <= t) ? -seg : seg;
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
    // star (M3): tips reach r+8 = 74 after rounding, leaning ~9 deg
    float dStar = sdStar5(p - vec2(772.0, 100.0), 66.0, 33.0, -0.15708) - 8.0;

    // circle (M2)
    float dCirc = length(p - vec2(645.0, 356.0)) - 78.0;

    // inverted heart (M4): two lobes + upward triangular tip, all softened
    float dL1 = length(p - vec2(459.0, 648.0)) - 49.0;
    float dL2 = length(p - vec2(551.0, 648.0)) - 49.0;
    float dLob = smin(dL1, dL2, 10.0);
    float dTri = sdTriIso(p - vec2(504.0, 529.0), vec2(94.0, 115.0)) - 5.0;
    float dHeart = smin(dLob, dTri, 16.0);

    // ---- M8: near-black base + additive composition ----
    vec3 col = vec3(0.010, 0.010, 0.012);

    // M7: far-field directional fog (E1/F2, R2, R3)
    {
        vec2 dw = p - vec2(798.0, 95.0);             // warm brown, upper right
        dw.x *= (dw.x > 0.0) ? 0.82 : 1.55;          // spreads right, held back left
        col += fog(dw, vec3(1.000, 0.643, 0.251), 380.0, 0.52, 1.7);
    }
    {
        vec2 dp = p - vec2(645.0, 356.0);            // purple pink, middle
        col += fog(dp, vec3(0.925, 0.424, 0.769), 350.0, 0.50, 1.8);
    }
    {
        vec2 db = p - vec2(445.0, 682.0);            // blue, lower left
        db.y *= (db.y > 0.0) ? 0.82 : 1.40;          // spreads down, held back up
        col += fog(db, vec3(0.376, 0.706, 1.000), 450.0, 0.52, 1.55);
    }

    // M6: shape-hugging bloom
    col += vec3(1.000, 0.643, 0.251)*exp(-max(dStar, 0.0)/45.0);
    col += vec3(0.925, 0.424, 0.769)*exp(-max(dCirc, 0.0)/50.0);
    col += vec3(0.376, 0.706, 1.000)*exp(-max(dHeart, 0.0)/50.0);

    // M5: solid cores with core->edge gradient
    float tS = clamp(-dStar/35.0, 0.0, 1.0);
    vec3 cS = mix(vec3(1.000, 0.769, 0.251), vec3(1.000, 0.957, 0.784), tS*tS*(3.0 - 2.0*tS));
    float tC = clamp(-dCirc/22.0, 0.0, 1.0);
    vec3 cC = mix(vec3(1.000, 0.816, 0.894), vec3(1.000, 1.000, 1.000), tC*tC*(3.0 - 2.0*tC));
    float tH = clamp(-dHeart/80.0, 0.0, 1.0);
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
