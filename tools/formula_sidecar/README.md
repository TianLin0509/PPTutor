# 本机公式识别组件 v1.0.0

截图不会上传。CPU 推理使用 PP-FormulaNet_plus-M 的 ONNX 模型，最多 2 个运算线程。
模型由 RapidAI 发布，包含 tokenizer 字典；构建前校验 SHA256。

模型地址：
https://www.modelscope.cn/models/RapidAI/RapidDoc/resolve/v1.0.0/formula/PP-FormulaNet_plus-M/pp_formulanet_plus_m.onnx

SHA256：71b6d389cf7b857e45252a4b98cfced1a3ffca7bf24d9497d02d052a41d9493b

先用独立 venv 安装 requirements.txt，再执行：

```powershell
uv run python tools/formula_sidecar/build.py --python <venv>/Scripts/python.exe --models <models目录>
```

产物在 artifacts/formula-component。component.json 包含压缩包与逐文件校验值。
发布到 GitHub 独立 tag `formula-v1.0.0`，不要将该 tag 设为应用最新版本。
完整安装包增加 `tools/build_installer.py --formula-component artifacts/formula-component`。
首次点击识别公式后安装内置组件；绿色包可按需从 GitHub 下载。

识别结果需要对照原图核对。低清晰度、手写、多公式选区可能产生误识别；
不能用测试样例的读对率推断用户截图的正确率。未知 Word 结构会明确禁用该复制按钮，
LaTeX 可以编辑后重新预览。预览使用系统浏览器的临时独立 profile，无需联网。

算法及模型来源：PaddleOCR (Apache-2.0)；ONNX 转换与预处理参考 RapidDoc (Apache-2.0)。
https://github.com/PaddlePaddle/PaddleOCR
https://github.com/RapidAI/RapidDoc
对应许可证位于 licenses/；组件使用的第三方依赖许可证随冻结运行库分发。
