(() => {
  // 页面加载时执行一次: 指定参数白名单、范围和初值, 编译并启动预览.
  // 以后 Agent 的工具只需要调用 shaderPreview.setParameters({名称: 新值}).
  const defaults = {
    GLOW_STRENGTH: { min: 0, max: 3, initial: 1.2 },
    LINE_WIDTH: { min: 0.002, max: 0.08, initial: 0.015 },
  };
  // Python 在页面加载前注入配置; 直接双击打开时仍使用原演示.
  const config = window.shaderPreviewConfig || {
    glsl_code: document.querySelector("#shader").textContent,
    width: 512, height: 512, parameters: defaults,
  };
  const parameters = config.parameters;
  const labels = { GLOW_STRENGTH: "光晕强度", LINE_WIDTH: "线条宽度" };
  const status = document.querySelector("#status");
  window.addEventListener("shader-preview-error", event => { status.textContent = event.detail; });
  try {
    window.shaderPreview = window.createShaderPreview(config);
    const controls = document.querySelector("#controls");
    for (const [name, spec] of Object.entries(parameters)) {
      const label = document.createElement("label");
      label.htmlFor = name;
      label.textContent = labels[name] || name;
      const output = document.createElement("output");
      output.htmlFor = name;
      output.id = name + "-value";
      output.textContent = spec.initial;
      const slider = document.createElement("input");
      Object.assign(slider, { id: name, type: "range", min: spec.min, max: spec.max, step: "any", value: spec.initial });
      label.append(output);
      controls.append(label, slider);
      slider.addEventListener("input", () => {
        try {
          // 滑块只传数值; 不读取文件、不重编译、不启动第二个浏览器.
          window.shaderPreview.setParameters({ [name]: Number(slider.value) });
        } catch (error) { status.textContent = error.message; }
      });
    }
    // 所有成功更新都从会话事件读取完整快照, 外部直接调参也同步到控件.
    window.addEventListener("shader-preview-update", event => {
      for (const [name, value] of Object.entries(event.detail.parameters)) {
        document.getElementById(name).value = value;
        document.getElementById(name + "-value").textContent = value;
      }
      status.textContent = "预览已更新 · 版本 " + event.detail.revision;
    });
    document.querySelector("#reset").addEventListener("click", () => {
      try {
        const initial = Object.fromEntries(Object.entries(parameters).map(([name, spec]) => [name, spec.initial]));
        window.shaderPreview.setParameters(initial);
        status.textContent = "已恢复初值";
      } catch (error) { status.textContent = error.message; }
    });
    status.textContent = "预览已就绪 · 拖动滑块开始调参";
  } catch (error) {
    // Python 连接层读取同一份错误, 保留编译阶段和驱动日志.
    window.shaderPreviewError = { stage: error.stage || "init", log: error.message };
    status.textContent = "预览启动失败: " + error.message;
  }
})();
