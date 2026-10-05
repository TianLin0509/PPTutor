"""A separate, on-demand local formula runtime. Screenshots never leave the PC."""
import json,os,shutil,urllib.request,uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from ..config import data_dir
from .encoding import encode,MAX_PIXELS,CaptureCancelled
from .formula_format import clean_latex
from .formula_process import run

RELEASE_BASE='https://github.com/TianLin0509/PPTutor/releases/download/formula-v1.0.0'


def component_dir():return data_dir()/'formula'


def bundled_dir():
    import sys
    folder=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parents[3]))/'formula'
    return folder if (folder/'component.json').is_file() and (folder/'component.zip').is_file() else None


def command():
    override=os.environ.get('PPTUTOR_FORMULA_CMD')
    if override:
        value=json.loads(override)
        if not isinstance(value,list) or not value or any(not isinstance(x,str) or not x for x in value):
            raise ValueError('公式组件命令配置无效')
        return value
    exe=component_dir()/'pptdoctor-formula.exe'
    pointer=component_dir()/'active.json'
    if pointer.is_file():
        record=json.loads(pointer.read_text(encoding='utf-8'))
        name=record.get('runtime','') if isinstance(record,dict) else None
        if not isinstance(name,str) or not name.startswith('runtime-') or Path(name).name!=name or '/' in name or '\\' in name:
            raise ValueError('公式组件安装记录无效')
        exe=component_dir()/name/'pptdoctor-formula.exe'
    return [str(exe)] if exe.is_file() else None


def is_installed():
    try:return bool(command())
    except (ValueError,TypeError,OSError):return False


def fetch_manifest():
    with urllib.request.urlopen(RELEASE_BASE+'/component.json',timeout=30) as response:
        payload=json.loads(response.read(2*1024*1024).decode('utf-8'))
    payload['_base']=RELEASE_BASE
    return payload


def install(*,progress=None,cancelled=None,manifest=None,archive_path=None):
    from .. import imgtext_ocr
    if cancelled is not None and cancelled.is_set():raise CaptureCancelled()
    bundled=bundled_dir() if manifest is None else None
    if bundled:
        payload=json.loads((bundled/'component.json').read_text(encoding='utf-8'));archive_path=bundled/'component.zip'
    else:payload=manifest or fetch_manifest()
    if 'pptdoctor-formula.exe' not in payload.get('files',{}):raise ValueError('不是公式识别组件')
    root=component_dir();root.mkdir(parents=True,exist_ok=True)
    name='runtime-'+uuid.uuid4().hex;destination=root/name
    # Install into a fresh runtime first. A partial copy can never replace
    # the previous working runtime; only this tiny pointer is switched.
    pointer=root/('active-'+uuid.uuid4().hex+'.json')
    try:
        version=imgtext_ocr.install(payload,progress=progress,cancel=(cancelled.is_set if cancelled else None),
                                   archive_path=archive_path,target_dir=destination)
        if cancelled is not None and cancelled.is_set():raise CaptureCancelled()
        if not (destination/'pptdoctor-formula.exe').is_file():raise ValueError('公式组件安装不完整')
        pointer.write_text(json.dumps({'runtime':name,'version':version}),encoding='utf-8')
        if cancelled is not None and cancelled.is_set():raise CaptureCancelled()
        os.replace(pointer,root/'active.json')
    except Exception:
        # Only this operation's fresh UUID directory is eligible for cleanup.
        if destination.exists():
            if destination.resolve().parent!=root.resolve() or destination.is_symlink():
                raise ValueError('临时组件目录边界异常，请检查安装目录')
            shutil.rmtree(destination)
        raise
    finally:pointer.unlink(missing_ok=True)
    return version


def recognize(image,*,cancelled=None):
    cmd=command()
    if not cmd:raise RuntimeError('请先下载本地公式识别组件')
    if image.isNull() or image.width()*image.height()>MAX_PIXELS:raise ValueError('公式截图为空或过大，请框选单个公式')
    with TemporaryDirectory(prefix='pptdoctor-formula-') as directory:
        folder=Path(directory);source=folder/'formula.png';request=folder/'request.json';response=folder/'response.json'
        source.write_bytes(encode(image,'PNG'))
        request.write_text(json.dumps({'image':str(source)}),encoding='utf-8')
        run(cmd+['--request',str(request),'--response',str(response)],cancelled=cancelled,timeout=90)
        if not response.is_file() or response.stat().st_size>65536:raise ValueError('公式组件没有返回有效结果')
        payload=json.loads(response.read_text(encoding='utf-8'))
        if not payload.get('ok'):raise ValueError('公式识别失败：'+str(payload.get('error','请重新框选'))[:300])
        return clean_latex(payload.get('latex',''))
