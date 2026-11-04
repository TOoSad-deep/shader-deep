(() => {
    // 立即执行的函数为这一个页面建立画布与 WebGL2 上下文.
    // 实时预览页面提供自己的画布; 原单帧入口仍自动创建画布.
    const providedCanvas = document.querySelector("canvas[data-shader-preview]");
    const canvas = providedCanvas || document.createElement("canvas");
    if (!providedCanvas) document.body.replaceChildren(canvas);
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
        // 把失败阶段附在异常上; renderShader 的 catch 将其转换成 Python 能读取的对象.
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

    function setUniforms(program, uniforms) {
        // 查询链接后的真实类型, 避免用 uniform1f 给 int、向量或数组误赋值.
        const active = new Map();
        const count = gl.getProgramParameter(program, gl.ACTIVE_UNIFORMS);
        for (let index = 0; index < count; index++) {
            const info = gl.getActiveUniform(program, index);
            active.set(info.name, info);
        }
        for (const [name, value] of Object.entries(uniforms)) {
            const info = active.get(name);
            if (!info || info.type !== gl.FLOAT || info.size !== 1) {
                // 未使用的 uniform 可能被编译器优化掉, 此时也不能声称已成功调参.
                fail("uniform", "Expected an active scalar float uniform: " + name);
            }
            gl.uniform1f(gl.getUniformLocation(program, name), value);
        }
    }

    function draw(program, width, height, time, uniforms) {
        // 每个候选先清成透明色, 防止 discard 的像素沿用上一帧内容.
        // 关闭混合与抖动, 使写出的颜色尽量直接对应片段 Shader 的输出.
        gl.disable(gl.BLEND);
        gl.disable(gl.DITHER);
        gl.clearColor(0, 0, 0, 0);
        gl.clear(gl.COLOR_BUFFER_BIT);
        gl.useProgram(program);
        // uniform 是本次绘制共享的输入: 分辨率和固定时间.
        // Shader 未使用某个 uniform 时其位置可为 null, WebGL 会忽略对应赋值.
        gl.uniform3f(gl.getUniformLocation(program, "iResolution"), width, height, 1);
        gl.uniform1f(gl.getUniformLocation(program, "iTime"), time);
        // 自定义参数通过 WebGL API 赋值, 无需改动 GLSL 字符串.
        setUniforms(program, uniforms);
        gl.drawArrays(gl.TRIANGLES, 0, 3);
        const error = gl.getError();
        if (error !== gl.NO_ERROR || gl.isContextLost()) {
            fail("draw", "WebGL2 draw failed, error code: " + error);
        }
    }

    function exportPng() {
        // 直接从画布导出 PNG 数据, 不截取网页截图, 因而没有页面边距和缩放干扰.
        const png = canvas.toDataURL("image/png");
        if (!png.startsWith("data:image/png;base64,")) {
            fail("export", "Canvas did not produce a PNG");
        }
        return png;
    }

    window.createShaderPreview = ({ glsl_code, width, height, parameters, time = 0 }) => {
        // 一个会话只编译一次. program 留在此闭包中, 直到 dispose 统一释放.
        // parameters 由宿主页面提供, 不由后续调参调用扩大名称或范围.
        const specs = structuredClone(parameters);
        const values = {};
        const shaders = [];
        let program = null;
        let frame = null;
        let disposed = false;
        let paused = false;
        let error = null;
        let revision = 0;
        let renderedTime = 0;
        const started = performance.now();

        function validate(updates) {
            for (const [name, value] of Object.entries(updates)) {
                if (!Object.hasOwn(specs, name) || name === "iTime" || name === "iResolution") {
                    fail("uniform", "Parameter is not allowed: " + name);
                }
                const spec = specs[name];
                if (!Number.isFinite(value) || value < spec.min || value > spec.max) {
                    fail("uniform", "Parameter is outside its range: " + name);
                }
            }
        }

        function dispose() {
            // 页面退出或宿主显式关闭时, 同时停止动画和释放 GPU 资源.
            if (disposed) return;
            disposed = true;
            cancelAnimationFrame(frame);
            gl.useProgram(null);
            if (program) gl.deleteProgram(program);
            shaders.forEach(shader => gl.deleteShader(shader));
            window.removeEventListener("pagehide", hide);
            window.removeEventListener("pageshow", show);
        }

        function hide(event) {
            // 往返缓存会保留这个页面和闭包, 此时只暂停, 不永久销毁 program.
            if (!event.persisted) return dispose();
            paused = true;
            cancelAnimationFrame(frame);
            frame = null;
        }

        function show(event) {
            // 只恢复曾暂停的会话; 重复 pageshow 不额外创建动画循环.
            if (!event.persisted || !paused || disposed) return;
            paused = false;
            tick();
        }

        function render() {
            if (disposed) fail("preview", "Preview is closed");
            if (error) throw error;
            if (gl.isContextLost()) fail("context", "WebGL2 context was lost");
            // time=null 表示动画时间; 数值则固定时间, 便于之后比较参数效果.
            const seconds = time === null ? (performance.now() - started) / 1000 : time;
            draw(program, width, height, seconds, values);
            renderedTime = seconds;
        }

        function tick() {
            if (paused || disposed) return;
            try {
                render();
                frame = requestAnimationFrame(tick);
            } catch (cause) {
                error = cause;
                window.dispatchEvent(new CustomEvent("shader-preview-error", { detail: cause.message }));
                dispose();
            }
        }

        try {
            for (const [name, spec] of Object.entries(specs)) {
                if (![spec.min, spec.max, spec.initial].every(Number.isFinite) || spec.min > spec.max) {
                    fail("uniform", "Invalid parameter configuration: " + name);
                }
                values[name] = spec.initial;
            }
            validate(values);
            resize(width, height);
            program = gl.createProgram();
            if (!program) fail("link", "Could not allocate program");
            prepareProgram(program, glsl_code, shaders);
            render();
            frame = requestAnimationFrame(tick);
            window.addEventListener("pagehide", hide);
            window.addEventListener("pageshow", show);
        } catch (cause) {
            dispose();
            throw cause;
        }

        return {
            setParameters(updates) {
                // 先检查整组再更新, 避免一部分参数已更新后才发现另一项无效.
                validate(updates);
                render(); // 先确认会话仍可用, 再修改保存的参数.
                Object.assign(values, updates);
                revision += 1;
                render();
                const result = { revision, parameters: { ...values } };
                // 统一通知页面: 滑块、重置按钮和后续外部工具调用共用一个显示入口.
                window.dispatchEvent(new CustomEvent("shader-preview-update", { detail: result }));
                return result;
            },
            capture() {
                // 在同一 JS 调用内绘制并导出, 返回图片与参数版本的一致快照.
                render();
                return { revision, parameters: { ...values }, time: renderedTime, png: exportPng() };
            },
            dispose,
        };
    };

    window.renderShader = ({ glsl_code, width, height, time, uniforms = {} }) => {
        // 每次调用拥有独立 program/shader, 画布与上下文跨调用复用.
        // 成功与失败都返回可序列化对象, 由 Python 再统一转换成字节或 RenderError.
        const shaders = [];
        let program = null;
        try {
            if (gl.isContextLost()) {
                fail("context", "WebGL2 context was lost");
            }
            resize(width, height);
            program = gl.createProgram();
            if (!program) {
                fail("link", "Could not allocate program");
            }
            prepareProgram(program, glsl_code, shaders);
            draw(program, width, height, time, uniforms);
            return { ok: true, png: exportPng() };
        } catch (error) {
            return { ok: false, stage: error.stage || "render", message: error.message || String(error) };
        } finally {
            // 先解除当前 program 绑定, 再删除 program 和两段 Shader.
            // finally 在成功导出或任何失败分支都会执行, 避免候选迭代积累 GPU 对象.
            gl.useProgram(null);
            if (program) {
                gl.deleteProgram(program);
            }
            shaders.forEach(shader => gl.deleteShader(shader));
        }
    };
})();
