"""Build a separate frozen formula runtime and its verified install archive."""
import argparse,hashlib,importlib.util,shutil,subprocess,sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--python',required=True);parser.add_argument('--models',required=True)
    args=parser.parse_args();work=ROOT/'artifacts/formula-build';work.mkdir(parents=True,exist_ok=True)
    model=Path(args.models).resolve()/'pp_formulanet_plus_m.onnx'
    with model.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
    if digest!='71b6d389cf7b857e45252a4b98cfced1a3ffca7bf24d9497d02d052a41d9493b':
        raise ValueError('公式模型与上游校验值不符')
    command=[args.python,'-m','PyInstaller','--noconfirm','--name','pptdoctor-formula','--console',
             '--distpath',str(work/'dist'),'--workpath',str(work/'work'),'--specpath',str(work),
             '--collect-binaries','onnxruntime','--collect-all','tokenizers',
             '--exclude-module','matplotlib','--exclude-module','torch','--exclude-module','cv2',
             '--exclude-module','rapid_latex_ocr',
             '--add-data',str(model)+';models',str(Path(__file__).with_name('worker.py'))]
    subprocess.run(command,check=True)
    source=work/'dist/pptdoctor-formula'
    shutil.copytree(Path(__file__).parent/'licenses',source/'licenses',dirs_exist_ok=True)
    shutil.copyfile(Path(__file__).parent/'README.md',source/'NOTICE.md')
    # Pull the exact installed dependency licenses from the build interpreter.
    license_script='''import importlib.metadata as m, pathlib, shutil, sys
out=pathlib.Path(sys.argv[1])
for name in ['numpy','onnxruntime','Pillow','tokenizers','flatbuffers','coloredlogs','humanfriendly','packaging','protobuf']:
 try:d=m.distribution(name)
 except m.PackageNotFoundError:
  if name in ['numpy','onnxruntime','Pillow','tokenizers']:raise
  continue
 for f in d.files or []:
  if any(part.lower().startswith(('license','copying','notice','authors')) for part in f.parts):
   src=d.locate_file(f)
   if src.is_file() and src.stat().st_size<2000000:
    dst=out/name/str(f).replace('../','');dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src,dst)
'''
    subprocess.run([args.python,'-c',license_script,str(source/'licenses')],check=True)
    # The optional helper does not use OpenCV video codecs.
    for extra in (source/'_internal/cv2').glob('opencv_videoio_ffmpeg*.dll'):extra.unlink()
    spec=importlib.util.spec_from_file_location('ocr_component_builder',ROOT/'tools/ocr_sidecar/build.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.publish(source,ROOT/'artifacts/formula-component','1.0.0')
    print(str(ROOT/'artifacts/formula-component'))


if __name__=='__main__':main()
