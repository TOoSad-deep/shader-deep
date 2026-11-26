(() => {
    // 立即执行的函数为这一个页面建立画布与 WebGL2 上下文.
    // canvas/gl 留在闭包内供多次调用复用, 只公开 window.shaderRenderer 作为 Python 入口.
    const canvas = document.createElement("canvas");
    document.body.replaceChildren(canvas);
    const gl = canvas.getContext("webgl2", {
        // 保留 Shader 输出的透明度, 不使用深度/模板缓冲或几何抗锯齿.
        alpha: true,
        premultipliedAlpha: false,
        antialias: false,
        depth: false,
        stencil: false,
        // 绘制结果需保留到后续 toDataURL 读取, 供 PNG 导出使用.
        preserveDrawingBuffer: true,
    });
    if (!gl) {
        throw new Error("WebGL2 is not available in this browser");
    }

    // 用 gl_VertexID 为三个顶点生成一个覆盖画布的大三角形, 无需传入顶点缓冲.
    // 裁剪空间坐标为 (-1,-1)、(3,-1)、(-1,3); 每个覆盖像素再执行片段 Shader.
    const vertexSource = `#version 300 es
void main() {
    vec2 position = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
    gl_Position = vec4(position * 2.0 - 1.0, 0.0, 1.0);
}
`;

    function fail(stage, message) {
        // 把失败阶段附在异常上, 由浏览器入口转换成 Python 能读取的对象.
        const error = new Error(message);
        error.stage = stage;
        throw error;
    }

    function compile(type, source, stage, shaders) {
        const shader = gl.createShader(type);
        if (!shader) {
            fail(stage, "Could not allocate shader");
        }
        // 分配成功立刻登记, 即使随后编译失败, finally 也能找到并释放这个对象.
        shaders.push(shader);
        gl.shaderSource(shader, source);
        gl.compileShader(shader);
        if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
            // 原样保留驱动的编译日志, 包含符号与行号, 让模型能针对实际错误修复.
            fail(stage, gl.getShaderInfoLog(shader) || "Shader compilation failed");
        }
        return shader;
    }

    function prepareProgram(program, code, shaders) {
        // 将模型的 mainImage 代码接入 GLSL ES 3.00 的标准入口.
        // #line 1 让用户代码起始行重新从 1 计数, 减少包装代码对错误行号的影响.
        // iResolution/iTime/main 在这里提供, 调用方无需重复声明.
        const fragmentSource = `#version 300 es
precision highp float;
precision highp int;
uniform vec3 iResolution;
uniform float iTime;
out vec4 rendererFragColor;
#line 1
${code}
void main() {
    mainImage(rendererFragColor, gl_FragCoord.xy);
}
`;
        gl.attachShader(program, compile(gl.VERTEX_SHADER, vertexSource, "vertex_compile", shaders));
        gl.attachShader(program, compile(gl.FRAGMENT_SHADER, fragmentSource, "fragment_compile", shaders));
        // 两段 Shader 各自编译成功还不够, 链接要继续检查阶段之间的接口是否匹配.
        gl.linkProgram(program);
        if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
            fail("link", gl.getProgramInfoLog(program) || "Program linking failed");
        }
    }

    function resize(width, height) {
        // 检查真实 WebGL 限制和最终缓冲尺寸; 不静默缩小图片, 以免改变比较条件.
        const viewport = gl.getParameter(gl.MAX_VIEWPORT_DIMS);
        const buffer = gl.getParameter(gl.MAX_RENDERBUFFER_SIZE);
        if (width > Math.min(viewport[0], buffer) || height > Math.min(viewport[1], buffer)) {
            fail("size", "Requested dimensions exceed WebGL2 limits");
        }
        canvas.width = width;
        canvas.height = height;
        if (gl.drawingBufferWidth !== width || gl.drawingBufferHeight !== height) {
            fail("size", "Browser could not allocate the requested drawing buffer");
        }
        gl.viewport(0, 0, width, height);
    }

    const uniformTypes = [gl.FLOAT, gl.FLOAT_VEC2, gl.FLOAT_VEC3, gl.FLOAT_VEC4];
    const uniformSetters = [gl.uniform1fv, gl.uniform2fv, gl.uniform3fv, gl.uniform4fv];

    function bindUniforms(program, uniforms) {
        const locations = new Map();
        if (!uniforms.length) return locations;

        const active = new Map();
        const count = gl.getProgramParameter(program, gl.ACTIVE_UNIFORMS);
        for (let index = 0; index < count; index++) {
            const info = gl.getActiveUniform(program, index);
            if (info) active.set(info.name, info);
        }
        for (const [name, components] of uniforms) {
            const info = active.get(name);
            const location = gl.getUniformLocation(program, name);
            if (!info || location === null) {
                fail("uniforms", `Uniform '${name}' is absent or inactive in the linked program`);
            }
            if (info.size !== 1 || info.type !== uniformTypes[components.length - 1]) {
                fail("uniforms", `Uniform '${name}' does not match the supplied float or vector type`);
            }
            locations.set(name, location);
        }
        return locations;
    }

    function draw(state, uniforms) {
        const { program, resolution, timeLocation, locations, width, height, time } = state;
        // 每个候选先清成透明色, 防止 discard 的像素沿用上一帧内容.
        // 关闭混合与抖动, 使写出的颜色尽量直接对应片段 Shader 的输出.
        gl.disable(gl.BLEND);
        gl.disable(gl.DITHER);
        gl.clearColor(0, 0, 0, 0);
        gl.clear(gl.COLOR_BUFFER_BIT);
        gl.useProgram(program);
        // uniform 是本次绘制共享的输入: 分辨率和固定时间.
        // Shader 未使用某个 uniform 时其位置可为 null, WebGL 会忽略对应赋值.
        gl.uniform3f(resolution, width, height, 1);
        gl.uniform1f(timeLocation, time);
        for (const [name, components] of uniforms) {
            uniformSetters[components.length - 1].call(gl, locations.get(name), components);
        }
        gl.drawArrays(gl.TRIANGLES, 0, 3);
        checkGlError("draw");
    }

    function checkGlError(stage) {
        const codes = [];
        for (let code = gl.getError(); code !== gl.NO_ERROR; code = gl.getError()) {
            codes.push(code);
        }
        if (codes.length || gl.isContextLost()) {
            fail(stage, `WebGL2 ${stage} failed, error codes: ${codes.join(", ") || "context lost"}`);
        }
    }

    function exportPng() {
        // 直接从画布导出 PNG 数据, 不截取网页截图, 因而没有页面边距和缩放干扰.
        const png = canvas.toDataURL("image/png");
        if (!png.startsWith("data:image/png;base64,")) {
            fail("export", "Canvas did not produce a PNG");
        }
        return png.slice("data:image/png;base64,".length);
    }

    function readRegion([left, top, right, bottom], canvasHeight) {
        const width = right - left;
        const height = bottom - top;
        const pixels = new Uint8Array(width * height * 4);
        // WebGL 原点在左下角, 转换区域坐标后逐行反转, 输出与 PNG 区域坐标一致.
        gl.readPixels(left, canvasHeight - bottom, width, height, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
        checkGlError("read_pixels");
        const stride = width * 4;
        const topDown = new Uint8Array(pixels.length);
        for (let row = 0; row < height; row++) {
            const start = (height - row - 1) * stride;
            topDown.set(pixels.subarray(start, start + stride), row * stride);
        }
        // 只做跨进程传输编码, 不在试验路径调用画布 PNG 编码器.
        let binary = "";
        const chunk = 32768;
        for (let start = 0; start < topDown.length; start += chunk) {
            binary += String.fromCharCode(...topDown.subarray(start, start + chunk));
        }
        return btoa(binary);
    }

    function release(program) {
        gl.useProgram(null);
        gl.deleteProgram(program);
    }

    function start({ code, width, height, time, uniforms }) {
        if (gl.isContextLost()) fail("context", "WebGL2 context was lost");
        resize(width, height);
        const program = gl.createProgram();
        if (!program) fail("link", "Could not allocate program");
        const shaders = [];
        try {
            prepareProgram(program, code, shaders);
            for (const shader of shaders) gl.detachShader(program, shader);
            const locations = bindUniforms(program, uniforms);
            return {
                program, width, height, time, locations,
                resolution: gl.getUniformLocation(program, "iResolution"),
                timeLocation: gl.getUniformLocation(program, "iTime"),
            };
        } catch (error) {
            release(program);
            throw error;
        } finally {
            shaders.forEach(shader => gl.deleteShader(shader));
        }
    }

    let current = null;

    function operate(operation, args) {
        if (operation === "start" || operation === "render") {
            if (current) fail("session", "Renderer already has an active optimization session");
            const state = start(args);
            if (operation === "start") {
                current = state;
                return null;
            }
            try {
                draw(state, args.uniforms);
                return exportPng();
            } finally {
                release(state.program);
            }
        }
        if (!current) fail("session", "No active optimization session");
        if (operation === "end") {
            const { program } = current;
            current = null;
            release(program);
            return null;
        }
        if (operation === "png") {
            draw(current, args);
            return exportPng();
        }
        if (operation === "read") {
            draw(current, args.uniforms);
            return args.regions.map(region => readRegion(region, current.height));
        }
        fail("session", "Unknown renderer operation");
    }

    window.shaderRenderer = (operation, args) => {
        try {
            return { ok: true, value: operate(operation, args) };
        } catch (error) {
            return { ok: false, stage: error.stage || "render", message: error.message || String(error) };
        }
    };
})();
