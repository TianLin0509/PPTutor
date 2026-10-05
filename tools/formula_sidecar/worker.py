"""Offline PP-FormulaNet_plus-M helper, isolated from the desktop app."""
import argparse,json,sys,time
from pathlib import Path

VERSION='1.0.0'
MODEL_NAME='pp_formulanet_plus_m.onnx'


def model_path():
    root=Path(getattr(sys,'_MEIPASS',Path(__file__).parent))
    return root/'models'/MODEL_NAME


def fitted_size(width,height):
    scale=384/max(width,height)
    return max(1,round(width*scale)),max(1,round(height*scale))


def prepare(image):
    """Upstream UniMERNet preprocessing, implemented without OpenCV."""
    import numpy as np
    from PIL import Image,ImageOps
    gray=np.asarray(image.convert('L'));low=int(gray.min());high=int(gray.max())
    if high-low<2:raise ValueError('选区没有清晰公式，请重新框选')
    normalized=(gray.astype(np.float32)-low)/(high-low)*255
    mask=Image.fromarray((normalized<200).astype('uint8')*255);box=mask.getbbox()
    if not box:raise ValueError('选区没有清晰公式')
    image=image.crop(box).convert('L')
    image=image.resize(fitted_size(*image.size),Image.Resampling.BILINEAR)
    dx=384-image.width;dy=384-image.height
    image=ImageOps.expand(image,(dx//2,dy//2,dx-dx//2,dy-dy//2),fill=0)
    pixels=(np.asarray(image,dtype=np.float32)/255-.7931)/.1738
    return pixels[None,None,:,:]


def build_engine():
    import socket,onnxruntime as ort
    from tokenizers import Tokenizer
    ort.disable_telemetry_events()
    def offline(*args,**kwargs):raise RuntimeError('公式识别只允许本机运行')
    socket.socket.connect=offline;socket.socket.connect_ex=offline
    path=model_path()
    if not path.is_file():raise ValueError('公式组件缺少模型，请重新安装')
    options=ort.SessionOptions();options.intra_op_num_threads=2;options.inter_op_num_threads=1
    session=ort.InferenceSession(str(path),options,providers=['CPUExecutionProvider'])
    dictionary=json.loads(session.get_modelmeta().custom_metadata_map['character'])
    tokenizer=Tokenizer.from_str(json.dumps(dictionary['fast_tokenizer_file']))
    def recognize(source):
        from PIL import Image
        started=time.monotonic()
        with Image.open(source) as image:batch=prepare(image)
        tokens=session.run(None,{session.get_inputs()[0].name:batch})[0][0].reshape(-1).tolist()
        if 2 in tokens:tokens=tokens[:tokens.index(2)+1]
        return tokenizer.decode(tokens,skip_special_tokens=True),time.monotonic()-started
    return recognize


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--request',required=True);parser.add_argument('--response',required=True)
    args=parser.parse_args();started=time.monotonic()
    try:
        request=json.loads(Path(args.request).read_text(encoding='utf-8'))
        source=Path(request['image'])
        from PIL import Image
        with Image.open(source) as image:
            if image.width*image.height>24000000:raise ValueError('截图过大，请只框选单个公式')
        engine=build_engine();latex,elapsed=engine(str(source))
        payload={'ok':True,'latex':latex,'inference_seconds':round(elapsed,3),'total_seconds':round(time.monotonic()-started,3),'version':VERSION}
    except Exception as exc:payload={'ok':False,'error':str(exc)[:400],'version':VERSION}
    Path(args.response).write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8')
    return 0


if __name__=='__main__':raise SystemExit(main())
