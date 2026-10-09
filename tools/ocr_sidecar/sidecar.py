"""PPT Doctor 的 OCR 侧车：读一个 JSON 请求，写一个 JSON 响应，然后退出。

单独打包成 pptdoctor-ocr.exe，随「识别组件」按需下载。主程序不 import 这里的
任何东西，也不需要 onnxruntime / numpy / opencv。

协议：
    sidecar --request req.json --response resp.json
    req  = {"images": ["C:/a.png", ...]}
    resp = {"ok": true, "results": {"C:/a.png": [{"text","box":[x0,y0,x1,y1],"score"}]}}
失败时 resp = {"ok": false, "error": "..."}，退出码非 0。
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
import time
import os

__version__ = "1.1.0"


def build_engine():
    from rapidocr_onnxruntime import RapidOCR
    # Large default thread pools cost more than they save on short screenshots.
    # These are execution settings; all three original OCR models stay enabled.
    return RapidOCR(intra_op_num_threads=min(4, os.cpu_count() or 1), inter_op_num_threads=1)


def serve():
    engine = None
    for line in sys.stdin:
        request = {}
        try:
            request = json.loads(line)
            start = time.perf_counter()
            if engine is None:
                engine = build_engine()
            images = [str(p) for p in request['images']]
            payload = {'ok': True, 'results': {p: recognize(engine, p) for p in images},
                       'seconds': time.perf_counter() - start, 'version': __version__}
        except Exception as exc:
            payload = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
        payload['id'] = request.get('id') if isinstance(request, dict) else None
        print('PPTDOCTOR_OCR:' + json.dumps(payload, ensure_ascii=False), flush=True)
    return 0


def recognize(engine, path: str) -> list[dict]:
    out = engine(path)
    rows = out[0] if isinstance(out, tuple) else out
    result = []
    for item in rows or []:
        quad, text, score = item[0], item[1], float(item[2])
        xs = [float(p[0]) for p in quad]
        ys = [float(p[1]) for p in quad]
        result.append({
            "text": str(text),
            "box": [min(xs), min(ys), max(xs), max(ys)],
            "score": score,
        })
    # 阅读顺序：先按行带分组，再按左边界。让下游拿到的顺序和人看的一致。
    if result:
        heights = sorted(r["box"][3] - r["box"][1] for r in result)
        band = max(6.0, heights[len(heights) // 2] * 0.6)
        result.sort(key=lambda r: (round(r["box"][1] / band), r["box"][0]))
    return result


def main(argv=None) -> int:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(prog="pptdoctor-ocr")
    parser.add_argument("--request")
    parser.add_argument("--response")
    parser.add_argument('--serve', action='store_true')
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    if args.serve:
        return serve()
    if not args.request or not args.response:
        parser.error('--request and --response are required without --serve')

    try:
        with open(args.request, encoding="utf-8") as f:
            request = json.load(f)
        images = [str(p) for p in (request.get("images") or [])]
        engine = build_engine()
        results = {path: recognize(engine, path) for path in images}
        payload = {"ok": True, "version": __version__, "results": results}
        code = 0
    except Exception as exc:  # noqa: BLE001 侧车的唯一职责就是把失败原因带回去
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                   "trace": traceback.format_exc()[-2000:]}
        code = 1
    with open(args.response, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return code


if __name__ == "__main__":
    sys.exit(main())
