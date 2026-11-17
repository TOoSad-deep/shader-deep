float smin(float a, float b, float k){
    float h = clamp(0.5 + 0.5*(b - a)/k, 0.0, 1.0);
    return mix(b, a, h) - k*h*(1.0 - h);
}

// isosceles triangle SDF: apex at origin, base at y = q.y (half width q.x)
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
vec3 fog(vec2 d, vec3 col, float rF, float a, float k){
    float q = length(d)/rF;
    return col*(a*exp(-k*q));
}

void mainImage(out vec4 fragColor, in vec2 fragCoord){
    // M1: logical pixels, origin top-left, height 720
    float S = 720.0/iResolution.y;
    vec2 p = vec2(fragCoord.x, iResolution.y - fragCoord.y)*S;

    // ---- star (M3): pentagon core + five spikes, min-union, rounded by 8 ----
    float dStar;
    {
        vec2 w = p - vec2(774.0, 103.0);
        float r   = 72.0;                  // tip radius
        float Ri  = 34.0;                  // inner-vertex radius
        float ap  = Ri*0.809016994;        // pentagon apothem
        float wsp = Ri*0.587785252;        // spike base half width
        float hsp = r - Ri*0.809016994;    // spike height
        float dPent = -1e9;
        float dSp   =  1e9;
        for (int k = 0; k < 5; k++){
            float th = -0.15708 + float(k)*1.2566370614;   // lean ~9 deg left
            vec2 dir = vec2(sin(th), -cos(th));            // outward, screen up = -y
            dPent = max(dPent, dot(w, dir) - ap);
            vec2 lw = w - dir*r;                           // relative to tip
            vec2 q = vec2(dot(lw, vec2(cos(th), sin(th))), dot(lw, -dir));
            dSp = min(dSp, sdTriIso(q, vec2(wsp, hsp)));
        }
        dStar = min(dPent, dSp) - 8.0;
    }

    // ---- circle (M2) ----
    float dCirc = length(p - vec2(645.0, 356.0)) - 78.0;

    // ---- inverted heart (M4) ----
    float dL1 = length(p - vec2(458.0, 647.0)) - 50.0;
    float dL2 = length(p - vec2(552.0, 647.0)) - 50.0;
    float dLob = smin(dL1, dL2, 10.0);
    float dTri = sdTriIso(p - vec2(504.0, 529.0), vec2(94.0, 115.0)) - 5.0;
    float dHeart = smin(dLob, dTri, 16.0);

    // ---- M8: near-black base ----
    vec3 col = vec3(0.010, 0.010, 0.012);

    // M7: far-field fog (smooth directional warps, no seams)
    {
        vec2 dw = p - vec2(800.0, 92.0);               // warm brown, upper right
        dw.x *= mix(1.50, 0.85, smoothstep(-60.0, 60.0, dw.x));
        col += fog(dw, vec3(1.000, 0.643, 0.251), 380.0, 0.52, 1.7);
    }
    {
        vec2 dp = p - vec2(645.0, 356.0);              // purple pink, middle
        col += fog(dp, vec3(0.925, 0.424, 0.769), 350.0, 0.50, 1.8);
    }
    {
        vec2 db = p - vec2(447.0, 680.0);              // blue, lower left
        db.y *= mix(1.45, 0.80, smoothstep(-60.0, 60.0, db.y));
        db.x *= mix(1.20, 0.95, smoothstep(-60.0, 60.0, db.x));
        col += fog(db, vec3(0.376, 0.706, 1.000), 440.0, 0.52, 1.55);
    }

    // M6: shape-hugging bloom
    col += vec3(1.000, 0.643, 0.251)*exp(-max(dStar, 0.0)/45.0);
    col += vec3(0.925, 0.424, 0.769)*exp(-max(dCirc, 0.0)/50.0);
    col += vec3(0.376, 0.706, 1.000)*exp(-max(dHeart, 0.0)/50.0);

    // M5: solid cores with core->edge gradient
    float tS = clamp(-dStar/35.0, 0.0, 1.0);
    vec3 cS = mix(vec3(1.000, 0.769, 0.251), vec3(1.000, 0.957, 0.784), tS*tS*(3.0 - 2.0*tS));
    float tC = clamp(-dCirc/20.0, 0.0, 1.0);
    vec3 cC = mix(vec3(1.000, 0.816, 0.894), vec3(1.000, 1.000, 1.000), tC*tC*(3.0 - 2.0*tC));
    float tH = clamp(-dHeart/150.0, 0.0, 1.0);
    vec3 cH = mix(vec3(0.588, 0.859, 1.000), vec3(0.839, 0.965, 1.000), tH);

    // opaque coverage with ~3px soft edge
    col = mix(col, cS, 1.0 - smoothstep(-1.5, 1.5, dStar));
    col = mix(col, cC, 1.0 - smoothstep(-1.5, 1.5, dCirc));
    col = mix(col, cH, 1.0 - smoothstep(-1.5, 1.5, dHeart));

    // tiny dither against banding in the wide dark gradients
    float n = fract(sin(dot(fragCoord, vec2(12.9898, 78.233)))*43758.5453);
    col += (n - 0.5)*(1.0/255.0);

    fragColor = vec4(max(col, vec3(0.0)), 1.0);
}
